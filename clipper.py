"""Corta el clip del buffer y lo convierte a vertical 9:16 (1080x1920, fondo desenfocado)."""
import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path

from config import BUFFER, CFG, TMP, tool

log = logging.getLogger("clip")
FMT = "%Y%m%d-%H%M%S"
W, H = 1080, 1920
WRITING_GRACE = 5      # el segmento más nuevo con mtime más reciente que esto se está escribiendo
FFMPEG_TIMEOUT = 600
# fondo: se desenfoca a 1/4 de tamaño y se agranda (12x más rápido que desenfocar a 1080x1920)
FC = ("[0:v]fps=30,split[a][b];"
      f"[a]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,crop={W // 4}:{H // 4},"
      f"boxblur=10:2,scale={W}:{H}[bg];"
      f"[b]scale={W}:-2[fg];"
      "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p[v]")


# --- selección de segmentos (pura, testeable) -----------------------------
def scan_buffer(folder=None):
    """Lista (path, inicio, fin, tamaño) de los .ts del buffer. inicio sale del nombre, fin = mtime."""
    out = []
    for f in (folder or BUFFER).glob("*.ts"):
        try:
            st = f.stat()
            start = datetime.strptime(f.stem, FMT).timestamp()
        except (ValueError, OSError):
            continue
        out.append((f, start, st.st_mtime, st.st_size))
    return sorted(out, key=lambda x: x[1])


def select_segments(segs, t0, t1, now, min_coverage=0.5):
    """Elige los segmentos que cubren [t0, t1], en orden.
    Ignora vacíos y el que se está escribiendo. Devuelve [(path, inicio)] o [] si no alcanza."""
    if not segs:
        return []
    newest = max(s[1] for s in segs)
    chosen = []
    for path, start, end, size in segs:
        if size <= 0:
            continue
        if start == newest and now - end < WRITING_GRACE:
            continue  # todavía abierto por ffmpeg
        if start < t1 and end > t0:
            chosen.append((path, start, end))
    covered = sum(min(e, t1) - max(s, t0) for _, s, e in chosen)
    if not chosen or covered < (t1 - t0) * min_coverage:
        return []
    return [(p, s) for p, s, _ in chosen]


# --- ffmpeg --------------------------------------------------------------
async def run(args, timeout=FFMPEG_TIMEOUT, cwd=None):
    """Corre un subproceso; mata si se pasa del timeout o si la tarea se cancela.
    Devuelve (código, stdout, stderr)."""
    p = await asyncio.create_subprocess_exec(*args, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
                                             stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout)
        return p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    except BaseException:  # timeout o cancelación (Ctrl+C): no dejar ffmpeg colgado
        if p.returncode is None:
            p.kill()
            await p.wait()
        raise


async def probe(path):
    """Devuelve dict con width, height, duration (o None si el archivo no es válido)."""
    code, out, _ = await run([tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=width,height:format=duration",
                              "-of", "json", str(path)], timeout=60)
    if code != 0:
        return None
    try:
        d = json.loads(out)
        s = d["streams"][0]
        return {"width": int(s["width"]), "height": int(s["height"]),
                "duration": float(d.get("format", {}).get("duration") or 0)}
    except (ValueError, KeyError, IndexError):
        return None


def _concat_line(p):
    # escape de comillas simples para el demuxer concat
    return "file '" + Path(p).resolve().as_posix().replace("'", "'\\''") + "'\n"


async def render_vertical(segs, offset, duration, out):
    lst = TMP / f"{out.stem}.txt"
    lst.write_text("".join(_concat_line(s) for s in segs), encoding="utf-8")
    try:
        code, _, err = await run([
            tool("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
            "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err",
            "-f", "concat", "-safe", "0", "-i", str(lst),
            "-ss", f"{offset:.2f}", "-t", f"{duration:.2f}",
            "-filter_complex", FC, "-map", "[v]", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-r", "30",
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart", str(out)])
    finally:
        lst.unlink(missing_ok=True)
    if code != 0:
        log.warning("ffmpeg falló: %s", err.strip()[-500:])
    return code == 0


async def make_clip(t_event, now=None):
    """Corta [t_event - pre, t_event + post] del buffer. Devuelve Path del mp4 o None."""
    t0, t1 = t_event - CFG["clip_pre_seconds"], t_event + CFG["clip_post_seconds"]
    segs = select_segments(scan_buffer(), t0, t1, now or time.time())
    if not segs:
        log.warning("no hay segmentos suficientes en el buffer para %s",
                    time.strftime("%H:%M:%S", time.localtime(t_event)))
        return None
    out = TMP / f"clip_{time.strftime('%Y%m%d_%H%M%S', time.localtime(t_event))}.mp4"
    ok = await render_vertical([p for p, _ in segs], max(0.0, t0 - segs[0][1]), t1 - t0, out)
    if not ok:
        # reintento sin segmentos corruptos
        good = [(p, s) for p, s in segs if await probe(p)]
        if good and len(good) < len(segs):
            log.info("reintentando sin %d segmentos corruptos", len(segs) - len(good))
            ok = await render_vertical([p for p, _ in good], max(0.0, t0 - good[0][1]), t1 - t0, out)
    info = await probe(out) if ok and out.exists() else None
    if not info or (info["width"], info["height"]) != (W, H) or info["duration"] < 1:
        log.warning("clip inválido: %s", info)
        out.unlink(missing_ok=True)
        return None
    return out


async def burn_subtitles(video, ass):
    """Quema subtítulos .ass. Corre ffmpeg desde la carpeta del .ass para evitar
    los problemas de escape de rutas de Windows (C:\\...) en el filtro subtitles."""
    out = video.with_name(video.stem + "_sub.mp4")
    code, _, err = await run([tool("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
                              "-i", str(video.resolve()), "-vf", f"subtitles={ass.name}",
                              "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                              "-c:a", "copy", "-movflags", "+faststart", str(out.resolve())],
                             cwd=str(ass.parent))
    if code != 0 or not out.exists():
        log.warning("no se pudieron quemar subtítulos: %s", err.strip()[-300:])
        out.unlink(missing_ok=True)
        return video
    video.unlink(missing_ok=True)
    return out.replace(video)

