import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
from config import CFG  # noqa: E402


@pytest.fixture
def fresh_db(tmp_path):
    db.init(tmp_path / "test.db")
    yield db
    db.init()


@pytest.fixture
def cfg(monkeypatch):
    """Permite cambiar CFG en un test sin afectar a los demás."""
    def set_(section, key, value):
        target = CFG[section] if section else CFG
        monkeypatch.setitem(target, key, value)
    return set_


needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                                  reason="ffmpeg/ffprobe no están en el PATH")


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], check=True,
                   stdin=subprocess.DEVNULL)


def ffprobe_size(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height:format=duration", "-of", "csv=p=0:s=x", str(path)],
                         capture_output=True, text=True, check=True).stdout.split()
    w, h = out[0].split("x")[:2]
    return int(w), int(h), float(out[-1].strip("x"))


@pytest.fixture
def make_video(tmp_path):
    """Crea un mp4 vertical de prueba y devuelve su ruta."""
    def _make(name="v.mp4", seconds=3, size="1080x1920"):
        p = tmp_path / name
        ffmpeg("-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:duration={seconds}",
               "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
               "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
               "-movflags", "+faststart", str(p))
        return p
    return _make


os.environ.setdefault("PYTHONUTF8", "1")
