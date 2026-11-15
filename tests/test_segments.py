import os
import time
from datetime import datetime
from pathlib import Path

from clipper import FMT, scan_buffer, select_segments

SEG = 10


def segs(start, n, size=1000):
    """n segmentos consecutivos de 10 s desde `start`: (path, inicio, fin, tamaño)."""
    return [(Path(f"s{i}.ts"), start + i * SEG, start + (i + 1) * SEG, size) for i in range(n)]


def test_elige_los_que_cubren_el_rango():
    s = segs(1000, 10)                       # 1000..1100
    out = select_segments(s, 1032, 1074, now=2000)
    assert [p.name for p, _ in out] == ["s3.ts", "s4.ts", "s5.ts", "s6.ts", "s7.ts"]
    assert 1032 - out[0][1] == 2             # offset dentro del primer segmento


def test_excluye_el_segmento_en_escritura():
    s = segs(1000, 6)                        # el último termina en 1060
    out = select_segments(s, 1020, 1062, now=1061)   # se escribió hace 1 s
    assert "s5.ts" not in [p.name for p, _ in out]


def test_incluye_el_ultimo_si_la_grabacion_paro():
    s = segs(1000, 6)
    out = select_segments(s, 1020, 1062, now=1100)
    assert "s5.ts" in [p.name for p, _ in out]


def test_ignora_vacios():
    s = segs(1000, 6)
    s[3] = (s[3][0], s[3][1], s[3][2], 0)
    out = select_segments(s, 1000, 1060, now=2000)
    assert "s3.ts" not in [p.name for p, _ in out]


def test_cobertura_insuficiente():
    s = segs(1000, 2)                        # solo 20 s de los 42 pedidos
    assert select_segments(s, 1000, 1042, now=2000) == []
    assert select_segments([], 0, 42, now=100) == []
    assert select_segments(segs(5000, 6), 1000, 1042, now=9000) == []  # fuera del buffer


def test_scan_buffer_lee_nombres(tmp_path):
    base = time.time() - 100
    for i in range(3):
        f = tmp_path / (datetime.fromtimestamp(base + i * SEG).strftime(FMT) + ".ts")
        f.write_bytes(b"x" * 10)
        os.utime(f, (base + (i + 1) * SEG,) * 2)
    (tmp_path / "basura.ts").write_bytes(b"x")
    (tmp_path / "otro.txt").write_text("x")
    out = scan_buffer(tmp_path)
    assert len(out) == 3
    assert [round(o[1] - out[0][1]) for o in out] == [0, 10, 20]
    assert abs(out[0][2] - (base + SEG)) < 1
