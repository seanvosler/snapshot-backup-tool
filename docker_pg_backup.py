#!/usr/bin/env python3
import argparse, hashlib, json, os, shutil, socket, subprocess
from datetime import datetime
from pathlib import Path
from snapshot_paths import SOURCE_ROOT as ROOT, CONFIG, STATE_DIR as STATE, CACHE_DIR as CACHE, ensure_data_dirs

ensure_data_dirs()

def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("job")
    args=ap.parse_args()
    cfg=json.loads(CONFIG.read_text())
    job=next(j for j in cfg["jobs"] if j["name"]==args.job)
    name=job["name"]; container=job["container"]; db=job["database"]
    user=job.get("db_user","postgres")
    docker=job.get("docker","/usr/local/bin/docker")
    restore=job.get("pg_restore","/opt/homebrew/bin/pg_restore")
    dest=Path(os.path.expanduser(job["destination"])).resolve()
    keep=int(job.get("keep_local_snapshots",2))
    dest.mkdir(parents=True,exist_ok=True); CACHE.mkdir(parents=True,exist_ok=True); STATE.mkdir(parents=True,exist_ok=True)

    stamp=datetime.now().strftime("%Y-%m-%d_%H%M%S")
    safe="".join(c if c.isalnum() or c in "-_" else "-" for c in name)
    partial=CACHE/f"{safe}-{stamp}.dump.partial"
    staged=CACHE/f"{safe}-{stamp}.dump"
    partial.unlink(missing_ok=True); staged.unlink(missing_ok=True)

    with partial.open("wb") as out:
        cp=subprocess.run([docker,"exec",container,"pg_dump","-U",user,"-Fc","-d",db],stdout=out,stderr=subprocess.PIPE)
    if cp.returncode!=0:
        partial.unlink(missing_ok=True)
        raise SystemExit((cp.stderr or b"").decode(errors="ignore"))
    partial.replace(staged)

    v=subprocess.run([restore,"-l",str(staged)],capture_output=True,text=True)
    if v.returncode!=0 or not v.stdout.strip():
        staged.unlink(missing_ok=True)
        raise SystemExit(v.stderr or "pg_restore verification failed")

    h=hashlib.sha256()
    with staged.open("rb") as f:
        for chunk in iter(lambda:f.read(8*1024*1024),b""): h.update(chunk)
    digest=h.hexdigest(); size=staged.stat().st_size
    meta={"app":"Snapshot Backup","version":"1.0","hostname":socket.gethostname(),"created":now(),
          "job":name,"job_type":"docker-postgres","source":job.get("source"),"container":container,
          "database":db,"archive":staged.name,"archive_bytes":size,"archive_sha256":digest,
          "snapshot_method":"docker exec pg_dump -Fc + pg_restore -l verification"}
    m=CACHE/(staged.name+".json"); s=CACHE/(staged.name+".sha256")
    m.write_text(json.dumps(meta,indent=2)+"\n"); s.write_text(f"{digest}  {staged.name}\n")
    final=dest/staged.name
    shutil.move(str(staged),final); shutil.move(str(m),dest/m.name); shutil.move(str(s),dest/s.name)

    archives=sorted(dest.glob(f"{safe}-*.dump"),key=lambda p:p.stat().st_mtime,reverse=True)
    for old in archives[keep:]:
        for p in [old,Path(str(old)+".json"),Path(str(old)+".sha256")]:
            p.unlink(missing_ok=True)

    state={"last_check":now(),"last_result":"success","last_snapshot":now(),"last_archive":str(final),
           "last_archive_bytes":size,"file_count":1,"source_bytes":None,"job_type":"docker-postgres"}
    tmp=STATE/f"{name}.json.tmp"; final_state=STATE/f"{name}.json"
    tmp.write_text(json.dumps(state,indent=2)+"\n"); tmp.replace(final_state)
    print(json.dumps({"job":name,"result":"success",**state}))

if __name__=="__main__":
    main()
