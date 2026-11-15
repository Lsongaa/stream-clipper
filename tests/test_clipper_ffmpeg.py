"""Corte vertical real con ffmpeg sobre segmentos .ts sintéticos (como los del grabador)."""
import asyncio
import os
import time
from datetime import datetime

import pytest

import clipper
import subtitles
from clipper import FMT
from conftest import ffmpeg, ffprobe_size, needs_ffmpeg

pytestmark = needs_ffmpeg


@pytest.fixture
def buffer(tmp_path, monkeypatch):
    """6 segmentos de 10 s, 1280x720 horizontales, terminados hace 30 s."""
    buf = tmp_path / "buffer"
    buf.mkdir()
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=60",
           "-f", "lavfi", "-i", "sine=frequency=300:duration=60",
           "-c:v", "libx264", "-preset", "ultrafast", "-g", "60", "-c:a", "aac",
           "-f", "segment", "-segment_time", "10", "-segment_format", "mpegts",
           "-reset_timestamps", "1", str(buf / "seg%03d.ts"))
    start = int(time.time()) - 90
    for i, f in enumerate(sorted(buf.glob("seg*.ts"))):
        name = buf / (datetime.fromtimestamp(start + 10 * i).strftime(FMT) + ".ts")
        f.rename(name)
        os.utime(name, (start + 10 * (i + 1),) * 2)
    monkeypatch.setattr(clipper, "BUFFER", buf)
    monkeypatch.setattr(clipper, "TMP", tmp_path)
    return buf, start


def test_corte_vertical_1080x1920(buffer, cfg):
    cfg(None, "clip_pre_seconds", 30)
    cfg(None, "clip_post_seconds", 12)
    _, start = buffer
    out = asyncio.run(clipper.make_clip(start + 40))
    assert out and out.exists()
    w, h, dur = ffprobe_size(out)
    assert (w, h) == (1080, 1920)
    assert abs(dur - 42) < 1.5          # recorta a pre+post, no a segmentos enteros


def test_segmento_corrupto(buffer, cfg):
    cfg(None, "clip_pre_seconds", 30)
    cfg(None, "clip_post_seconds", 12)
    buf, start = buffer
    victim = sorted(buf.glob("*.ts"))[2]
    mtime = victim.stat().st_mtime
    victim.write_bytes(os.urandom(200_000))      # basura en el medio del clip
    os.utime(victim, (mtime, mtime))
    out = asyncio.run(clipper.make_clip(start + 40))
    assert out and out.exists()
    assert ffprobe_size(out)[:2] == (1080, 1920)


def test_sin_buffer_devuelve_none(buffer):
    _, start = buffer
    assert asyncio.run(clipper.make_clip(start - 3600)) is None


def test_subtitulos_quemados(make_video, tmp_path):
    v = make_video("c.mp4", seconds=4)
    ass = tmp_path / "c.ass"
    ass.write_text(subtitles.build_ass([(0.2, 0.6, "hola"), (0.7, 1.0, "che"), (2.0, 2.5, "{raro}\\N")]),
                   encoding="utf-8")
    out = asyncio.run(clipper.burn_subtitles(v, ass))
    assert out == v and v.exists()
    assert ffprobe_size(v)[:2] == (1080, 1920)


def test_probe_archivo_invalido(tmp_path):
    bad = tmp_path / "x.mp4"
    bad.write_bytes(b"no soy un video")
    assert asyncio.run(clipper.probe(bad)) is None
