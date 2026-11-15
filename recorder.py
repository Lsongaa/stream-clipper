"""Graba el stream en segmentos .ts de N segundos (buffer circular) con streamlink | ffmpeg."""
import asyncio
import importlib.util
import logging
import os
import sys
import time

from config import BUFFER, CFG, tool

log = logging.getLogger("rec")
STALL_SECONDS = 90   # sin segmentos nuevos por este tiempo => reiniciar la grabación


def stream_url():
    if CFG["record_from"] == "twitch":
        return f"https://twitch.tv/{CFG['twitch_channel']}"
    return f"https://kick.com/{CFG['kick_channel']}"


def streamlink_cmd():
    """Comando completo de streamlink que escribe el stream a stdout.
    Corre como módulo del mismo Python: así kill() mata el proceso real
    (el .exe de pip es un lanzador que deja un python hijo)."""
    if importlib.util.find_spec("streamlink_cli"):
        base = [sys.executable, "-m", "streamlink_cli"]
    else:
        base = [tool("streamlink")]
    return base + ["-O", "--loglevel", "error", "--retry-open", "2", "--stream-timeout", "30",
                   stream_url(), "best"]


def _newest_mtime():
    try:
        return max((f.stat().st_mtime for f in BUFFER.glob("*.ts")), default=0.0)
    except OSError:
        return 0.0


async def _pump(stream, name):
    """Pasa el stderr de un subproceso al log (y evita que se llene el buffer del pipe)."""
    async for line in stream:
        txt = line.decode("utf-8", "replace").strip()
        if txt:  # "no playable streams" = offline: normal, no ensuciar el log
            lvl = logging.DEBUG if "no playable streams" in txt.lower() else logging.INFO
            log.log(lvl, "%s: %s", name, txt[:300])


async def _kill(p):
    if p and p.returncode is None:
        try:
            p.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(p.wait(), 10)
        except asyncio.TimeoutError:
            log.error("proceso %s no terminó", p.pid)


async def record_once():
    """Una sesión de grabación. Vuelve cuando el stream termina, se cuelga o falla."""
    seg = CFG["segment_seconds"]
    r, w = os.pipe()
    sl = ff = None
    pumps = []
    try:
        sl = await asyncio.create_subprocess_exec(
            *streamlink_cmd(), stdin=asyncio.subprocess.DEVNULL, stdout=w, stderr=asyncio.subprocess.PIPE)
        os.close(w)
        w = None
        ff = await asyncio.create_subprocess_exec(
            tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-fflags", "+genpts+discardcorrupt",
            "-i", "pipe:0", "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy",
            "-f", "segment", "-segment_time", str(seg), "-segment_format", "mpegts",
            "-reset_timestamps", "1", "-strftime", "1", str(BUFFER / "%Y%m%d-%H%M%S.ts"),
            stdin=r, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        os.close(r)
        r = None
        pumps = [asyncio.create_task(_pump(sl.stderr, "streamlink")),
                 asyncio.create_task(_pump(ff.stderr, "ffmpeg"))]
        started = time.time()
        while True:
            await asyncio.sleep(5)
            if sl.returncode is not None:
                log.debug("streamlink terminó (código %s)", sl.returncode)
                try:  # ffmpeg recibe EOF y cierra el último segmento solo
                    await asyncio.wait_for(ff.wait(), 15)
                except asyncio.TimeoutError:
                    pass
                return
            if ff.returncode is not None:
                log.warning("ffmpeg terminó (código %s)", ff.returncode)
                return
            last = max(_newest_mtime(), started)
            if time.time() - last > max(STALL_SECONDS, 4 * seg):
                log.warning("grabación colgada (sin segmentos nuevos), reiniciando")
                return
    finally:
        for fd in (r, w):
            if fd is not None:
                os.close(fd)
        await _kill(sl)
        await _kill(ff)
        for t in pumps:
            t.cancel()


async def run():
    offline = False
    while True:
        if not offline:
            log.info("conectando a %s", stream_url())
        t0 = time.time()
        await record_once()
        if time.time() - t0 < 60:  # se cortó enseguida: offline (o error), reintentar en 60 s
            if not offline:
                log.info("stream offline o sin acceso; reintento cada 60 s en silencio")
            offline = True
            await asyncio.sleep(60)
        else:
            log.info("la grabación se cortó tras %.0f s; reconectando", time.time() - t0)
            offline = False
            await asyncio.sleep(5)


def cleanup_once(now=None):
    """Borra segmentos más viejos que buffer_minutes. Devuelve cuántos borró."""
    cut = (now or time.time()) - CFG["buffer_minutes"] * 60
    n = 0
    for f in BUFFER.glob("*.ts"):
        try:
            if f.stat().st_mtime < cut:
                f.unlink()
                n += 1
        except OSError:  # en uso (Windows) o ya borrado: se reintenta en la próxima vuelta
            pass
    return n


async def cleanup():
    while True:
        cleanup_once()
        await asyncio.sleep(30)
