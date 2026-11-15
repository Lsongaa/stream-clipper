import asyncio
import time

import pytest

import publisher

DAY0 = time.mktime((2026, 11, 13, 10, 0, 0, 0, 0, -1))   # 10:00 hora local
H = 3600


@pytest.fixture
def pub(fresh_db, cfg, tmp_path):
    cfg("upload", "max_per_day_per_platform", 3)
    cfg("upload", "min_gap_minutes", 90)
    cfg("upload", "retry_minutes", 15)
    cfg("upload", "max_attempts", 3)
    cfg("upload", "min_score", 6)
    cfg("upload", "require_approval", False)

    def add(score, created=DAY0 - H, status="new"):
        f = tmp_path / f"c{score}_{created}.mp4"
        f.write_bytes(b"video")
        cid = fresh_db.add_clip(f, "gracioso", score, f"t{score}", "d", "a,b", now=created)
        if status != "new":
            fresh_db.set_status(cid, status)
        return cid
    return add


class Up:
    """Uploader falso que registra las llamadas y puede fallar."""

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, clip):
        self.calls.append(clip["id"])
        if self.fail:
            raise RuntimeError("boom")
        return f"url/{clip['id']}"


def tick(up, now, p="youtube"):
    return asyncio.run(publisher.tick(p, uploader=up, now=now))


def test_orden_por_score_y_antiguedad(pub):
    a = pub(7, created=DAY0 - 3 * H)
    b = pub(9, created=DAY0 - 2 * H)
    c = pub(7, created=DAY0 - 4 * H)
    pub(3)                                         # bajo min_score: nunca
    up = Up()
    for i in range(5):
        tick(up, DAY0 + i * 2 * H)
    assert up.calls == [b, c, a]                   # 9 primero; empate de 7 => el más viejo


def test_no_repite_lo_ya_subido(pub):
    pub(8)
    up = Up()
    tick(up, DAY0)
    tick(up, DAY0 + 3 * H)
    assert len(up.calls) == 1
    # pero sí lo sube a otra plataforma
    tick(up, DAY0, p="tiktok")
    assert len(up.calls) == 2


def test_tope_diario_y_reinicio_a_medianoche(pub, cfg):
    cfg("upload", "min_gap_minutes", 0)
    for s in range(6, 10):
        pub(s)
    up = Up()
    for i in range(6):
        tick(up, DAY0 + i * 60)
    assert len(up.calls) == 3                       # tope 3 por día
    tick(up, DAY0 + 15 * H)                         # 01:00 del día siguiente
    assert len(up.calls) == 4


def test_separacion_minima(pub):
    pub(8)
    pub(7)
    up = Up()
    tick(up, DAY0)
    tick(up, DAY0 + 89 * 60)
    assert len(up.calls) == 1                       # 89 min < 90
    tick(up, DAY0 + 91 * 60)
    assert len(up.calls) == 2


def test_maximo_3_reintentos(pub, fresh_db):
    cid = pub(8)
    up = Up(fail=True)
    for i in range(6):
        tick(up, DAY0 + i * 20 * 60)                # cada 20 min (> retry_minutes)
    assert up.calls == [cid] * 3
    assert [u["status"] for u in fresh_db.uploads_for(cid)] == ["error"] * 3


def test_espera_despues_de_error(pub):
    pub(8)
    up = Up(fail=True)
    tick(up, DAY0)
    tick(up, DAY0 + 5 * 60)                         # 5 min < retry_minutes
    assert len(up.calls) == 1


def test_error_y_despues_ok_no_repite(pub, fresh_db):
    cid = pub(8)
    tick(Up(fail=True), DAY0)
    up = Up()
    tick(up, DAY0 + 20 * 60)
    tick(up, DAY0 + 5 * H)
    assert up.calls == [cid]


def test_subida_interrumpida_no_se_reintenta(pub, fresh_db):
    cid = pub(8)
    fresh_db.start_upload(cid, "youtube", now=DAY0)       # el programa murió a mitad
    assert fresh_db.recover_interrupted() == 1
    up = Up()
    tick(up, DAY0 + 5 * H)
    assert up.calls == []
    assert fresh_db.uploads_today("youtube", DAY0 + 1) == 1  # cuenta para el tope


def test_aprobacion_manual(pub, cfg):
    cfg("upload", "require_approval", True)
    pub(9)                                          # score alto pero sin aprobar
    ok = pub(2, status="approved")                  # aprobado a mano: se sube aunque el score sea bajo
    up = Up()
    tick(up, DAY0)
    tick(up, DAY0 + 3 * H)
    assert up.calls == [ok]


def test_descartado_no_se_sube(pub):
    pub(9, status="discarded")
    up = Up()
    tick(up, DAY0)
    assert up.calls == []


def test_archivo_borrado_cuenta_como_error(pub, fresh_db):
    cid = pub(9)
    from pathlib import Path
    Path(fresh_db.get_clip(cid)["path"]).unlink()
    up = Up()
    tick(up, DAY0)
    assert up.calls == []                           # ni llama al uploader
    assert fresh_db.uploads_for(cid)[0]["status"] == "error"
