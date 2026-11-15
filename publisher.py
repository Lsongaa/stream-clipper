"""Cola de publicación: mejor score primero, tope diario, separación mínima, reintentos limitados."""
import asyncio
import importlib
import logging
import time
from pathlib import Path

import db
from config import CFG

log = logging.getLogger("pub")


def _uploader(platform):
    return importlib.import_module(f"uploaders.{platform}").upload


def can_upload(platform, now=None):
    """None si se puede subir ya; si no, el motivo."""
    u, now = CFG["upload"], now or time.time()
    if db.uploads_today(platform, now) >= u["max_per_day_per_platform"]:
        return "tope diario"
    if now - db.last_upload(platform) < u["min_gap_minutes"] * 60:
        return "separación mínima"
    if now - db.last_upload(platform, ("error",)) < u["retry_minutes"] * 60:
        return "espera tras error"
    return None


def _do_upload(platform, clip, upload_id, uploader, now=None):
    """Corre en un hilo y registra el resultado él mismo: si el programa se corta a mitad,
    la fila queda 'uploading' y al reiniciar pasa a 'interrupted' (nunca se repite)."""
    try:
        if not Path(clip["path"]).exists():
            raise FileNotFoundError(f"no existe {clip['path']}")
        url = uploader(clip)
        db.finish_upload(upload_id, "ok", url, now)
        log.info("subido a %s: %s", platform, url)
        return True
    except Exception as e:
        db.finish_upload(upload_id, "error", f"{type(e).__name__}: {e}", now)
        log.error("falló subida a %s (clip %s): %s", platform, clip["id"], e)
        return False


async def tick(platform, uploader=None, now=None):
    """Intenta una subida a una plataforma. Devuelve el id del clip intentado o None."""
    u = CFG["upload"]
    if can_upload(platform, now):
        return None
    clip = db.next_pending(platform, u["min_score"], u["require_approval"], u["max_attempts"])
    if not clip:
        return None
    upload_id = db.start_upload(clip["id"], platform, now)
    await asyncio.to_thread(_do_upload, platform, clip, upload_id,
                            uploader or _uploader(platform), now)
    return clip["id"]


async def run():
    while True:
        for p in CFG["upload"]["platforms"]:
            try:
                await tick(p)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("publisher %s", p)
        await asyncio.sleep(60)
