# Snapshot Backup — V1.0

A local-first macOS snapshot utility for folders and local databases that are awkward for Google Drive Desktop to sync directly.

## Portable architecture

The project source is now intentionally separate from installed runtime/state.

### Source repo

This folder can live anywhere, for example:

`~/git/snapshot-backup`

Tracked source includes:

- `backupctl.py` — backup engine / CLI
- `db_discovery.py` — local-data discovery and database enrollment
- `docker_pg_backup.py` — Docker PostgreSQL logical backup helper
- `snapshot_paths.py` — portable runtime/data paths
- `SnapshotBackup.swift` — native menu-bar + management UI
- `macos/Info.plist` — app bundle metadata
- `install.command` — builds/deploys the stable runtime and launchd jobs
- `config.example.json` — blank example config

### Stable installed runtime

`~/Library/Application Support/Snapshot Backup/runtime/`

This contains installed copies of the Python engine and the compiled `Snapshot Backup.app`. launchd points here, **not at the source repo**.

That means the source repo may be renamed, moved, cloned elsewhere, or temporarily unavailable without breaking scheduled backups.

### Mutable user state

`~/Library/Application Support/Snapshot Backup/`

Contains:

- `config.json`
- `state/`
- `logs/`
- `runtime/`

These are intentionally outside Git.

### Backup payloads

New installs default to writing completed archives to:

`~/SnapshotBackups`

The actual location is controlled by top-level `backup_root` in the installed `config.json`, so existing installs keep their current destination unless you change it explicitly.

## Installing / updating

Run:

```bash
./install.command
```

The installer:

1. migrates old source-local config/state/logs if necessary;
2. preserves existing mutable data;
3. installs the runtime under Application Support;
4. compiles and signs the native menu-bar app;
5. regenerates launchd jobs using stable installed paths;
6. starts the menu app;
7. leaves the nightly 9 PM scheduler enabled.

After source-code changes, run `install.command` again to deploy them.

## Moving or renaming the source repo

Once `install.command` has completed successfully, moving the source repo does **not** interrupt the installed service.

After moving, run `install.command` once from the new repo location before doing further development. This refreshes the installed runtime from that checkout, although the existing installed runtime would continue working even without doing so.

## Folder snapshots

Folder jobs use `.tar.zst`.

- metadata fingerprinting skips unchanged folders;
- archives are staged outside Google Drive;
- `zstd -t` verifies them;
- SHA-256 + JSON metadata are generated;
- only completed archives move to `BACKUPS/`;
- two newest snapshots are retained locally;
- overlapping/nested folder jobs are blocked.

Typical dev-noise exclusions include `node_modules`, virtualenvs, caches, build output, logs, and SQLite WAL/SHM sidecars. `.git` is intentionally preserved.

## Local Data Discovery

Discovery surfaces local-only data that may be painful to lose:

- SQLite / generic DB files
- Homebrew PostgreSQL databases
- Docker PostgreSQL databases and persistent volumes
- DuckDB, Realm, LMDB, Redis and other local stores
- project-local database files
- sanitized `.env` database references
- Homebrew service data

Discovery is deliberately aggressive; enrollment is deliberate. Nothing is automatically added to backups.

The UI labels likely production/application databases as **Recommended**, test-like databases as **Likely testing DB**, and infrastructure-only entries separately.

## Safe database backups

### SQLite

Uses the SQLite online backup API, then `PRAGMA integrity_check`, compression verification and SHA-256.

### Homebrew PostgreSQL

Uses `pg_dump -Fc`, validates with `pg_restore -l`, then hashes and stores the dump.

### Docker PostgreSQL

Runs `pg_dump -Fc` inside the container, validates the resulting dump locally with `pg_restore -l`, then hashes and stores it.

Raw live PostgreSQL data directories are never blindly copied.

## Native UI

The menu-bar app provides:

- Back Up Now
- Manage Backups…
- Scan for Local Data…
- Open Backup Folder
- Open Logs

The management window provides:

- folder/database type icons
- per-job status and Run Now
- Add Folder…
- duplicate/nested-folder protection
- Remove… with confirmation and generated-backup cleanup
- Local Data Discovery
- ignore controls
- safe database enrollment

The UI is not a single point of failure. The nightly scheduler runs independently through launchd.

## CLI

```bash
python3 backupctl.py status
python3 backupctl.py run-all
python3 backupctl.py run git
python3 backupctl.py add-folder /path/to/folder
python3 backupctl.py remove-job job-name

python3 db_discovery.py scan
python3 db_discovery.py add-sqlite /path/to/database.sqlite
python3 db_discovery.py add-postgres postgres://localhost:5433/database_name
python3 db_discovery.py add-docker-postgres docker-postgres://container/database
```

## Git

`.gitignore` excludes mutable state, local config, logs, caches, and compiled app artifacts. The repository should contain source and example configuration only.
