#!/bin/zsh
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "$0")" && pwd)"
DATA_ROOT="$HOME/Library/Application Support/Snapshot Backup"
RUNTIME_ROOT="$DATA_ROOT/runtime"
APP_ROOT="$RUNTIME_ROOT/Snapshot Backup.app"
LAUNCH_DIR="$HOME/Library/LaunchAgents"
BACKUP_ROOT_DEFAULT="$HOME/SnapshotBackups"
PYTHON="$(command -v python3)"

mkdir -p "$DATA_ROOT/state" "$DATA_ROOT/logs" "$RUNTIME_ROOT" "$LAUNCH_DIR" "$BACKUP_ROOT_DEFAULT"

# One-time migration from the old source-local layout.
if [[ ! -f "$DATA_ROOT/config.json" ]]; then
  if [[ -f "$SOURCE_ROOT/config.json" ]]; then
    cp "$SOURCE_ROOT/config.json" "$DATA_ROOT/config.json"
  elif [[ -f "$SOURCE_ROOT/config.example.json" ]]; then
    cp "$SOURCE_ROOT/config.example.json" "$DATA_ROOT/config.json"
  fi
fi

for dir in state logs; do
  if [[ -d "$SOURCE_ROOT/$dir" ]]; then
    cp -n "$SOURCE_ROOT/$dir/"* "$DATA_ROOT/$dir/" 2>/dev/null || true
  fi
done

"$PYTHON" - <<PY
from pathlib import Path
import json
p=Path.home()/"Library"/"Application Support"/"Snapshot Backup"/"config.json"
if not p.exists():
    raise SystemExit("No config.json available to install.")
d=json.loads(p.read_text())
d.setdefault("version",1)
d.setdefault("schedule","21:00")
d.setdefault("backup_root",str(Path.home()/"SnapshotBackups"))
d.setdefault("jobs",[])
p.write_text(json.dumps(d,indent=2)+"\n")
PY

# Install the movable runtime. Mutable state remains outside this directory.
for f in backupctl.py db_discovery.py docker_pg_backup.py snapshot_paths.py; do
  cp "$SOURCE_ROOT/$f" "$RUNTIME_ROOT/$f"
done
chmod +x "$RUNTIME_ROOT/"*.py

mkdir -p "$APP_ROOT/Contents/MacOS"
cp "$SOURCE_ROOT/macos/Info.plist" "$APP_ROOT/Contents/Info.plist"
/usr/bin/swiftc -framework Cocoa "$SOURCE_ROOT/SnapshotBackup.swift" -o "$APP_ROOT/Contents/MacOS/SnapshotBackup"
/usr/bin/codesign --force --deep --sign - "$APP_ROOT" >/dev/null

SCHED="$LAUNCH_DIR/com.snapshotbackup.tool.scheduler.plist"
MENU="$LAUNCH_DIR/com.snapshotbackup.tool.menu.plist"

cat > "$SCHED" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.snapshotbackup.tool.scheduler</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$RUNTIME_ROOT/backupctl.py</string>
    <string>run-all</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>21</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>$DATA_ROOT/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$DATA_ROOT/logs/launchd.err.log</string>
</dict></plist>
PLIST

cat > "$MENU" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.snapshotbackup.tool.menu</string>
  <key>ProgramArguments</key>
  <array><string>$APP_ROOT/Contents/MacOS/SnapshotBackup</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
  <key>StandardOutPath</key><string>$DATA_ROOT/logs/menu.out.log</string>
  <key>StandardErrorPath</key><string>$DATA_ROOT/logs/menu.err.log</string>
</dict></plist>
PLIST

plutil -lint "$SCHED" "$MENU" >/dev/null

uid="$(id -u)"
launchctl bootout "gui/$uid/com.snapshotbackup.tool.scheduler" 2>/dev/null || true
launchctl bootout "gui/$uid/com.snapshotbackup.tool.menu" 2>/dev/null || true
launchctl bootstrap "gui/$uid" "$SCHED"
launchctl bootstrap "gui/$uid" "$MENU"
launchctl enable "gui/$uid/com.snapshotbackup.tool.scheduler"
launchctl enable "gui/$uid/com.snapshotbackup.tool.menu"

echo
echo "Snapshot Backup installed."
echo "Source:  $SOURCE_ROOT"
echo "Runtime: $RUNTIME_ROOT"
echo "Data:    $DATA_ROOT"
echo "Backups: $("$PYTHON" -c 'import json,pathlib; p=pathlib.Path.home()/"Library/Application Support"/"Snapshot Backup"/"config.json"; print(json.load(open(p))["backup_root"])')"
echo
echo "The source folder may now be moved or renamed without breaking the installed service."
echo "After source updates, run install.command again to deploy the new version."
