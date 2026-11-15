"""Panel web, pipeline de main, supervisión de tareas, grabador real y logs sin secretos."""
import asyncio
import logging
import os
import sys
import time

import pytest

import main
import panel
import recorder
import subtitles
from config import CFG, RedactingFormatter
from conftest import needs_ffmpeg

PORT = CFG["panel"]["port"]
HOST = {"Host": f"127.0.0.1:{PORT}"}


@pytest.fixture
def web(fresh_db, tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"0123456789" * 100)
    cid = fresh_db.add_clip(f, "gracioso", 8, "Título", "d", "x,y")
    fresh_db.add_clip(tmp_path / "b.mp4", "epico", 5, "Otro", "d", "z")
    return panel.app.test_client(), cid, f


def test_panel_lista_y_filtra(web):
    c, cid, _ = web
    r = c.get("/", headers=HOST)
    assert r.status_code == 200 and "Título" in r.text and "Otro" in r.text
    r = c.get("/?cat=gracioso", headers=HOST)
    assert "Título" in r.text and "Otro" not in r.text


def test_panel_video_con_range(web):
    c, cid, _ = web
    r = c.get(f"/video/{cid}", headers={**HOST, "Range": "bytes=0-9"})
    assert r.status_code == 206 and r.data == b"0123456789"
    assert c.get("/video/999", headers=HOST).status_code == 404


def test_panel_aprobar_y_descartar(web, fresh_db):
    c, cid, f = web
    h = {**HOST, "X-Clipper": "1"}
    assert c.post(f"/api/clips/{cid}/approve", headers=h).status_code == 200
    assert fresh_db.get_clip(cid)["status"] == "approved"
    assert c.post(f"/api/clips/{cid}/discard", headers=h).status_code == 200
    assert fresh_db.get_clip(cid)["status"] == "discarded" and not f.exists()
    assert "Título" not in c.get("/", headers=HOST).text


def test_panel_protecciones(web):
    c, cid, _ = web
    assert c.post(f"/api/clips/{cid}/approve", headers=HOST).status_code == 403          # sin header: CSRF
    assert c.get("/", headers={"Host": "evil.com"}).status_code == 403                  # DNS rebinding
    assert c.post(f"/api/clips/{cid}/hackear", headers={**HOST, "X-Clipper": "1"}).status_code == 400


def test_panel_servidor_real_arranca_y_para(fresh_db):
    import urllib.request
    p = panel.Panel(port=0).start()
    port = p.srv.server_port
    req = urllib.request.Request(f"http://127.0.0.1:{port}/")
    req.add_header("Host", f"127.0.0.1:{PORT}")
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
    p.stop()
    p.th.join(5)
    assert not p.th.is_alive()


# --- main ----------------------------------------------------------------
def test_comando_manual_y_cooldown(cfg):
    cfg("detector", "manual_users", [])
    async def run():
        p = main.Pipeline()
        p.on_msg("twitch", "ana", "!CLIP ")
        p.on_msg("kick", "beto", "!clip")          # dentro del cooldown manual
        p.on_msg("twitch", "ana", "hola")
        return p.events.qsize()
    assert asyncio.run(run()) == 1


def test_comando_manual_solo_usuarios_permitidos(cfg):
    cfg("detector", "manual_users", ["ElMod"])
    async def run():
        p = main.Pipeline()
        p.on_msg("twitch", "random", "!clip")
        n1 = p.events.qsize()
        p.on_msg("twitch", "elmod", "!clip")
        return n1, p.events.qsize()
    assert asyncio.run(run()) == (0, 1)


def test_cola_de_eventos_acotada():
    async def run():
        p = main.Pipeline()
        for i in range(100):
            p.push_event(time.time(), "x")
        return p.events.qsize()
    assert asyncio.run(run()) == 20


def test_supervise_reinicia_y_no_tira_a_las_demas(monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(main.asyncio, "sleep", lambda s: real_sleep(0.01))
    runs = {"mala": 0, "buena": 0}

    async def mala():
        runs["mala"] += 1
        raise ValueError("falla")

    async def buena():
        while True:
            runs["buena"] += 1
            await real_sleep(0.01)

    async def scenario():
        ts = [asyncio.create_task(main.supervise("mala", mala)),
              asyncio.create_task(main.supervise("buena", buena))]
        await real_sleep(0.3)
        for t in ts:
            t.cancel()
        res = await asyncio.gather(*ts, return_exceptions=True)
        return res
    res = asyncio.run(scenario())
    assert runs["mala"] >= 3 and runs["buena"] >= 5
    assert all(isinstance(r, asyncio.CancelledError) for r in res)


def test_una_sola_instancia(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "ROOT", tmp_path)
    first = main.single_instance()
    assert first is not None
    assert main.single_instance() is None        # segunda instancia: rechazada
    first.close()
    again = main.single_instance()
    assert again is not None
    again.close()


# --- grabador con un "streamlink" falso que emite MPEG-TS -----------------
@needs_ffmpeg
def test_grabador_segmenta_y_mata_subprocesos_al_cancelar(tmp_path, monkeypatch, cfg):
    cfg(None, "segment_seconds", 2)
    monkeypatch.setattr(recorder, "BUFFER", tmp_path)
    # ffmpeg en vivo (-re) escribiendo MPEG-TS a stdout, como hace streamlink -O
    fake = ["ffmpeg", "-loglevel", "error", "-re", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30",
            "-f", "lavfi", "-i", "sine", "-c:v", "libx264", "-preset", "ultrafast", "-g", "30",
            "-c:a", "aac", "-f", "mpegts", "pipe:1"]
    monkeypatch.setattr(recorder, "streamlink_cmd", lambda: fake)
    procs = []
    real_exec = asyncio.create_subprocess_exec

    async def spy(*a, **k):
        p = await real_exec(*a, **k)
        procs.append(p)
        return p
    monkeypatch.setattr(recorder.asyncio, "create_subprocess_exec", spy)

    async def scenario():
        t = asyncio.create_task(recorder.record_once())
        await asyncio.sleep(8)
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
    asyncio.run(scenario())
    segs = sorted(tmp_path.glob("*.ts"))
    assert len(segs) >= 2 and all(s.stat().st_size > 0 for s in segs[:-1])
    assert all(p.returncode is not None for p in procs)     # nada quedó vivo


def test_grabador_stream_offline_vuelve(tmp_path, monkeypatch):
    """Si streamlink sale enseguida (offline), record_once vuelve sin colgarse."""
    monkeypatch.setattr(recorder, "BUFFER", tmp_path)
    monkeypatch.setattr(recorder, "streamlink_cmd", lambda: [sys.executable, "-c", "import sys; sys.exit(1)"])
    if not recorder.tool("ffmpeg"):
        monkeypatch.setattr(recorder, "tool", lambda n: sys.executable)
    t0 = time.time()
    asyncio.run(asyncio.wait_for(recorder.record_once(), 40))
    assert time.time() - t0 < 30


def test_limpieza_borra_viejos(tmp_path, monkeypatch, cfg):
    cfg(None, "buffer_minutes", 6)
    monkeypatch.setattr(recorder, "BUFFER", tmp_path)
    old, new = tmp_path / "a.ts", tmp_path / "b.ts"
    for f in (old, new):
        f.write_bytes(b"x")
    os.utime(old, (time.time() - 400,) * 2)
    assert recorder.cleanup_once() == 1
    assert not old.exists() and new.exists()


# --- subtítulos y logs ---------------------------------------------------
def test_subtitulos_agrupa_y_escapa():
    words = [(0, .3, " hola"), (.3, .6, " che"), (.6, .9, " que"), (.9, 1.2, " onda"), (1.2, 1.5, " loco"),
             (4.0, 4.3, " {\\b1}raro")]
    chunks = subtitles.group_words(words)
    assert [c[2] for c in chunks] == ["hola che que onda", "loco", "b1raro"]
    ass = subtitles.build_ass(words)
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert "Dialogue: 0,0:00:00.00,0:00:01.20,Default,,0,0,0,,HOLA CHE QUE ONDA" in ass
    assert subtitles.build_ass([]) is None


def test_logs_no_muestran_secretos(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-supersecreto123")
    fmt = RedactingFormatter("%(message)s")
    rec = logging.LogRecord("x", logging.ERROR, "", 0,
                            'key=sk-ant-supersecreto123 {"access_token": "act.abc", "refresh_token":"rft.xyz"} '
                            "Authorization: Bearer tok123", None, None)
    out = fmt.format(rec)
    for s in ("supersecreto", "act.abc", "rft.xyz", "tok123"):
        assert s not in out
