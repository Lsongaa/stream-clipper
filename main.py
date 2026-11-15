"""Orquestador 24/7: grabación, chat, detección, corte, análisis, publicación y panel.
Cada tarea corre supervisada: si una falla se reinicia sola y no tira a las demás."""
import asyncio
import logging
import os
import shutil
import sys
import time

from config import CFG, CLIPS, ROOT, TMP, setup_logging, tool

log = logging.getLogger("main")
EXIT_CONFIG = 2           # run.bat no relanza con estos códigos
EXIT_ALREADY_RUNNING = 3


class Pipeline:
    def __init__(self):
        from detector import Detector
        self.det = Detector()
        self.events = asyncio.Queue(maxsize=20)   # momentos a cortar (t, motivo)
        self.to_analyze = asyncio.Queue()         # clips cortados esperando Whisper + Claude
        self.last_manual = float("-inf")
        self.last_event = float("-inf")

    def push_event(self, t, why):
        try:
            self.events.put_nowait((t, why))
        except asyncio.QueueFull:
            log.warning("cola de eventos llena: se ignora %s", why)

    def on_msg(self, src, user, text):
        try:
            self.det.add(user, text)
            d = CFG["detector"]
            if text.strip().lower() == d["manual_command"]:
                allowed = not d["manual_users"] or user.lower() in [u.lower() for u in d["manual_users"]]
                if allowed and time.time() - self.last_manual > d["manual_cooldown_seconds"]:
                    self.last_manual = time.time()
                    self.push_event(time.time(), f"{d['manual_command']} de {user} ({src})")
        except Exception:
            log.exception("on_msg")

    async def watch(self):
        while True:
            score = self.det.check()
            if score is not None:
                self.push_event(time.time(), f"pico de chat ({score})")
            await asyncio.sleep(2)

    async def cutter(self):
        import clipper
        while True:
            t, why = await self.events.get()
            if t - self.last_event < CFG["merge_seconds"]:
                log.info("evento '%s' muy cerca del anterior: se une al mismo clip", why)
                continue
            self.last_event = t
            log.info("clip pedido: %s", why)
            # esperar a que se graben los segundos posteriores al evento
            wait = t + CFG["clip_post_seconds"] + CFG["segment_seconds"] + 5 - time.time()
            await asyncio.sleep(max(0, wait))
            try:
                path = await clipper.make_clip(t)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("error cortando clip")
                continue
            if path:
                await self.to_analyze.put((path, t))

    async def analyzer(self):
        import classifier
        import clipper
        import db
        import subtitles
        while True:
            path, t = await self.to_analyze.get()
            try:
                chat = self.det.sample(t - CFG["clip_pre_seconds"], t + CFG["clip_post_seconds"])
                info, words, transcript = await asyncio.to_thread(classifier.analyze, path, chat)
                if CFG["subtitles"]:
                    ass = subtitles.build_ass(words)
                    if ass:
                        ass_path = path.with_suffix(".ass")
                        ass_path.write_text(ass, encoding="utf-8")
                        path = await clipper.burn_subtitles(path, ass_path)
                        ass_path.unlink(missing_ok=True)
                dest = CLIPS / info["category"]
                dest.mkdir(exist_ok=True)
                final = dest / path.name
                shutil.move(str(path), str(final))
                db.add_clip(final, info["category"], info["score"], info["title"],
                            info["description"], ",".join(info["hashtags"]), transcript)
                log.info("clip guardado [%s %s/10] %s", info["category"], info["score"], info["title"])
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("error analizando %s", path)


async def supervise(name, fn):
    """Corre fn() para siempre; si falla o termina, la reinicia con espera creciente."""
    delay = 5
    while True:
        started = time.monotonic()
        try:
            await fn()
            log.warning("%s terminó sola; reiniciando", name)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s falló; reinicio en %ss", name, delay)
        delay = 5 if time.monotonic() - started > 300 else min(delay * 2, 300)
        await asyncio.sleep(delay)


def single_instance():
    """Lock de archivo: evita dos instancias (p. ej. Programador de tareas + manual)."""
    f = open(ROOT / "clipper.lock", "a+")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def preflight():
    missing = [t for t in ("ffmpeg", "ffprobe") if not tool(t)]
    if missing:
        log.error("No encuentro %s: instalá ffmpeg (winget install Gyan.FFmpeg) "
                  "o poné ffmpeg_path en config.yaml", missing)
        sys.exit(EXIT_CONFIG)
    ch = CFG["twitch_channel"] if CFG["record_from"] == "twitch" else CFG["kick_channel"]
    if not ch:
        log.error("record_from=%s pero ese canal está vacío en config.yaml", CFG["record_from"])
        sys.exit(EXIT_CONFIG)
    for f in TMP.glob("*"):  # restos de una ejecución cortada
        try:
            f.unlink()
        except OSError:
            pass


async def main():
    import chat
    import db
    import publisher
    import recorder
    n = db.recover_interrupted()
    if n:
        log.warning("%d subidas quedaron a medias en la ejecución anterior: marcadas 'interrupted' "
                    "(no se reintentan para no duplicar; revisalas en la plataforma)", n)
    p = Pipeline()
    jobs = {"grabador": recorder.run, "limpieza": recorder.cleanup, "detector": p.watch,
            "cortador": p.cutter, "analizador": p.analyzer, "publicador": publisher.run}
    if CFG["twitch_channel"]:
        jobs["chat-twitch"] = lambda: chat.twitch(CFG["twitch_channel"], p.on_msg)
    if CFG["kick_channel"]:
        jobs["chat-kick"] = lambda: chat.kick(CFG["kick_channel"], p.on_msg, CFG["kick_chatroom_id"])
    panel = None
    if CFG["panel"]["enabled"]:
        from panel import Panel
        try:
            panel = Panel().start()
        except OSError as e:
            log.error("no se pudo abrir el panel en el puerto %s: %s", CFG["panel"]["port"], e)
    tasks = [asyncio.create_task(supervise(name, fn), name=name) for name, fn in jobs.items()]
    log.info("clipper iniciado (%s). Ctrl+C para salir.", ", ".join(jobs))
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)  # cada tarea mata sus subprocesos
        if panel:
            panel.stop()
        log.info("tareas detenidas (si había una subida o transcripción en curso, se espera a que termine)")


if __name__ == "__main__":
    setup_logging()
    lock = single_instance()
    if not lock:
        log.error("ya hay otra instancia corriendo")
        sys.exit(EXIT_ALREADY_RUNNING)
    preflight()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    log.info("clipper detenido")
