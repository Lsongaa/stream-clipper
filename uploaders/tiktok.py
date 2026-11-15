"""TikTok Content Posting API (Direct Post, FILE_UPLOAD).
Flujo oficial: creator_info/query -> video/init -> PUT por partes -> status/fetch.
El token se crea una vez con: python auth_tiktok.py"""
import json
import os
import threading
import time
from pathlib import Path

import requests

from config import CFG, ROOT

TOKEN = ROOT / "tiktok_token.json"
API = "https://open.tiktokapis.com"
MB = 1024 * 1024
MAX_CHUNK = 64 * MB          # doc: cada parte entre 5 MB y 64 MB (la última hasta 128 MB)
_lock = threading.Lock()


# --- token ---------------------------------------------------------------
def save_token(data):
    data = dict(data, obtained_at=time.time())
    tmp = TOKEN.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, TOKEN)  # atómico: el refresh_token rota y el viejo deja de servir


def access_token():
    """Reusa el access_token mientras sea válido (dura ~2 h); si no, lo renueva."""
    with _lock:
        if not TOKEN.exists():
            raise RuntimeError("falta tiktok_token.json: corré python auth_tiktok.py")
        t = json.loads(TOKEN.read_text(encoding="utf-8"))
        if time.time() < t.get("obtained_at", 0) + int(t.get("expires_in", 0)) - 300:
            return t["access_token"]
        r = requests.post(f"{API}/v2/oauth/token/", timeout=30, data={
            "client_key": os.environ["TIKTOK_CLIENT_KEY"],
            "client_secret": os.environ["TIKTOK_CLIENT_SECRET"],
            "grant_type": "refresh_token", "refresh_token": t["refresh_token"]}).json()
        if "access_token" not in r:
            raise RuntimeError(f"refresh de TikTok falló ({r.get('error')}: {r.get('error_description')}); "
                               "corré python auth_tiktok.py")
        save_token(r)
        return r["access_token"]


def _api(path, token, body=None):
    r = requests.post(f"{API}{path}", timeout=30, json=body or {},
                      headers={"Authorization": f"Bearer {token}",
                               "Content-Type": "application/json; charset=UTF-8"})
    try:
        d = r.json()
    except ValueError:
        raise RuntimeError(f"TikTok {path}: HTTP {r.status_code}")
    err = d.get("error") or {}
    if err.get("code") != "ok":
        raise RuntimeError(f"TikTok {path}: {err.get('code')} - {err.get('message')}")
    return d.get("data") or {}


# --- partes --------------------------------------------------------------
def plan_chunks(size):
    """Devuelve (chunk_size, [(inicio, fin_inclusivo)]) según la doc:
    total_chunk_count = floor(size / chunk_size) y la última parte se lleva el resto."""
    if size <= 0:
        raise ValueError("archivo vacío")
    chunk = size if size <= MAX_CHUNK else MAX_CHUNK
    count = max(1, size // chunk)
    ranges = [(i * chunk, (i + 1) * chunk - 1) for i in range(count)]
    ranges[-1] = (ranges[-1][0], size - 1)
    return chunk, ranges


def caption(clip):
    tags = " ".join("#" + t for t in clip["tags"].split(",") if t)
    text = f"{clip['title']}\n\n{clip['description']}\n\n{tags}".strip()
    while len(text.encode("utf-16-le")) // 2 > 2200:  # doc: máx 2200 runas UTF-16
        text = text[:-10]
    return text


def upload(clip, status_wait=180):
    path = Path(clip["path"])
    size = path.stat().st_size
    token = access_token()
    info = _api("/v2/post/publish/creator_info/query/", token)
    privacy = CFG["upload"]["tiktok_privacy"]
    options = info.get("privacy_level_options") or []
    if privacy not in options:
        raise RuntimeError(f"tiktok_privacy={privacy} no permitido para esta cuenta; opciones: {options}")
    chunk, ranges = plan_chunks(size)
    init = _api("/v2/post/publish/video/init/", token, {
        "post_info": {
            "title": caption(clip),
            "privacy_level": privacy,
            "disable_duet": bool(info.get("duet_disabled")),
            "disable_stitch": bool(info.get("stitch_disabled")),
            "disable_comment": bool(info.get("comment_disabled")),
            "video_cover_timestamp_ms": 1000,
            "brand_content_toggle": False,
            "brand_organic_toggle": False,
        },
        "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk,
                        "total_chunk_count": len(ranges)},
    })
    publish_id, upload_url = init["publish_id"], init["upload_url"]
    with open(path, "rb") as f:
        for a, b in ranges:
            f.seek(a)
            r = requests.put(upload_url, data=f.read(b - a + 1), timeout=300, headers={
                "Content-Type": "video/mp4", "Content-Length": str(b - a + 1),
                "Content-Range": f"bytes {a}-{b}/{size}"})
            if r.status_code not in (200, 201, 206):
                raise RuntimeError(f"TikTok PUT parte {a}-{b}: HTTP {r.status_code}")
    # esperar el resultado de la publicación
    deadline = time.time() + status_wait
    status = "PROCESSING_UPLOAD"
    while time.time() < deadline:
        time.sleep(5)
        st = _api("/v2/post/publish/status/fetch/", token, {"publish_id": publish_id})
        status = st.get("status", status)
        if status == "PUBLISH_COMPLETE":
            break
        if status == "FAILED":
            raise RuntimeError(f"TikTok rechazó el video: {st.get('fail_reason')}")
    return f"tiktok publish_id={publish_id} ({status}, {privacy})"
