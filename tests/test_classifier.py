"""Clasificador con la API de Anthropic mockeada (no hace llamadas reales)."""
import json
from types import SimpleNamespace as NS

import classifier
from conftest import needs_ffmpeg


class FakeMessages:
    def __init__(self, response=None, exc=None):
        self.response, self.exc, self.kwargs = response, exc, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.exc:
            raise self.exc
        return self.response


def fake_client(monkeypatch, **kw):
    msgs = FakeMessages(**kw)
    monkeypatch.setattr(classifier, "_client", NS(messages=msgs))
    return msgs


def resp(payload, stop="end_turn"):
    # Haiku 5.5 piensa por defecto: el primer bloque puede ser 'thinking', no texto
    return NS(stop_reason=stop, content=[NS(type="thinking", thinking=""),
                                         NS(type="text", text=json.dumps(payload))])


GOOD = {"category": "epico", "score": 9, "title": "Clutch 1v4 increíble",
        "description": "Ganó la ronda solo.", "hashtags": ["#clutch", "valorant", "gaming"]}


def test_clasifica_y_manda_salida_estructurada(monkeypatch):
    msgs = fake_client(monkeypatch, response=resp(GOOD))
    r = classifier.sanitize(classifier.classify("texto", ["a: wtf", "b: jaja"]))
    assert r["category"] == "epico" and r["score"] == 9
    assert r["hashtags"] == ["clutch", "valorant", "gaming"]
    k = msgs.kwargs
    assert k["model"] == classifier.CFG["claude_model"]
    assert k["output_config"]["format"]["type"] == "json_schema"
    assert k["output_config"]["format"]["schema"]["properties"]["category"]["enum"] == classifier.CATEGORIES
    assert "a: wtf" in k["messages"][0]["content"]


def test_sanitize_respuestas_raras():
    r = classifier.sanitize({"category": "otra", "score": "99", "title": "<b>\"x\"</b> #tag",
                             "hashtags": "uno, dos dos  #tres"})
    assert r["category"] == "random" and r["score"] == 10
    assert "<" not in r["title"] and "#" not in r["title"]
    assert r["hashtags"] == ["uno", "dos", "tres"]
    assert classifier.sanitize(None) == classifier.DEFAULT
    assert classifier.sanitize({"score": "abc"})["score"] == 5


def test_rechazo_y_errores_dan_default(monkeypatch):
    monkeypatch.setattr(classifier, "transcribe", lambda p: ("hola", [(0, 1, "hola")]))
    fake_client(monkeypatch, response=resp({}, stop="refusal"))
    info, words, tr = classifier.analyze("x.mp4", [])
    assert info == classifier.DEFAULT and tr == "hola" and words == [(0, 1, "hola")]

    fake_client(monkeypatch, exc=ConnectionError("sin red"))
    assert classifier.analyze("x.mp4", [])[0] == classifier.DEFAULT


def test_whisper_falla_igual_clasifica(monkeypatch):
    def boom(p):
        raise RuntimeError("modelo roto")
    monkeypatch.setattr(classifier, "transcribe", boom)
    msgs = fake_client(monkeypatch, response=resp(GOOD))
    info, words, tr = classifier.analyze("x.mp4", ["c: lol"])
    assert info["category"] == "epico" and words == [] and tr == ""
    assert "(sin voz)" in msgs.kwargs["messages"][0]["content"]


@needs_ffmpeg
def test_load_audio_con_ffmpeg(make_video, tmp_path):
    """Whisper recibe el audio ya decodificado (evita el bug de faster-whisper con PyAV 19+)."""
    from conftest import ffmpeg
    v = make_video("a.mp4", seconds=3)
    a = classifier.load_audio(v)
    assert a.dtype.name == "float32" and abs(a.size - 48000) < 1600 and 0 < abs(a).max() <= 1
    mute = tmp_path / "mute.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x240:duration=1", "-c:v", "libx264", str(mute))
    assert classifier.load_audio(mute).size == 0


def test_sdk_real_con_transporte_mock(monkeypatch):
    """Usa el SDK anthropic real (sin red): verifica la petición HTTP y el parseo de la respuesta."""
    import anthropic
    import httpx2
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("x-api-key") or request.headers.get("authorization")
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": seen["body"]["model"],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 20},
            "content": [{"type": "thinking", "thinking": "", "signature": "sig"},
                        {"type": "text", "text": json.dumps(GOOD)}]})

    client = anthropic.Anthropic(api_key="sk-test", max_retries=0, http_client=anthropic.DefaultHttpxClient(
        transport=httpx2.MockTransport(handler)))
    monkeypatch.setattr(classifier, "_client", client)
    r = classifier.classify("hola", ["x: y"])
    assert r["title"] == GOOD["title"]
    assert seen["url"].endswith("/v1/messages") and "sk-test" in seen["auth"]
    body = seen["body"]
    assert body["model"] == "claude-haiku-5-5"
    assert body["output_config"]["format"]["schema"]["required"] == classifier.SCHEMA["required"]
    assert body["output_config"]["effort"] == "low"
