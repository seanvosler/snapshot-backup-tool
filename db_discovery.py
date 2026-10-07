#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, os, stat, subprocess, time
from datetime import datetime
from pathlib import Path
from snapshot_paths import SOURCE_ROOT as ROOT, CONFIG, STATE_DIR as STATE, DATA_ROOT, ensure_data_dirs, backup_root_from_config

ensure_data_dirs()
OUT=STATE/"database_discovery.json"
IGNORED=STATE/"database_ignored.json"
HOME=Path.home()

FILE_EXTS={".sqlite":"SQLite",".sqlite3":"SQLite",".db":"Database file",".duckdb":"DuckDB",".realm":"Realm"}
SPECIAL_NAMES={"data.mdb":"LMDB","dump.rdb":"Redis","appendonly.aof":"Redis"}

ALLOWED_PREFIXES=[
    str(HOME/"git"),
    str(HOME/"Documents"),
    str(HOME/"Desktop"),
    str(HOME/"Downloads"),
    str(HOME/"Projects"),
    str(HOME/"Developer"),
    str(HOME/"Development"),
    str(HOME/"Sites"),
    str(HOME/"Library"/"Application Support"),
]
SKIP_PREFIXES=[
    str(HOME/"Library"/"Caches"),
    str(HOME/"Library"/"CloudStorage"),
]
NOISY_BITS=[
    "/Google/Chrome/","/Safari/","/Firefox/","/Microsoft Edge/",
    "/Library/Mail/","/Library/Messages/","/Library/Calendars/",
    "/Library/Application Support/AddressBook/",
    "/Library/Application Support/com.apple.",
    "/Library/Application Support/Google/",
    "/Library/Application Support/Slack/",
    "/Library/Application Support/discord/",
]

def iso(ts=None):
    return datetime.fromtimestamp(ts or time.time()).astimezone().isoformat(timespec="seconds")

def load_config():
    return json.loads(CONFIG.read_text()) if CONFIG.exists() else {"jobs":[]}

def load_ignored():
    if not IGNORED.exists(): return set()
    try:
        data=json.loads(IGNORED.read_text())
        return set(data.get("paths",[]))
    except Exception:
        return set()

def save_ignored(paths):
    STATE.mkdir(parents=True,exist_ok=True)
    tmp=IGNORED.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version":1,"paths":sorted(paths)},indent=2)+"\n")
    tmp.replace(IGNORED)

def lexical_under(path:str,parent:str):
    try:
        return os.path.commonpath([os.path.abspath(path),os.path.abspath(parent)])==os.path.abspath(parent)
    except Exception:
        return False

def configured_coverage(path,cfg):
    p=str(path)
    hits=[]
    for j in cfg.get("jobs",[]):
        if not j.get("enabled",True): continue
        src=os.path.expanduser(j.get("source",""))
        if not src: continue
        typ=j.get("type","folder")
        if typ=="folder":
            if lexical_under(p,src):
                hits.append({"job":j.get("name"),"type":typ,"mode":"folder"})
        elif typ in ("postgres","docker-postgres"):
            if p==src:
                hits.append({"job":j.get("name"),"type":typ,"mode":"logical"})
        elif os.path.abspath(p)==os.path.abspath(src):
            hits.append({"job":j.get("name"),"type":typ,"mode":"direct"})
    return hits

def classify_file(p:Path):
    kind=SPECIAL_NAMES.get(p.name.lower()) or FILE_EXTS.get(p.suffix.lower())
    try:
        with p.open("rb") as f: header=f.read(32)
    except OSError:
        return None
    if header.startswith(b"SQLite format 3\x00"):
        return "SQLite"
    return kind

def score(p:Path,kind,covered):
    s=50; sp=str(p)
    if kind=="SQLite": s+=25
    if kind in ("DuckDB","Realm","LMDB","Redis"): s+=15
    if any(x in sp for x in NOISY_BITS): s-=45
    if "/Library/Application Support/" in sp: s-=35
    if "/git/" in sp or "/Projects/" in sp or "/Documents/" in sp: s+=10
    low=sp.lower()
    noisy_names=("logs.sqlite","cookies.sqlite","places.sqlite","favicons.sqlite",
                 "formhistory.sqlite","permissions.sqlite","webappsstore.sqlite",
                 "google-analytics.sqlite","telemetry","persistentcache",
                 "qtwebengine","appcenter","sqliteResumeTransfer".lower())
    if any(x in low for x in noisy_names): s-=35
    if covered: s-=10
    return max(0,min(100,s))

def spotlight(query,timeout=15):
    try:
        cp=subprocess.run(
            ["/usr/bin/mdfind","-onlyin",str(HOME),query],
            capture_output=True,text=True,timeout=timeout
        )
        return [Path(x) for x in cp.stdout.splitlines() if x.strip()]
    except Exception:
        return []

def discover_homebrew_postgres(cfg,ignored):
    items=[]
    brew_var=Path("/opt/homebrew/var")
    if not brew_var.is_dir():
        return items
    clusters=[]
    for p in sorted(brew_var.glob("postgresql*")):
        if (p/"PG_VERSION").is_file():
            clusters.append(p)
    for cluster in clusters:
        try:
            version=(cluster/"PG_VERSION").read_text().strip()
        except Exception:
            version="?"
        port=5432
        conf=cluster/"postgresql.conf"
        if conf.is_file():
            try:
                for line in conf.read_text(errors="ignore").splitlines():
                    s=line.strip()
                    if s.startswith("port") and "=" in s and not s.startswith("#"):
                        port=int(s.split("=",1)[1].split("#",1)[0].strip())
                        break
            except Exception:
                pass
        psql=Path(f"/opt/homebrew/opt/postgresql@{version}/bin/psql")
        if not psql.exists():
            candidates=list(Path("/opt/homebrew/opt").glob("postgresql*/bin/psql"))
            if candidates: psql=candidates[-1]
        server_key=f"postgres://localhost:{port}"
        if str(server_key) not in ignored:
            items.append({
                "path":server_key,"kind":"PostgreSQL server","is_directory":True,
                "size":None,"modified":None,"coverage":[],
                "safe_mode":f"Homebrew PostgreSQL {version} · data: {cluster} · infrastructure only",
                "score":40,"engine":"postgres","port":port,"version":version,
                "cluster_path":str(cluster),
                "recommendation":"Infrastructure only",
                "attention":False
            })
        if not psql.exists():
            continue
        try:
            q="select datname, pg_database_size(datname) from pg_database where datistemplate = false order by pg_database_size(datname) desc;"
            cp=subprocess.run([str(psql),"-p",str(port),"-Atqc",q,"postgres"],capture_output=True,text=True,timeout=8)
            if cp.returncode!=0:
                continue
            for line in cp.stdout.splitlines():
                if "|" not in line: continue
                db,size=line.split("|",1)
                key=f"postgres://localhost:{port}/{db}"
                if key in ignored: continue
                cov=configured_coverage(key,cfg)
                dbscore=95
                low=db.lower()
                recommendation="Recommended"
                attention=True
                if db=="postgres":
                    dbscore=35
                    recommendation="Default/system DB"
                    attention=False
                elif low.startswith(("test","tmp","temp")) or "_test" in low or "_t_" in low:
                    dbscore=55
                    recommendation="Likely testing DB"
                items.append({
                    "path":key,"kind":"PostgreSQL database","is_directory":False,
                    "size":int(size) if size.isdigit() else None,"modified":None,
                    "coverage":cov,"safe_mode":"pg_dump custom-format logical backup",
                    "score":dbscore-(10 if cov else 0),
                    "engine":"postgres","port":port,"version":version,
                    "database":db,"cluster_path":str(cluster),
                    "postgres_safe_configured":any(x.get("type")=="postgres" for x in cov),
                    "recommendation":recommendation,
                    "attention":attention
                })
        except Exception:
            continue
    return items


def discover_docker_volumes(cfg,ignored):
    items=[]
    docker=Path("/usr/local/bin/docker")
    if not docker.exists():
        docker=Path("/opt/homebrew/bin/docker")
    if not docker.exists():
        return items
    try:
        cp=subprocess.run([str(docker),"volume","ls","-q"],capture_output=True,text=True,timeout=8)
        if cp.returncode!=0: return items
        names=[x.strip() for x in cp.stdout.splitlines() if x.strip()]
    except Exception:
        return items
    for name in names[:200]:
        key=f"docker-volume://{name}"
        if key in ignored: continue
        labels={}
        try:
            ip=subprocess.run([str(docker),"volume","inspect",name,"--format","{{json .Labels}}"],capture_output=True,text=True,timeout=5)
            if ip.returncode==0 and ip.stdout.strip() not in ("","null","<no value>"):
                labels=json.loads(ip.stdout.strip())
        except Exception:
            labels={}
        containers=[]
        try:
            cp=subprocess.run([str(docker),"ps","-a","--filter",f"volume={name}","--format","{{.Names}}|{{.Image}}|{{.Status}}"],capture_output=True,text=True,timeout=5)
            containers=[x for x in cp.stdout.splitlines() if x.strip()]
        except Exception:
            pass
        joined=" ".join(containers).lower()
        engine=None
        for e in ("postgres","mysql","mariadb","mongo","redis"):
            if e in joined or e in name.lower():
                engine=e; break
        kind="Docker database volume" if engine else "Docker volume"
        score=92 if engine else 68
        detail=(f"{engine} persistence" if engine else "persistent Docker volume")
        if containers: detail += " · " + "; ".join(containers[:3])
        items.append({
            "path":key,"kind":kind,"is_directory":True,"size":None,"modified":None,
            "coverage":[],"safe_mode":detail+" · inspect before backup",
            "score":score,"engine":engine or "docker","volume":name,"labels":labels,
            "containers":containers,
            "recommendation":"Persistent service data" if engine else "Review",
            "attention":True
        })

        if engine=="postgres" and containers:
            for row in containers:
                parts=row.split("|",2)
                container=parts[0]
                status=parts[2] if len(parts)>2 else ""
                if not status.lower().startswith("up"): continue
                try:
                    up=subprocess.run(
                        [str(docker),"exec",container,"sh","-lc",'printf "%s" "${POSTGRES_USER:-postgres}"'],
                        capture_output=True,text=True,timeout=5
                    )
                    db_user=up.stdout.strip() or "postgres"
                    qp=subprocess.run(
                        [str(docker),"exec",container,"psql","-U",db_user,"-Atqc",
                         "select datname, pg_database_size(datname) from pg_database where datistemplate=false order by pg_database_size(datname) desc;",
                         "postgres"],
                        capture_output=True,text=True,timeout=8
                    )
                    if qp.returncode!=0: continue
                    for line in qp.stdout.splitlines():
                        if "|" not in line: continue
                        db,size=line.split("|",1)
                        dbkey=f"docker-postgres://{container}/{db}"
                        if dbkey in ignored: continue
                        cov=configured_coverage(dbkey,cfg)
                        dbscore=96
                        low=db.lower()
                        recommendation="Recommended"
                        attention=True
                        if db=="postgres":
                            dbscore=35
                            recommendation="Default/system DB"
                            attention=False
                        elif low.startswith(("test","tmp","temp")) or "_test" in low or "_t_" in low:
                            dbscore=55
                            recommendation="Likely testing DB"
                        items.append({
                            "path":dbkey,"kind":"Docker PostgreSQL database","is_directory":False,
                            "size":int(size) if size.isdigit() else None,"modified":None,
                            "coverage":cov,"safe_mode":f"{container} · pg_dump inside container",
                            "score":dbscore-(10 if cov else 0),"engine":"docker-postgres",
                            "container":container,"database":db,"db_user":db_user,
                            "volume":name,
                            "postgres_safe_configured":any(x.get("type")=="docker-postgres" for x in cov),
                            "recommendation":recommendation,
                            "attention":attention
                        })
                except Exception:
                    pass
    return items

def discover_homebrew_data(cfg,ignored):
    items=[]
    var=Path("/opt/homebrew/var")
    if not var.is_dir(): return items
    patterns=[
        ("mysql*","Homebrew MySQL data","mysql"),
        ("mariadb*","Homebrew MariaDB data","mariadb"),
        ("redis*","Homebrew Redis data","redis"),
        ("mongodb*","Homebrew MongoDB data","mongodb"),
    ]
    for pat,kind,engine in patterns:
        for p in sorted(var.glob(pat)):
            if not p.is_dir(): continue
            key=f"homebrew-data://{p}"
            if key in ignored: continue
            items.append({
                "path":key,"kind":kind,"is_directory":True,"size":None,"modified":None,
                "coverage":[],"safe_mode":f"persistent service data at {p} · engine-aware backup recommended",
                "score":88,"engine":engine,"data_path":str(p),
                "recommendation":"Recommended",
                "attention":True
            })
    return items

def sanitize_db_url(value):
    try:
        from urllib.parse import urlsplit
        u=urlsplit(value.strip().strip('"').strip("'"))
        if not u.scheme: return None
        host=u.hostname or ""
        port=f":{u.port}" if u.port else ""
        path=u.path or ""
        return f"{u.scheme}://{host}{port}{path}"
    except Exception:
        return None


def discover_project_db_files(cfg,ignored):
    items=[]
    roots=[HOME/"git",HOME/"Projects",HOME/"Developer",HOME/"Development",HOME/"Sites"]
    skip={"node_modules",".git",".venv","venv","dist","build",".next","coverage",".cache","__pycache__"}
    seen=set()
    for root in roots:
        if not root.is_dir(): continue
        for base,dirs,files in os.walk(root,topdown=True,followlinks=False):
            bp=Path(base)
            dirs[:]=[d for d in dirs if d not in skip]
            try:
                depth=len(bp.relative_to(root).parts)
            except Exception:
                depth=99
            if depth>7:
                dirs[:]=[]
            for fn in files:
                p=bp/fn
                low=fn.lower()
                if p.suffix.lower() not in FILE_EXTS and low not in SPECIAL_NAMES:
                    continue
                key=str(p)
                if key in ignored or key in seen: continue
                seen.add(key)
                kind=SPECIAL_NAMES.get(low) or FILE_EXTS.get(p.suffix.lower()) or "Database file"
                cov=configured_coverage(p,cfg)
                scorev=96 if not cov else 80
                items.append({
                    "path":key,"kind":kind,"is_directory":False,"size":None,"modified":None,
                    "coverage":cov,
                    "safe_mode":f"development project data · {p.parent.name}",
                    "score":scorev,"engine":"project-file",
                    "sqlite_safe_configured":any(x.get("type")=="sqlite" and x.get("mode")=="direct" for x in cov),
                    "recommendation":"Recommended" if not cov else "Covered",
                    "attention":True
                })
    return items

def discover_project_db_refs(cfg,ignored):
    items=[]
    roots=[HOME/"git",HOME/"Projects",HOME/"Developer",HOME/"Development",HOME/"Sites"]
    skip={"node_modules",".git",".venv","venv","dist","build",".next","coverage"}
    interesting=("DATABASE_URL","DB_URL","POSTGRES_URL","POSTGRESQL_URL","MYSQL_URL","MARIADB_URL","MONGO_URL","MONGODB_URI","REDIS_URL")
    seen=set()
    for root in roots:
        if not root.is_dir(): continue
        for base,dirs,files in os.walk(root,topdown=True,followlinks=False):
            dirs[:]=[d for d in dirs if d not in skip]
            bp=Path(base)
            # keep this bounded to project-ish depth
            try:
                depth=len(bp.relative_to(root).parts)
            except Exception:
                depth=99
            if depth>4:
                dirs[:]=[]
            for fn in files:
                if not (fn==".env" or fn.startswith(".env.")): continue
                if "example" in fn.lower() or "sample" in fn.lower() or "template" in fn.lower(): continue
                p=bp/fn
                try: lines=p.read_text(errors="ignore").splitlines()
                except Exception: continue
                for line in lines:
                    s=line.strip()
                    if not s or s.startswith("#") or "=" not in s: continue
                    k,v=s.split("=",1); k=k.strip()
                    if k not in interesting and not k.endswith("DATABASE_URL"): continue
                    safe=sanitize_db_url(v)
                    if not safe: continue
                    key=f"env-db-ref://{p}#{k}"
                    if key in ignored or key in seen: continue
                    seen.add(key)
                    project=bp
                    while project.parent!=root and not (project/".git").exists():
                        project=project.parent
                    items.append({
                        "path":key,"kind":"Project database reference","is_directory":False,
                        "size":None,"modified":None,"coverage":[],
                        "safe_mode":f"{project.name} · {k} → {safe}",
                        "score":86,"engine":"env-reference","project":str(project),
                        "env_file":str(p),"env_key":k,"endpoint":safe,
                        "recommendation":"Reference only",
                        "attention":False
                    })
    return items

def discover():
    cfg=load_config()
    ignored=load_ignored()
    found=discover_homebrew_postgres(cfg,ignored)
    found += discover_homebrew_data(cfg,ignored)
    found += discover_docker_volumes(cfg,ignored)
    found += discover_project_db_refs(cfg,ignored)
    found += discover_project_db_files(cfg,ignored)
    seen={x["path"] for x in found}

    names=["*.sqlite","*.sqlite3","*.db","*.duckdb","*.realm","data.mdb","dump.rdb","appendonly.aof"]
    q=" || ".join([f'kMDItemFSName == "{n}"cd' for n in names])
    candidates=spotlight(q,15)

    # Keep discovery fast and predictable: only evaluate user/project locations
    # and Application Support, never CloudStorage or arbitrary system trees.
    filtered=[]
    for p in candidates:
        raw=str(p)
        if any(raw.startswith(x) for x in SKIP_PREFIXES): continue
        if not any(raw.startswith(x) for x in ALLOWED_PREFIXES): continue
        if any(x in raw for x in NOISY_BITS): continue
        filtered.append(p)

    for p in filtered[:2500]:
        key=str(p)
        if key in seen or key in ignored: continue
        kind=SPECIAL_NAMES.get(p.name.lower()) or FILE_EXTS.get(p.suffix.lower())
        if not kind: continue
        cov=configured_coverage(p,cfg)
        relevance=score(p,kind,cov)
        if relevance<45: continue
        seen.add(key)
        found.append({
            "path":key,"kind":kind,"is_directory":False,"size":None,
            "modified":None,"coverage":cov,
            "safe_mode":"SQLite online backup" if kind=="SQLite" else "review backup method",
            "score":relevance,
            "sqlite_safe_configured":any(x.get("type")=="sqlite" and x.get("mode")=="direct" for x in cov)
        })

    found.sort(key=lambda x:(-x["score"],x["kind"],x["path"].lower()))
    result={
        "version":3,"scanned":iso(),"hostname":os.uname().nodename,
        "count":len(found),
        "uncovered_count":sum(1 for x in found if not x["coverage"]),
        "sqlite_count":sum(1 for x in found if x["kind"]=="SQLite"),
        "postgres_count":sum(1 for x in found if x["kind"]=="PostgreSQL database"),
        "docker_volume_count":sum(1 for x in found if x["kind"].startswith("Docker")),
        "project_reference_count":sum(1 for x in found if x["kind"]=="Project database reference"),
        "items":found
    }
    STATE.mkdir(parents=True,exist_ok=True)
    tmp=OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(result,indent=2)+"\n")
    tmp.replace(OUT)
    return result

def slug(s):
    out="".join(c.lower() if c.isalnum() else "-" for c in s).strip("-")
    while "--" in out: out=out.replace("--","-")
    return out[:48] or "database"

def add_sqlite(path):
    p=Path(path).expanduser()
    if not p.is_file(): raise SystemExit(f"Not a file: {p}")
    if classify_file(p)!="SQLite": raise SystemExit("Selected file is not recognized as SQLite")
    cfg=load_config()
    for j in cfg.get("jobs",[]):
        if j.get("type")=="sqlite" and os.path.abspath(os.path.expanduser(j["source"]))==os.path.abspath(str(p)):
            print(j["name"]); return
    base=slug(p.stem); existing={j.get("name") for j in cfg.get("jobs",[])}
    name="sqlite-"+base; n=2
    while name in existing:
        name=f"sqlite-{base}-{n}"; n+=1
    dest=str((backup_root_from_config(cfg)/"databases"/name))
    cfg.setdefault("jobs",[]).append({
        "name":name,"type":"sqlite","enabled":True,
        "source":str(p),"destination":dest,
        "format":"sqlite.zst","only_if_changed":True,
        "keep_local_snapshots":2
    })
    tmp=CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg,indent=2)+"\n")
    tmp.replace(CONFIG)
    print(name)

def add_postgres(source):
    # source format: postgres://localhost:<port>/<database>
    if not source.startswith("postgres://localhost:") or "/" not in source[len("postgres://localhost:"):]:
        raise SystemExit("Unsupported PostgreSQL source")
    tail=source[len("postgres://localhost:"):]
    port_s,db=tail.split("/",1)
    port=int(port_s)
    if not db: raise SystemExit("Missing database name")
    cfg=load_config()
    for j in cfg.get("jobs",[]):
        if j.get("type")=="postgres" and j.get("source")==source:
            print(j["name"]); return
    base=slug(db)
    existing={j.get("name") for j in cfg.get("jobs",[])}
    name="postgres-"+base; n=2
    while name in existing:
        name=f"postgres-{base}-{n}"; n+=1
    version="17"
    pg_dump=f"/opt/homebrew/opt/postgresql@{version}/bin/pg_dump"
    pg_restore=f"/opt/homebrew/opt/postgresql@{version}/bin/pg_restore"
    if not Path(pg_dump).exists():
        dumps=list(Path("/opt/homebrew/opt").glob("postgresql*/bin/pg_dump"))
        restores=list(Path("/opt/homebrew/opt").glob("postgresql*/bin/pg_restore"))
        if dumps: pg_dump=str(dumps[-1])
        if restores: pg_restore=str(restores[-1])
    dest=str((backup_root_from_config(cfg)/"databases"/name))
    cfg.setdefault("jobs",[]).append({
        "name":name,"type":"postgres","enabled":True,
        "source":source,"database":db,"host":"localhost","port":port,
        "destination":dest,"format":"pg_dump.custom",
        "only_if_changed":False,"keep_local_snapshots":2,
        "pg_dump":pg_dump,"pg_restore":pg_restore
    })
    tmp=CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg,indent=2)+"\n"); tmp.replace(CONFIG)
    print(name)

def add_docker_postgres(source):
    prefix="docker-postgres://"
    if not source.startswith(prefix) or "/" not in source[len(prefix):]:
        raise SystemExit("Unsupported Docker PostgreSQL source")
    tail=source[len(prefix):]
    container,db=tail.split("/",1)
    if not container or not db:
        raise SystemExit("Missing container/database")
    docker="/usr/local/bin/docker" if Path("/usr/local/bin/docker").exists() else "/opt/homebrew/bin/docker"
    if not Path(docker).exists():
        raise SystemExit("Docker CLI not found")
    up=subprocess.run([docker,"exec",container,"sh","-lc",'printf "%s" "$POSTGRES_USER"'],capture_output=True,text=True,timeout=5)
    db_user=up.stdout.strip() or "postgres"
    cfg=load_config()
    for j in cfg.get("jobs",[]):
        if j.get("type")=="docker-postgres" and j.get("source")==source:
            print(j["name"])
            return
    base=slug(container+"-"+db)
    existing={j.get("name") for j in cfg.get("jobs",[])}
    name="docker-postgres-"+base
    n=2
    while name in existing:
        name=f"docker-postgres-{base}-{n}"; n+=1
    restores=list(Path("/opt/homebrew/opt").glob("postgresql*/bin/pg_restore"))
    pg_restore=str(restores[-1]) if restores else "/opt/homebrew/bin/pg_restore"
    dest=str((backup_root_from_config(cfg)/"databases"/name))
    cfg.setdefault("jobs",[]).append({
        "name":name,"type":"docker-postgres","enabled":True,
        "source":source,"container":container,"database":db,"db_user":db_user,
        "destination":dest,"format":"pg_dump.custom",
        "only_if_changed":False,"keep_local_snapshots":2,
        "docker":docker,"pg_restore":pg_restore
    })
    tmp=CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg,indent=2)+"\n")
    tmp.replace(CONFIG)
    print(name)

def ignore_path(path):
    p=str(Path(path).expanduser())
    ignored=load_ignored()
    ignored.add(p)
    save_ignored(ignored)
    print(p)

def main():
    ap=argparse.ArgumentParser()
    sub=ap.add_subparsers(dest="cmd",required=True)
    sub.add_parser("scan")
    a=sub.add_parser("add-sqlite"); a.add_argument("path")
    p=sub.add_parser("add-postgres"); p.add_argument("source")
    dp=sub.add_parser("add-docker-postgres"); dp.add_argument("source")
    i=sub.add_parser("ignore"); i.add_argument("path")
    args=ap.parse_args()
    if args.cmd=="scan":
        r=discover()
        print(json.dumps({k:r[k] for k in ("scanned","count","uncovered_count","sqlite_count")},indent=2))
    elif args.cmd=="add-sqlite":
        add_sqlite(args.path)
    elif args.cmd=="add-postgres":
        add_postgres(args.source)
    elif args.cmd=="add-docker-postgres":
        add_docker_postgres(args.source)
    else:
        ignore_path(args.path)

if __name__=="__main__":
    main()
