#!/usr/bin/env python3
import os
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get(
    "SNAPSHOT_BACKUP_DATA_DIR",
    str(Path.home() / "Library" / "Application Support" / "Snapshot Backup")
)).expanduser()
RUNTIME_ROOT = DATA_ROOT / "runtime"
CONFIG = DATA_ROOT / "config.json"
STATE_DIR = DATA_ROOT / "state"
LOG_DIR = DATA_ROOT / "logs"
CACHE_DIR = Path.home() / "Library" / "Caches" / "SnapshotBackup" / "staging"
DEFAULT_BACKUP_ROOT = Path.home() / "SnapshotBackups"

def backup_root_from_config(cfg=None):
    if cfg and cfg.get("backup_root"):
        return Path(os.path.expanduser(cfg["backup_root"])).resolve()
    return DEFAULT_BACKUP_ROOT.resolve()

def ensure_data_dirs():
    for p in (DATA_ROOT, STATE_DIR, LOG_DIR, RUNTIME_ROOT):
        p.mkdir(parents=True, exist_ok=True)
