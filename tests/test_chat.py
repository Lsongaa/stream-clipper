"""Chat de Twitch y Kick contra servidores WebSocket falsos en localhost (incluye reconexión)."""
import asyncio
import json

import pytest
import websockets

import chat


def test_parse_twitch():
    raw = (":tmi.twitch.tv 001 justinfan1 :Welcome\r\n"
           "PING :tmi.twitch.tv\r\n"
           ":pepe!pepe@pepe.tmi.twitch.tv PRIVMSG #canal :jajaja que : crack\r\n"
           ":tmi.twitch.tv RECONNECT\r\n")
    assert chat.parse_twitch(raw) == [("ping", ":tmi.twitch.tv"), ("msg", "pepe", "jajaja que : crack"),
                                      ("reconnect",)]


def test_parse_kick():
    msg = {"event": "App\\Events\\ChatMessageEvent",
           "data": json.dumps({"content": "KEKW", "sender": {"username": "ana"}})}
    assert chat.parse_kick(json.dumps(msg)) == ("msg", "ana", "KEKW")
    assert chat.parse_kick('{"event":"pusher:ping","data":{}}') == ("ping",)
    assert chat.parse_kick('{"event":"pusher:error","data":{"code":4001}}')[0] == "error"
    assert chat.parse_kick("no es json") is None
    assert chat.parse_kick('{"event":"App\\\\Events\\\\ChatMessageEvent","data":"{}"}') is None


@pytest.fixture
def fast_backoff(monkeypatch):
    monkeypatch.setattr(chat, "_backoff", lambda attempt: 0.05)


def test_twitch_mock_server(fast_backoff):
    async def scenario():
        conns, pongs, got = [], [], []

        async def handler(ws):
            conns.append([await ws.recv() for _ in range(3)])   # PASS, NICK, JOIN
            await ws.send("PING :tmi.twitch.tv\r\n")
            pongs.append(await ws.recv())
            if len(conns) == 1:
                await ws.send(":a!a@a.tmi.twitch.tv PRIVMSG #canal :hola\r\n"
                              ":b!b@b.tmi.twitch.tv PRIVMSG #canal :!clip\r\n")
                await ws.send(":tmi.twitch.tv RECONNECT\r\n")      # debe reconectar
                await asyncio.sleep(1)
            else:
                await ws.send(":c!c@c.tmi.twitch.tv PRIVMSG #canal :volvi\r\n")
                await asyncio.sleep(5)

        async with websockets.serve(handler, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            task = asyncio.create_task(chat.twitch("Canal", lambda *a: got.append(a),
                                                   url=f"ws://127.0.0.1:{port}"))
            for _ in range(100):
                if len(got) >= 3:
                    break
                await asyncio.sleep(0.05)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return conns, pongs, got

    conns, pongs, got = asyncio.run(scenario())
    assert conns[0][0] == "PASS SCHMOOPIIE" and conns[0][1].startswith("NICK justinfan")
    assert conns[0][2] == "JOIN #canal"
    assert pongs[0] == "PONG :tmi.twitch.tv"
    assert len(conns) == 2                                      # reconectó tras RECONNECT
    assert got == [("twitch", "a", "hola"), ("twitch", "b", "!clip"), ("twitch", "c", "volvi")]


def test_kick_mock_server_y_caida(fast_backoff):
    async def scenario():
        subs, pongs, got = [], [], []

        async def handler(ws):
            subs.append(json.loads(await ws.recv()))
            await ws.send(json.dumps({"event": "pusher:connection_established", "data": "{}"}))
            await ws.send(json.dumps({"event": "pusher:ping", "data": {}}))
            pongs.append(json.loads(await ws.recv()))
            await ws.send("basura que no es json")             # no debe cortar la conexión
            await ws.send(json.dumps({"event": "App\\Events\\ChatMessageEvent",
                                      "data": json.dumps({"content": f"msg{len(subs)}",
                                                          "sender": {"username": "u"}})}))
            if len(subs) == 1:
                await ws.close()                                # caída del servidor
            else:
                await asyncio.sleep(5)

        async with websockets.serve(handler, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            task = asyncio.create_task(chat.kick("canal", lambda *a: got.append(a), chatroom_id=123,
                                                 url=f"ws://127.0.0.1:{port}"))
            for _ in range(100):
                if len(got) >= 2:
                    break
                await asyncio.sleep(0.05)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return subs, pongs, got

    subs, pongs, got = asyncio.run(scenario())
    assert subs[0] == {"event": "pusher:subscribe", "data": {"auth": "", "channel": "chatrooms.123.v2"}}
    assert pongs[0]["event"] == "pusher:pong"
    assert got == [("kick", "u", "msg1"), ("kick", "u", "msg2")]   # reconectó tras la caída


@pytest.mark.parametrize("manual,espera", [("", 300), ("keyvieja", 1800)])
def test_kick_key_rechazada_espera_largo(monkeypatch, cfg, manual, espera):
    """Si Pusher rechaza la key (4001), no reintenta en loop rápido."""
    cfg(None, "kick_pusher_key", manual)
    sleeps = []
    real_sleep = asyncio.sleep

    async def fake_sleep(s):  # websockets también usa asyncio.sleep: solo cortamos en la espera larga
        sleeps.append(s)
        if s >= 60:
            raise asyncio.CancelledError
        await real_sleep(0)

    async def scenario():
        async def handler(ws):
            await ws.recv()
            await ws.send(json.dumps({"event": "pusher:error",
                                      "data": {"code": 4001, "message": "App key not in this cluster"}}))
            await real_sleep(1)

        async with websockets.serve(handler, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            monkeypatch.setattr(chat.asyncio, "sleep", fake_sleep)
            try:
                await chat.kick("c", lambda *a: None, chatroom_id=1, url=f"ws://127.0.0.1:{port}")
            except asyncio.CancelledError:
                pass
    asyncio.run(scenario())
    assert [s for s in sleeps if s >= 60] == [espera]


def test_servidor_caido_no_rompe(fast_backoff):
    """Sin servidor: reintenta para siempre sin lanzar excepciones."""
    async def scenario():
        task = asyncio.create_task(chat.twitch("x", lambda *a: None, url="ws://127.0.0.1:9"))
        await asyncio.sleep(0.5)
        assert not task.done()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_kick_pide_key_actual(monkeypatch, cfg):
    cfg(None, "kick_pusher_key", "")
    monkeypatch.setattr(chat, "_kick_pusher_blocking", lambda: ("nueva", "us3"))
    assert asyncio.run(chat.kick_pusher()) == ("nueva", "us3")

    def boom():
        raise ConnectionError("cloudflare")
    monkeypatch.setattr(chat, "_kick_pusher_blocking", boom)
    assert asyncio.run(chat.kick_pusher()) == chat.KICK_FALLBACK      # sin red: última conocida
    cfg(None, "kick_pusher_key", "manual")
    assert asyncio.run(chat.kick_pusher()) == ("manual", "us2")      # la de config manda
    assert chat.kick_ws_url("k", "us2").startswith("wss://ws-us2.pusher.com/app/k?protocol=7")
