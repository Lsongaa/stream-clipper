"""Instagram Reels con instagrapi (API NO oficial: la oficial exige una URL pública del video).
Riesgo de bloqueo/challenge: usá una cuenta secundaria."""
import logging
import os
import subprocess
import threading
from pathlib import Path

from config import ROOT, TMP, tool

log = logging.getLogger("ig")
SESSION = ROOT / "ig_session.json"
_client = None
_lock = threading.Lock()


def _login():
    """Reusa la sesión guardada (menos logins = menos riesgo de bloqueo)."""
    from instagrapi import Client
    from instagrapi.exceptions import LoginRequired
    user, pw = os.environ.get("IG_USER"), os.environ.get("IG_PASS")
    if not user or not pw:
        raise RuntimeError("faltan IG_USER / IG_PASS en .env")
    cl = Client()
    cl.delay_range = [2, 6]
    if SESSION.exists():
        cl.load_settings(SESSION)
        cl.login(user, pw)
        try:
            cl.get_timeline_feed()  # verifica que la sesión siga viva
        except LoginRequired:
            old = cl.get_settings()
            cl.set_settings({})
            cl.set_uuids(old["uuids"])  # mismo "dispositivo"
            cl.login(user, pw)
    else:
        cl.login(user, pw)
    cl.dump_settings(SESSION)
    return cl


def caption(clip):
    tags = [t for t in clip["tags"].split(",") if t][:30]  # Instagram: máx 30 hashtags
    text = f"{clip['title']}\n\n{clip['description']}\n\n" + " ".join("#" + t for t in tags)
    return text.strip()[:2200]


def upload(clip, client=None):
    global _client
    with _lock:
        cl = client or _client or _login()
        thumb = TMP / f"ig_thumb_{clip['id']}.jpg"
        # miniatura con ffmpeg (si no, instagrapi necesita moviepy)
        subprocess.run([tool("ffmpeg"), "-y", "-loglevel", "error", "-ss", "1", "-i", clip["path"],
                        "-frames:v", "1", "-q:v", "3", str(thumb)], timeout=60, check=True,
                       stdin=subprocess.DEVNULL, capture_output=True)
        try:
            m = cl.clip_upload(Path(clip["path"]), caption(clip), thumbnail=thumb)
        except Exception:
            _client = None  # forzar login nuevo la próxima vez
            raise
        finally:
            thumb.unlink(missing_ok=True)
        if client is None:
            _client = cl
        return f"https://instagram.com/reel/{m.code}"
