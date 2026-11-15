"""Uploaders con HTTP mockeado: verifica endpoints, campos y headers según la doc oficial."""
import json
import subprocess
import time
from types import SimpleNamespace as NS

import pytest

from conftest import needs_ffmpeg


def clip(path, **kw):
    d = {"id": 7, "path": str(path), "title": "Clutch <increíble>", "description": "desc",
         "tags": "clutch,valorant,gaming", "score": 9, "category": "epico"}
    d.update(kw)
    return d


# --- YouTube -------------------------------------------------------------
def test_youtube_body_limites():
    from uploaders.youtube import build_body
    b = build_body(clip("x", title="a" * 200, tags=",".join(f"tag{i:03d}" for i in range(200))))
    s = b["snippet"]
    assert len(s["title"]) <= 100 and s["title"].endswith("#Shorts")
    assert len(",".join(s["tags"])) <= 500
    assert "<" not in build_body(clip("x"))["snippet"]["title"]
    assert b["status"] == {"privacyStatus": "public", "selfDeclaredMadeForKids": False}
    assert s["categoryId"] == "20"


def test_youtube_subida_reanudable(tmp_path, monkeypatch):
    """Cliente oficial de Google con HTTP falso: inicia sesión reanudable, reintenta un 503 y sube."""
    from googleapiclient.discovery import build
    from googleapiclient.http import HttpMockSequence
    from uploaders import youtube
    monkeypatch.setattr(youtube.time, "sleep", lambda s: None)
    f = tmp_path / "c.mp4"
    f.write_bytes(b"0" * 2048)
    seen = []

    class Rec(HttpMockSequence):
        def request(self, uri, method="GET", body=None, headers=None, **kw):
            seen.append((method, uri, body))
            return super().request(uri, method, body, headers, **kw)

    http = Rec([
        ({"status": "200", "location": "https://upload.example/sesion1"}, b""),
        ({"status": "503"}, b"backend error"),
        ({"status": "200"}, json.dumps({"id": "abc123"}).encode()),
    ])
    yt = build("youtube", "v3", http=http, static_discovery=True)
    assert youtube.upload(clip(f), service=yt) == "https://youtube.com/shorts/abc123"
    m, uri, body = seen[0]
    assert m == "POST" and "/upload/youtube/v3/videos" in uri
    assert "uploadType=resumable" in uri and "part=snippet%2Cstatus" in uri
    meta = json.loads(body)
    assert meta["snippet"]["title"].endswith("#Shorts") and meta["status"]["privacyStatus"] == "public"
    assert [s[0] for s in seen[1:]] == ["PUT", "PUT"] and seen[1][1] == "https://upload.example/sesion1"


def test_youtube_error_no_reintentable(tmp_path):
    from googleapiclient.discovery import build
    from googleapiclient.http import HttpMockSequence
    from uploaders import youtube
    f = tmp_path / "c.mp4"
    f.write_bytes(b"0" * 10)
    err = {"error": {"errors": [{"reason": "uploadLimitExceeded"}], "code": 400}}
    http = HttpMockSequence([({"status": "400"}, json.dumps(err).encode())])
    yt = build("youtube", "v3", http=http, static_discovery=True)
    with pytest.raises(RuntimeError, match="uploadLimitExceeded"):
        youtube.upload(clip(f), service=yt)


def test_youtube_sin_token(tmp_path, monkeypatch):
    from uploaders import youtube
    monkeypatch.setattr(youtube, "TOKEN", tmp_path / "no.json")
    with pytest.raises(RuntimeError, match="auth_youtube"):
        youtube.credentials()


# --- TikTok --------------------------------------------------------------
def test_tiktok_partes():
    from uploaders.tiktok import MB, plan_chunks
    assert plan_chunks(3 * MB) == (3 * MB, [(0, 3 * MB - 1)])            # < 5 MB: entero
    assert plan_chunks(50 * MB) == (50 * MB, [(0, 50 * MB - 1)])
    assert plan_chunks(100 * MB) == (64 * MB, [(0, 100 * MB - 1)])       # 1 parte de hasta 128 MB
    for size in (64 * MB, 100 * MB, 129 * MB, 1000 * MB + 7):
        c, r = plan_chunks(size)
        assert len(r) == max(1, size // c)                               # floor, como pide la doc
        assert r[0][0] == 0 and r[-1][1] == size - 1
        assert all(b + 1 == a for (_, b), (a, _) in zip(r, r[1:]))       # contiguas
        assert all(5 * MB <= b - a + 1 <= 64 * MB for a, b in r[:-1])
        assert r[-1][1] - r[-1][0] + 1 <= 128 * MB


class FakeHTTP:
    def __init__(self):
        self.posts, self.puts = [], []
        self.privacy, self.status = ["PUBLIC_TO_EVERYONE", "SELF_ONLY"], "PUBLISH_COMPLETE"

    def post(self, url, timeout=None, json=None, data=None, headers=None):
        self.posts.append((url, json, data, headers))
        ok = {"code": "ok", "message": "", "log_id": "x"}
        body = {
            "/v2/oauth/token/": {"access_token": "AT2", "refresh_token": "RT2", "expires_in": 7200,
                                 "refresh_expires_in": 15552000, "open_id": "o", "scope": "video.publish"},
            "/v2/post/publish/creator_info/query/": {"data": {"privacy_level_options": self.privacy,
                                                              "comment_disabled": True}, "error": ok},
            "/v2/post/publish/video/init/": {"data": {"publish_id": "p1", "upload_url": "https://up/x?t=1"},
                                             "error": ok},
            "/v2/post/publish/status/fetch/": {"data": {"status": self.status, "fail_reason": "spam"},
                                               "error": ok},
        }[url.split("open.tiktokapis.com")[1]]
        return NS(status_code=200, json=lambda: body)

    def put(self, url, data=None, timeout=None, headers=None):
        self.puts.append((url, len(data), headers))
        return NS(status_code=201)


@pytest.fixture
def tt(tmp_path, monkeypatch):
    from uploaders import tiktok
    monkeypatch.setattr(tiktok, "TOKEN", tmp_path / "tok.json")
    monkeypatch.setattr(tiktok.time, "sleep", lambda s: None)
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "ck")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "cs")
    fake = FakeHTTP()
    monkeypatch.setattr(tiktok, "requests", NS(post=fake.post, put=fake.put))
    tiktok.save_token({"access_token": "AT1", "refresh_token": "RT1", "expires_in": 7200})
    return tiktok, fake


def test_tiktok_flujo_completo(tt, tmp_path):
    tiktok, fake = tt
    f = tmp_path / "c.mp4"
    f.write_bytes(b"v" * 4096)
    out = tiktok.upload(clip(f))
    assert "publish_id=p1" in out and "PUBLISH_COMPLETE" in out
    paths = [u.split("open.tiktokapis.com")[1] for u, *_ in fake.posts]
    assert paths == ["/v2/post/publish/creator_info/query/", "/v2/post/publish/video/init/",
                     "/v2/post/publish/status/fetch/"]                  # token vigente: sin refresh
    _, body, _, headers = fake.posts[1]
    assert headers["Authorization"] == "Bearer AT1"
    assert headers["Content-Type"] == "application/json; charset=UTF-8"
    pi, si = body["post_info"], body["source_info"]
    assert pi["privacy_level"] == "SELF_ONLY" and pi["disable_comment"] is True
    assert pi["brand_content_toggle"] is False and "#clutch" in pi["title"]
    assert si == {"source": "FILE_UPLOAD", "video_size": 4096, "chunk_size": 4096, "total_chunk_count": 1}
    url, n, h = fake.puts[0]
    assert url == "https://up/x?t=1" and n == 4096
    assert h["Content-Range"] == "bytes 0-4095/4096" and h["Content-Type"] == "video/mp4"
    assert h["Content-Length"] == "4096"
    assert fake.posts[2][1] == {"publish_id": "p1"}


def test_tiktok_varias_partes(tt, tmp_path, monkeypatch):
    tiktok, fake = tt
    monkeypatch.setattr(tiktok, "MAX_CHUNK", 1000)       # achico el máximo para no crear 200 MB
    f = tmp_path / "c.mp4"
    f.write_bytes(b"v" * 2500)
    tiktok.upload(clip(f))
    assert fake.posts[1][1]["source_info"]["total_chunk_count"] == 2
    assert [p[2]["Content-Range"] for p in fake.puts] == ["bytes 0-999/2500", "bytes 1000-2499/2500"]


def test_tiktok_refresca_token_vencido(tt):
    tiktok, fake = tt
    data = json.loads(tiktok.TOKEN.read_text())
    data["obtained_at"] = time.time() - 8000
    tiktok.TOKEN.write_text(json.dumps(data))
    assert tiktok.access_token() == "AT2"
    url, _, form, _ = fake.posts[0]
    assert url == "https://open.tiktokapis.com/v2/oauth/token/"
    assert form == {"client_key": "ck", "client_secret": "cs", "grant_type": "refresh_token",
                    "refresh_token": "RT1"}
    assert json.loads(tiktok.TOKEN.read_text())["refresh_token"] == "RT2"   # guardó el rotado


def test_tiktok_privacidad_no_permitida(tt, tmp_path, cfg):
    tiktok, fake = tt
    cfg("upload", "tiktok_privacy", "PUBLIC_TO_EVERYONE")
    fake.privacy = ["SELF_ONLY"]
    f = tmp_path / "c.mp4"
    f.write_bytes(b"v")
    with pytest.raises(RuntimeError, match="no permitido"):
        tiktok.upload(clip(f))
    assert fake.puts == []


def test_tiktok_publicacion_fallida(tt, tmp_path):
    tiktok, fake = tt
    fake.status = "FAILED"
    f = tmp_path / "c.mp4"
    f.write_bytes(b"v" * 10)
    with pytest.raises(RuntimeError, match="spam"):
        tiktok.upload(clip(f))


def test_tiktok_caption_largo():
    from uploaders.tiktok import caption
    c = caption(clip("x", description="😀" * 3000))
    assert len(c.encode("utf-16-le")) // 2 <= 2200


# --- Instagram -----------------------------------------------------------
@needs_ffmpeg
def test_instagram_con_cliente_falso(make_video, tmp_path, monkeypatch):
    from uploaders import instagram
    monkeypatch.setattr(instagram, "TMP", tmp_path)
    v = make_video("ig.mp4", seconds=3)
    calls = {}

    class FakeIG:
        def clip_upload(self, path, caption, thumbnail=None):
            calls.update(path=path, caption=caption, thumb_exists=thumbnail.exists())
            return NS(code="ABC")

    assert instagram.upload(clip(v), client=FakeIG()) == "https://instagram.com/reel/ABC"
    assert calls["thumb_exists"] and "#clutch" in calls["caption"]
    assert not list(tmp_path.glob("ig_thumb_*"))        # borró la miniatura


@needs_ffmpeg
def test_instagrapi_analiza_video_sin_moviepy(make_video, tmp_path):
    """La función real de instagrapi lee nuestro mp4 + miniatura sin necesitar moviepy."""
    from instagrapi.mixins.clip import analyze_video
    v = make_video("ig2.mp4", seconds=3)
    th = tmp_path / "t.jpg"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(v), "-frames:v", "1", str(th)], check=True)
    _, w, h, dur = analyze_video(v, th)
    assert (w, h) == (1080, 1920) and 2.5 < dur < 3.5
