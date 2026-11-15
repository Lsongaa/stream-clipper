"""YouTube Shorts vía YouTube Data API v3 (videos.insert, subida reanudable).
El token se crea una vez con: python auth_youtube.py"""
import json
import os
import random
import time

from config import CFG, ROOT

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
TOKEN = ROOT / "youtube_token.json"
SECRET = ROOT / "client_secret.json"
RETRY_STATUS = (500, 502, 503, 504)


def save_token(creds):
    tmp = TOKEN.with_suffix(".tmp")
    tmp.write_text(creds.to_json(), encoding="utf-8")
    os.replace(tmp, TOKEN)  # escritura atómica


def credentials():
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    if not TOKEN.exists():
        raise RuntimeError("falta youtube_token.json: corré python auth_youtube.py")
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds.valid:
        if not creds.refresh_token:
            raise RuntimeError("token de YouTube sin refresh_token: corré python auth_youtube.py")
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise RuntimeError(f"token de YouTube vencido/revocado ({e}): corré python auth_youtube.py")
        save_token(creds)
    return creds


def _clean(s):
    return str(s).replace("<", "").replace(">", "")  # YouTube no acepta < ni >


def build_body(clip):
    tags = [t for t in clip["tags"].split(",") if t]
    title = _clean(clip["title"]).strip()[:100 - len(" #Shorts")] + " #Shorts"   # máx 100 caracteres
    desc = _clean(clip["description"]) + "\n\n" + " ".join("#" + t for t in tags) + " #Shorts"
    desc = desc.encode("utf-8")[:4900].decode("utf-8", "ignore")                 # máx 5000 bytes
    kept, total = [], 0
    for t in tags:  # tags: máx 500 caracteres contando comas (y comillas si tienen espacios)
        n = len(t) + (2 if " " in t else 0) + (1 if kept else 0)
        if total + n > 500:
            break
        kept.append(t)
        total += n
    return {
        "snippet": {"title": title, "description": desc, "tags": kept,
                    "categoryId": str(CFG["upload"]["youtube_category_id"])},
        "status": {"privacyStatus": CFG["upload"]["youtube_privacy"], "selfDeclaredMadeForKids": False},
    }


def upload(clip, service=None):
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
    yt = service or build("youtube", "v3", credentials=credentials(), cache_discovery=False)
    req = yt.videos().insert(part="snippet,status", body=build_body(clip),
                             media_body=MediaFileUpload(clip["path"], mimetype="video/mp4",
                                                        chunksize=-1, resumable=True))
    resp, errors = None, 0
    while resp is None:
        try:
            _, resp = req.next_chunk()
        except HttpError as e:
            if e.resp.status not in RETRY_STATUS or errors >= 5:
                detail = e.content.decode("utf-8", "replace") if isinstance(e.content, bytes) else e.content
                try:
                    detail = json.loads(detail)["error"]["errors"][0]["reason"]
                except (ValueError, KeyError, IndexError, TypeError):
                    pass
                raise RuntimeError(f"YouTube HTTP {e.resp.status}: {detail}") from None
            errors += 1
            time.sleep(min(60, 2 ** errors) + random.random())
        except (ConnectionError, TimeoutError, OSError):
            if errors >= 5:
                raise
            errors += 1
            time.sleep(min(60, 2 ** errors) + random.random())
    return "https://youtube.com/shorts/" + resp["id"]
