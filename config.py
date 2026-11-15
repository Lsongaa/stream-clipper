"""Carga config.yaml + .env, completa valores por defecto, valida y define rutas."""
import logging
import logging.handlers
import os
import re
import shutil
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

CATEGORIES = ["gracioso", "triste", "epico", "fail", "random"]
PLATFORMS = ["youtube", "tiktok", "instagram"]

# Valores por defecto: config.yaml solo necesita lo que cambia
DEFAULTS = {
    "twitch_channel": "",
    "kick_channel": "",
    "kick_chatroom_id": None,
    "kick_pusher_key": "",        # vacío = se pide a Kick automáticamente (no oficial, ver README)
    "kick_pusher_cluster": "us2", # solo se usa si kick_pusher_key está puesta a mano
    "record_from": "twitch",
    "segment_seconds": 10,
    "buffer_minutes": 6,
    "clip_pre_seconds": 30,
    "clip_post_seconds": 12,
    "merge_seconds": 20,          # eventos más juntos que esto se unen en un solo clip
    "detector": {
        "window_seconds": 10,
        "baseline_minutes": 10,
        "zscore": 3.0,
        "min_msgs": 8,
        "cooldown_seconds": 90,
        "manual_command": "!clip",
        "manual_cooldown_seconds": 20,
        "manual_users": [],       # vacío = cualquiera puede usar !clip
    },
    "language": "es",
    "whisper_model": "small",
    "claude_model": "claude-haiku-5-5",
    "subtitles": False,
    "ffmpeg_path": "",            # vacío = buscar en el PATH
    "panel": {"enabled": True, "port": 8787},
    "upload": {
        "platforms": ["youtube", "tiktok", "instagram"],
        "require_approval": False,
        "max_per_day_per_platform": 6,
        "min_gap_minutes": 90,
        "retry_minutes": 15,
        "max_attempts": 3,
        "min_score": 6,
        "youtube_privacy": "public",
        "youtube_category_id": "20",
        "tiktok_privacy": "SELF_ONLY",
    },
}


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(base.get(k), dict) and isinstance(v, dict) else v
    return out


def validate(cfg):
    """Devuelve lista de errores de configuración (vacía = ok)."""
    err = []
    if cfg["record_from"] not in ("twitch", "kick"):
        err.append("record_from debe ser twitch o kick")
    for k in ("segment_seconds", "buffer_minutes", "clip_pre_seconds", "clip_post_seconds"):
        if not isinstance(cfg[k], (int, float)) or cfg[k] <= 0:
            err.append(f"{k} debe ser un número > 0")
    if not err:
        need = cfg["clip_pre_seconds"] + cfg["clip_post_seconds"] + 3 * cfg["segment_seconds"] + 60
        if cfg["buffer_minutes"] * 60 < need:
            err.append(f"buffer_minutes muy chico: necesita al menos {need / 60:.1f} min")
    bad = set(cfg["upload"]["platforms"]) - set(PLATFORMS)
    if bad:
        err.append(f"plataformas desconocidas: {bad}")
    if cfg["upload"]["youtube_privacy"] not in ("public", "unlisted", "private"):
        err.append("youtube_privacy debe ser public, unlisted o private")
    if cfg["upload"]["tiktok_privacy"] not in ("PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS",
                                               "FOLLOWER_OF_CREATOR", "SELF_ONLY"):
        err.append("tiktok_privacy inválido")
    return err


def load(path=ROOT / "config.yaml"):
    with open(path, encoding="utf-8") as f:
        cfg = _merge(DEFAULTS, yaml.safe_load(f) or {})
    errors = validate(cfg)
    if errors:
        print("config.yaml inválido:\n - " + "\n - ".join(errors), file=sys.stderr)
        raise SystemExit(2)  # run.bat no relanza con código 2
    cfg["detector"]["manual_command"] = str(cfg["detector"]["manual_command"]).strip().lower()
    return cfg


CFG = load()
BUFFER = ROOT / "buffer"
CLIPS = ROOT / "clips"
TMP = ROOT / "tmp"
LOGS = ROOT / "logs"
for d in (BUFFER, CLIPS, TMP, LOGS):
    d.mkdir(exist_ok=True)


def tool(name):
    """Ruta a ffmpeg/ffprobe/streamlink (o None si no está)."""
    if name in ("ffmpeg", "ffprobe") and CFG["ffmpeg_path"]:
        d = Path(CFG["ffmpeg_path"])           # carpeta bin de ffmpeg (o ruta a ffmpeg.exe)
        p = (d.parent if d.suffix.lower() == ".exe" else d) / f"{name}.exe"
        return str(p) if p.exists() else None
    local = Path(sys.executable).parent / (name + ".exe")  # streamlink instalado en el venv
    if local.exists():
        return str(local)
    found = shutil.which(name)
    if not found and name in ("ffmpeg", "ffprobe") and os.environ.get("LOCALAPPDATA"):
        # instalado con winget pero el PATH de este proceso es viejo
        pk = Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "WinGet" / "Packages"
        hits = sorted(pk.glob(f"Gyan.FFmpeg*/*/bin/{name}.exe"))
        found = str(hits[-1]) if hits else None
    return found


# --- logging -------------------------------------------------------------
_SECRET_ENV = ("ANTHROPIC_API_KEY", "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "IG_USER", "IG_PASS")
_SECRET_RE = re.compile(
    r"(?i)((?:access_token|refresh_token|client_secret|code_verifier|password|authorization)"
    r"[\"']?\s*[:=]\s*[\"']?(?:bearer\s+)?)[^\"'&\s,}]+")


class RedactingFormatter(logging.Formatter):
    """Tapa secretos (valores del .env y tokens) en todo lo que se loguea, tracebacks incluidos."""

    def format(self, record):
        s = super().format(record)
        for k in _SECRET_ENV:
            v = os.environ.get(k)
            if v and len(v) >= 4:
                s = s.replace(v, "***")
        return _SECRET_RE.sub(r"\1***", s)


def setup_logging(level=logging.INFO):
    fmt = RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    con = logging.StreamHandler()
    fh = logging.handlers.RotatingFileHandler(LOGS / "clipper.log", maxBytes=5_000_000,
                                              backupCount=5, encoding="utf-8")
    for h in (con, fh):
        h.setFormatter(fmt)
        root.addHandler(h)
    # librerías muy charlatanas
    for noisy in ("httpx", "httpx2", "urllib3", "googleapiclient", "faster_whisper", "werkzeug",
                  "websockets", "instagrapi", "public_request", "private_request"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
