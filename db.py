"""SQLite: clips y subidas. Seguro entre hilos (un lock alrededor de cada operación)."""
import sqlite3
import threading
import time

from config import ROOT

_lock = threading.RLock()
con = None

SCHEMA = """
create table if not exists clips(id integer primary key, path text, category text, score int,
  title text, description text, tags text, created real);
create table if not exists uploads(id integer primary key, clip_id int, platform text,
  status text, info text, ts real);
create index if not exists ix_uploads on uploads(clip_id, platform, status);
"""
# estados de clip: new | approved | discarded
# estados de subida: uploading | ok | error | interrupted (se cortó el programa a mitad: no se reintenta)
DONE = ("ok", "uploading", "interrupted")


def init(path=ROOT / "clips.db"):
    global con
    with _lock:
        if con:
            con.close()
        con = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("pragma journal_mode=wal")
        con.executescript(SCHEMA)
        cols = {r["name"] for r in con.execute("pragma table_info(clips)")}
        if "status" not in cols:  # migración desde la versión anterior
            con.execute("alter table clips add column status text not null default 'new'")
        if "transcript" not in cols:
            con.execute("alter table clips add column transcript text default ''")
        con.commit()


def _all(sql, args=()):
    with _lock:
        return [dict(r) for r in con.execute(sql, args).fetchall()]


def _one(sql, args=()):
    rows = _all(sql, args)
    return rows[0] if rows else None


def _exec(sql, args=()):
    with _lock:
        cur = con.execute(sql, args)
        con.commit()
        return cur.lastrowid


def add_clip(path, category, score, title, description, tags, transcript="", now=None):
    return _exec("insert into clips(path,category,score,title,description,tags,created,status,transcript)"
                 " values(?,?,?,?,?,?,?,'new',?)",
                 (str(path), category, score, title, description, tags, now or time.time(), transcript))


def get_clip(clip_id):
    return _one("select * from clips where id=?", (clip_id,))


def list_clips(category=None, include_discarded=False):
    sql, args = "select * from clips where 1=1", []
    if category:
        sql += " and category=?"
        args.append(category)
    if not include_discarded:
        sql += " and status!='discarded'"
    return _all(sql + " order by created desc", args)


def set_status(clip_id, status):
    _exec("update clips set status=? where id=?", (status, clip_id))


def next_pending(platform, min_score, require_approval=False, max_attempts=3):
    """Mejor clip sin subir a esa plataforma. Con aprobación manual solo los aprobados
    (y en ese caso no se mira min_score: lo decidiste vos)."""
    cond = "c.status='approved'" if require_approval else "c.status!='discarded' and c.score>=:min"
    return _one(f"""select * from clips c where {cond}
      and not exists(select 1 from uploads u where u.clip_id=c.id and u.platform=:p
                     and u.status in ('ok','uploading','interrupted'))
      and (select count(*) from uploads u where u.clip_id=c.id and u.platform=:p and u.status='error') < :max
      order by c.score desc, c.created asc limit 1""",
                {"min": min_score, "p": platform, "max": max_attempts})


def start_upload(clip_id, platform, now=None):
    return _exec("insert into uploads(clip_id,platform,status,info,ts) values(?,?,'uploading','',?)",
                 (clip_id, platform, now or time.time()))


def finish_upload(upload_id, status, info="", now=None):
    _exec("update uploads set status=?, info=?, ts=? where id=?",
          (status, str(info)[:1000], now or time.time(), upload_id))


def recover_interrupted():
    """Al arrancar: subidas que quedaron 'uploading' => 'interrupted' (pudo haberse publicado)."""
    with _lock:
        n = con.execute("update uploads set status='interrupted' where status='uploading'").rowcount
        con.commit()
    return n


def _midnight(now):
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def uploads_today(platform, now=None):
    now = now or time.time()
    return _one("select count(*) n from uploads where platform=? and status in ('ok','uploading','interrupted')"
                " and ts>=?", (platform, _midnight(now)))["n"]


def last_upload(platform, statuses=DONE):
    marks = ",".join("?" * len(statuses))
    return _one(f"select coalesce(max(ts),0) t from uploads where platform=? and status in ({marks})",
                (platform, *statuses))["t"]


def uploads_for(clip_id):
    return _all("select platform,status,info,ts from uploads where clip_id=? order by ts", (clip_id,))


init()
