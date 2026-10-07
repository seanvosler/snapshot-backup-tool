#!/usr/bin/env python3
from __future__ import annotations
import argparse, fnmatch, hashlib, json, os, shutil, socket, sqlite3, subprocess, sys, tempfile, time, fcntl
from datetime import datetime
from pathlib import Path
from snapshot_paths import SOURCE_ROOT as ROOT, CONFIG, STATE_DIR, LOG_DIR, CACHE_DIR, DATA_ROOT, ensure_data_dirs, backup_root_from_config

ensure_data_dirs()
STATUS = STATE_DIR / "status.json"

DEFAULT_EXCLUDES = [
    ".DS_Store", "node_modules", ".venv", "venv", "__pycache__",
    ".next", "dist", "build", "coverage", ".cache"
]

def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")

def log(msg, job=None):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{now_iso()}] " + (f"[{job}] " if job else "") + msg
    print(line, flush=True)
    with (LOG_DIR / "backup.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")

def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default

def save_json_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)

def load_config():
    if not CONFIG.exists():
        raise SystemExit(f"Missing config: {CONFIG}")
    return json.loads(CONFIG.read_text())

def should_exclude(rel: str, patterns: list[str]) -> bool:
    parts = rel.split("/")
    for pat in patterns:
        if fnmatch.fnmatch(rel, pat) or any(fnmatch.fnmatch(p, pat) for p in parts):
            return True
    return False

def scan_source(source: Path, excludes: list[str]):
    h = hashlib.sha256()
    file_count = 0
    total_bytes = 0
    records = []
    for base, dirs, files in os.walk(source, topdown=True, followlinks=False):
        basep = Path(base)
        relbase = os.path.relpath(basep, source)
        if relbase == ".": relbase = ""
        dirs[:] = sorted(d for d in dirs if not should_exclude("/".join(filter(None,[relbase,d])), excludes))
        for name in sorted(files):
            rel = "/".join(filter(None,[relbase,name]))
            if should_exclude(rel, excludes):
                continue
            p = basep / name
            try:
                st = p.lstat()
            except OSError:
                continue
            kind = "L" if p.is_symlink() else "F"
            size = st.st_size
            mtime_ns = st.st_mtime_ns
            rec = f"{kind}\t{rel}\t{size}\t{mtime_ns}"
            h.update((rec+"\n").encode("utf-8","surrogateescape"))
            records.append(rec)
            file_count += 1
            total_bytes += size
    return {
        "fingerprint": h.hexdigest(),
        "file_count": file_count,
        "total_bytes": total_bytes,
        "records": records,
    }

def run(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, **kw)

def archive_job(job, force=False):
    name = job["name"]
    source = Path(os.path.expanduser(job["source"])).resolve()
    dest = Path(os.path.expanduser(job["destination"])).resolve()
    excludes = job.get("exclude", DEFAULT_EXCLUDES)
    keep = int(job.get("keep_local_snapshots", 2))

    if not source.is_dir():
        raise RuntimeError(f"Source folder missing: {source}")
    dest.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    log("Scanning for changes…", name)
    scan = scan_source(source, excludes)
    state_path = STATE_DIR / f"{name}.json"
    old = load_json(state_path, {})
    if not force and job.get("only_if_changed", True) and old.get("fingerprint") == scan["fingerprint"]:
        log(f"No changes; skipped ({scan['file_count']:,} files).", name)
        old.update({"last_check": now_iso(), "last_result": "unchanged"})
        save_json_atomic(state_path, old)
        return {"job": name, "result": "unchanged", **scan}

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    fmt = job.get("format", "tar.zst")
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name)
    if fmt != "tar.zst":
        raise RuntimeError("Snapshot Backup currently supports tar.zst folder archives only")

    final_name = f"{safe}-{stamp}.tar.zst"
    partial = CACHE_DIR / (final_name + ".partial")
    staged = CACHE_DIR / final_name
    for p in (partial, staged):
        if p.exists(): p.unlink()

    log(f"Creating {final_name} from {scan['file_count']:,} files…", name)

    tar_cmd = ["/usr/bin/tar", "-c", "-f", "-", "-C", str(source.parent)]
    for pat in excludes:
        tar_cmd += ["--exclude", pat]
    tar_cmd += [source.name]

    tar = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    zstd = subprocess.Popen(
        ["/opt/homebrew/bin/zstd", "-T0", "-3", "-q", "-o", str(partial)],
        stdin=tar.stdout, stderr=subprocess.PIPE
    )
    tar.stdout.close()
    zerr = zstd.communicate()[1]
    terr = tar.stderr.read()
    trc = tar.wait()
    if trc != 0 or zstd.returncode != 0:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"Archive failed: tar={trc} zstd={zstd.returncode} {terr.decode(errors='ignore')} {zerr.decode(errors='ignore')}")

    partial.replace(staged)
    run(["/opt/homebrew/bin/zstd", "-t", "-q", str(staged)])
    sha = hashlib.sha256()
    with staged.open("rb") as f:
        for chunk in iter(lambda:f.read(8*1024*1024), b""):
            sha.update(chunk)
    digest = sha.hexdigest()
    archive_bytes = staged.stat().st_size

    meta = {
        "app": "Snapshot Backup",
        "version": "1.0",
        "hostname": socket.gethostname(),
        "created": now_iso(),
        "job": name,
        "source": str(source),
        "file_count": scan["file_count"],
        "source_bytes": scan["total_bytes"],
        "archive_bytes": archive_bytes,
        "archive": final_name,
        "archive_sha256": digest,
        "directory_fingerprint": scan["fingerprint"],
        "exclusions": excludes,
    }
    staged_meta = CACHE_DIR / (final_name + ".json")
    staged_sha = CACHE_DIR / (final_name + ".sha256")
    staged_meta.write_text(json.dumps(meta, indent=2) + "\n")
    staged_sha.write_text(f"{digest}  {final_name}\n")

    final = dest / final_name
    shutil.move(str(staged), final)
    shutil.move(str(staged_meta), dest / staged_meta.name)
    shutil.move(str(staged_sha), dest / staged_sha.name)
    log(f"Verified and moved to backup folder ({archive_bytes/1024/1024:.1f} MB).", name)

    # Retain newest N archives locally. Old removals are intentional; Google Drive can retain remote Trash.
    archives = sorted(dest.glob(f"{safe}-*.tar.zst"), key=lambda p:p.stat().st_mtime, reverse=True)
    for old_archive in archives[keep:]:
        for p in [old_archive, Path(str(old_archive)+".json"), Path(str(old_archive)+".sha256")]:
            if p.exists(): p.unlink()
        log(f"Pruned old local snapshot {old_archive.name}", name)

    state = {
        "fingerprint": scan["fingerprint"],
        "last_check": now_iso(),
        "last_result": "success",
        "last_snapshot": now_iso(),
        "last_archive": str(final),
        "last_archive_bytes": archive_bytes,
        "file_count": scan["file_count"],
        "source_bytes": scan["total_bytes"],
    }
    save_json_atomic(state_path, state)
    return {"job": name, "result": "success", **state}

def sqlite_fingerprint(source: Path):
    h=hashlib.sha256()
    total=0
    for p in [source, Path(str(source)+"-wal"), Path(str(source)+"-shm")]:
        if p.exists():
            st=p.stat()
            total += st.st_size
            h.update(f"{p.name}\t{st.st_size}\t{st.st_mtime_ns}\n".encode())
    return {"fingerprint":h.hexdigest(),"file_count":1,"total_bytes":total}

def sqlite_job(job, force=False):
    name=job["name"]
    source=Path(os.path.expanduser(job["source"])).resolve()
    dest=Path(os.path.expanduser(job["destination"])).resolve()
    keep=int(job.get("keep_local_snapshots",2))
    if not source.is_file():
        raise RuntimeError(f"SQLite source missing: {source}")
    dest.mkdir(parents=True,exist_ok=True)
    STATE_DIR.mkdir(parents=True,exist_ok=True)
    CACHE_DIR.mkdir(parents=True,exist_ok=True)

    log("Checking SQLite database for changes…",name)
    scan=sqlite_fingerprint(source)
    state_path=STATE_DIR/f"{name}.json"
    old=load_json(state_path,{})
    if not force and job.get("only_if_changed",True) and old.get("fingerprint")==scan["fingerprint"]:
        log("No SQLite changes; skipped.",name)
        old.update({"last_check":now_iso(),"last_result":"unchanged"})
        save_json_atomic(state_path,old)
        return {"job":name,"result":"unchanged",**scan}

    stamp=datetime.now().strftime("%Y-%m-%d_%H%M%S")
    safe="".join(c if c.isalnum() or c in "-_" else "-" for c in name)
    clean=CACHE_DIR/f"{safe}-{stamp}.sqlite"
    partial=CACHE_DIR/f"{safe}-{stamp}.sqlite.zst.partial"
    staged=CACHE_DIR/f"{safe}-{stamp}.sqlite.zst"
    for p in (clean,partial,staged):
        p.unlink(missing_ok=True)

    log("Creating transaction-consistent SQLite snapshot…",name)
    src=sqlite3.connect(f"file:{source}?mode=ro",uri=True,timeout=30)
    dst=sqlite3.connect(str(clean))
    try:
        src.backup(dst,pages=2048,sleep=0.05)
        row=dst.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0]!="ok":
            raise RuntimeError(f"SQLite integrity_check failed: {row}")
    finally:
        dst.close(); src.close()

    zr=subprocess.run(
        ["/opt/homebrew/bin/zstd","-T0","-3","-q","-o",str(partial),str(clean)],
        capture_output=True,text=True
    )
    clean.unlink(missing_ok=True)
    if zr.returncode!=0:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"zstd failed: {zr.stderr}")
    partial.replace(staged)
    run(["/opt/homebrew/bin/zstd","-t","-q",str(staged)])

    sha=hashlib.sha256()
    with staged.open("rb") as f:
        for chunk in iter(lambda:f.read(8*1024*1024),b""): sha.update(chunk)
    digest=sha.hexdigest()
    archive_bytes=staged.stat().st_size
    final_name=staged.name
    meta={
        "app":"Snapshot Backup","version":"1.0","hostname":socket.gethostname(),
        "created":now_iso(),"job":name,"job_type":"sqlite","source":str(source),
        "source_bytes":scan["total_bytes"],"archive_bytes":archive_bytes,
        "archive":final_name,"archive_sha256":digest,
        "directory_fingerprint":scan["fingerprint"],
        "snapshot_method":"Python sqlite3 backup API + PRAGMA integrity_check"
    }
    staged_meta=CACHE_DIR/(final_name+".json")
    staged_sha=CACHE_DIR/(final_name+".sha256")
    staged_meta.write_text(json.dumps(meta,indent=2)+"\n")
    staged_sha.write_text(f"{digest}  {final_name}\n")
    final=dest/final_name
    shutil.move(str(staged),final)
    shutil.move(str(staged_meta),dest/staged_meta.name)
    shutil.move(str(staged_sha),dest/staged_sha.name)
    log(f"Verified SQLite snapshot ({archive_bytes/1024/1024:.1f} MB).",name)

    archives=sorted(dest.glob(f"{safe}-*.sqlite.zst"),key=lambda p:p.stat().st_mtime,reverse=True)
    for old_archive in archives[keep:]:
        for p in [old_archive,Path(str(old_archive)+".json"),Path(str(old_archive)+".sha256")]:
            if p.exists(): p.unlink()
        log(f"Pruned old local snapshot {old_archive.name}",name)

    state={
        "fingerprint":scan["fingerprint"],"last_check":now_iso(),"last_result":"success",
        "last_snapshot":now_iso(),"last_archive":str(final),"last_archive_bytes":archive_bytes,
        "file_count":1,"source_bytes":scan["total_bytes"],"job_type":"sqlite"
    }
    save_json_atomic(state_path,state)
    return {"job":name,"result":"success",**state}

def postgres_job(job, force=False):
    name=job["name"]
    db=job["database"]
    host=job.get("host","localhost")
    port=str(job.get("port",5432))
    dest=Path(os.path.expanduser(job["destination"])).resolve()
    keep=int(job.get("keep_local_snapshots",2))
    pg_dump=job.get("pg_dump") or "/opt/homebrew/bin/pg_dump"
    pg_restore=job.get("pg_restore") or "/opt/homebrew/bin/pg_restore"
    for exe in (pg_dump,pg_restore):
        if not Path(exe).exists():
            raise RuntimeError(f"PostgreSQL tool missing: {exe}")
    dest.mkdir(parents=True,exist_ok=True)
    STATE_DIR.mkdir(parents=True,exist_ok=True)
    CACHE_DIR.mkdir(parents=True,exist_ok=True)

    stamp=datetime.now().strftime("%Y-%m-%d_%H%M%S")
    safe="".join(c if c.isalnum() or c in "-_" else "-" for c in name)
    partial=CACHE_DIR/f"{safe}-{stamp}.dump.partial"
    staged=CACHE_DIR/f"{safe}-{stamp}.dump"
    for p in (partial,staged):
        p.unlink(missing_ok=True)

    log(f"Creating PostgreSQL logical dump of {db}…",name)
    cp=subprocess.run(
        [pg_dump,"-h",host,"-p",port,"-Fc","-d",db,"-f",str(partial)],
        capture_output=True,text=True
    )
    if cp.returncode!=0:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"pg_dump failed: {cp.stderr.strip()}")

    partial.replace(staged)
    verify=subprocess.run([pg_restore,"-l",str(staged)],capture_output=True,text=True)
    if verify.returncode!=0 or not verify.stdout.strip():
        staged.unlink(missing_ok=True)
        raise RuntimeError(f"pg_restore verification failed: {verify.stderr.strip()}")

    sha=hashlib.sha256()
    with staged.open("rb") as f:
        for chunk in iter(lambda:f.read(8*1024*1024),b""): sha.update(chunk)
    digest=sha.hexdigest()
    archive_bytes=staged.stat().st_size
    final_name=staged.name
    meta={
        "app":"Snapshot Backup","version":"1.0","hostname":socket.gethostname(),
        "created":now_iso(),"job":name,"job_type":"postgres","source":job.get("source"),
        "database":db,"host":host,"port":int(port),"archive_bytes":archive_bytes,
        "archive":final_name,"archive_sha256":digest,
        "snapshot_method":"pg_dump -Fc + pg_restore -l verification"
    }
    staged_meta=CACHE_DIR/(final_name+".json")
    staged_sha=CACHE_DIR/(final_name+".sha256")
    staged_meta.write_text(json.dumps(meta,indent=2)+"\n")
    staged_sha.write_text(f"{digest}  {final_name}\n")
    final=dest/final_name
    shutil.move(str(staged),final)
    shutil.move(str(staged_meta),dest/staged_meta.name)
    shutil.move(str(staged_sha),dest/staged_sha.name)
    log(f"Verified PostgreSQL dump ({archive_bytes/1024/1024:.1f} MB).",name)

    archives=sorted(dest.glob(f"{safe}-*.dump"),key=lambda p:p.stat().st_mtime,reverse=True)
    for old_archive in archives[keep:]:
        for p in [old_archive,Path(str(old_archive)+".json"),Path(str(old_archive)+".sha256")]:
            if p.exists(): p.unlink()
        log(f"Pruned old local snapshot {old_archive.name}",name)

    state={
        "last_check":now_iso(),"last_result":"success","last_snapshot":now_iso(),
        "last_archive":str(final),"last_archive_bytes":archive_bytes,
        "file_count":1,"source_bytes":None,"job_type":"postgres"
    }
    save_json_atomic(STATE_DIR/f"{name}.json",state)
    return {"job":name,"result":"success",**state}

def docker_postgres_job(job, force=False):
    cp=subprocess.run([sys.executable,str(ROOT/"docker_pg_backup.py"),job["name"]],capture_output=True,text=True)
    if cp.returncode!=0:
        raise RuntimeError(cp.stderr.strip() or cp.stdout.strip() or "Docker PostgreSQL backup failed")
    lines=[x for x in cp.stdout.splitlines() if x.strip()]
    return json.loads(lines[-1]) if lines else {"job":job["name"],"result":"success"}

def update_global_status(results=None, running=False, error=None):
    current = load_json(STATUS, {})
    current.update({
        "version":"1.0",
        "updated": now_iso(),
        "running": running,
        "hostname": socket.gethostname(),
    })
    if results is not None: current["last_results"] = results
    if error is not None: current["last_error"] = error
    elif results is not None: current["last_error"] = None
    save_json_atomic(STATUS, current)

def run_all(force=False, only=None):
    cfg = load_config()
    jobs = [j for j in cfg.get("jobs",[]) if j.get("enabled",True)]
    if only:
        jobs = [j for j in jobs if j["name"] == only]
    update_global_status(running=True)
    results=[]
    try:
        for job in jobs:
            try:
                typ=job.get("type","folder")
                runner = sqlite_job if typ=="sqlite" else (postgres_job if typ=="postgres" else (docker_postgres_job if typ=="docker-postgres" else archive_job))
                results.append(runner(job, force=force))
            except Exception as e:
                log(f"ERROR: {e}", job.get("name"))
                results.append({"job":job.get("name"),"result":"error","error":str(e)})
        update_global_status(results=results, running=False)
        return 1 if any(r["result"]=="error" for r in results) else 0
    except Exception as e:
        update_global_status(running=False,error=str(e))
        raise

def run_locked(force=False, only=None):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = STATE_DIR / "backup.lock"
    with lock_path.open("w") as lf:
        try:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("Backup already running; this invocation was skipped.")
            return 0
        lf.write(str(os.getpid()))
        lf.flush()
        return run_all(force, only)

def _slug_name(s):
    out="".join(c.lower() if c.isalnum() else "-" for c in s).strip("-")
    while "--" in out: out=out.replace("--","-")
    return out[:56] or "folder"

def _path_relation(a: Path, b: Path):
    a=a.resolve(); b=b.resolve()
    if a==b: return "same"
    try:
        a.relative_to(b); return "inside"
    except Exception:
        pass
    try:
        b.relative_to(a); return "contains"
    except Exception:
        pass
    return None

def add_folder_job(source_raw):
    source=Path(os.path.expanduser(source_raw)).resolve()
    if not source.is_dir():
        raise SystemExit(f"Selected folder does not exist: {source}")
    cfg=load_config()
    for j in cfg.get("jobs",[]):
        if j.get("type","folder")!="folder" or not j.get("enabled",True):
            continue
        other=Path(os.path.expanduser(j["source"])).resolve()
        rel=_path_relation(source,other)
        if rel=="same":
            raise SystemExit(f"That folder is already backed up by '{j['name']}'.")
        if rel=="inside":
            raise SystemExit(f"That folder is already covered by parent backup '{j['name']}' ({other}).")
        if rel=="contains":
            raise SystemExit(f"That folder contains existing backup '{j['name']}' ({other}); overlapping folder jobs are not allowed.")
    base=_slug_name(source.name)
    existing={j.get("name") for j in cfg.get("jobs",[])}
    name=base; n=2
    while name in existing:
        name=f"{base}-{n}"; n+=1
    dest=(backup_root_from_config(cfg)/name).resolve()
    cfg.setdefault("jobs",[]).append({
        "name":name,"type":"folder","enabled":True,
        "source":str(source),"destination":str(dest),
        "format":"tar.zst","only_if_changed":True,
        "keep_local_snapshots":2,
        "exclude":[
            ".DS_Store","node_modules",".venv","venv","__pycache__",
            ".next","dist","build","coverage",".cache",
            "*.log","*.db-wal","*.db-shm","*.sqlite-wal","*.sqlite-shm"
        ]
    })
    save_json_atomic(CONFIG,cfg)
    print(json.dumps({"ok":True,"name":name,"source":str(source),"destination":str(dest)}))

def remove_job(name):
    cfg=load_config()
    jobs=cfg.get("jobs",[])
    job=next((j for j in jobs if j.get("name")==name),None)
    if not job:
        raise SystemExit(f"Backup job not found: {name}")
    destination=Path(os.path.expanduser(job.get("destination",""))).resolve() if job.get("destination") else None
    backups_root=backup_root_from_config(cfg).resolve()
    removed_destination=False
    if destination and destination.exists():
        try:
            destination.relative_to(backups_root)
        except Exception:
            raise SystemExit(f"Refusing to remove destination outside BACKUPS: {destination}")
        shutil.rmtree(destination)
        removed_destination=True
    state=STATE_DIR/f"{name}.json"
    state.unlink(missing_ok=True)
    cfg["jobs"]=[j for j in jobs if j.get("name")!=name]
    save_json_atomic(CONFIG,cfg)
    log(f"Removed backup job; destination cleanup={removed_destination}",name)
    print(json.dumps({"ok":True,"name":name,"removed_destination":removed_destination}))

def status():
    cfg=load_config()
    out={"global":load_json(STATUS,{}),"jobs":[]}
    for j in cfg.get("jobs",[]):
        s=load_json(STATE_DIR/f"{j['name']}.json",{})
        out["jobs"].append({"config":j,"state":s})
    print(json.dumps(out,indent=2))

def main():
    ap=argparse.ArgumentParser()
    sub=ap.add_subparsers(dest="cmd",required=True)
    r=sub.add_parser("run-all"); r.add_argument("--force",action="store_true")
    one=sub.add_parser("run"); one.add_argument("job"); one.add_argument("--force",action="store_true")
    addf=sub.add_parser("add-folder"); addf.add_argument("source")
    rm=sub.add_parser("remove-job"); rm.add_argument("job")
    sub.add_parser("status")
    args=ap.parse_args()
    if args.cmd=="run-all": raise SystemExit(run_locked(args.force))
    if args.cmd=="run": raise SystemExit(run_locked(args.force,args.job))
    if args.cmd=="add-folder": return add_folder_job(args.source)
    if args.cmd=="remove-job": return remove_job(args.job)
    status()

if __name__=="__main__":
    main()
