"""Lectores de chat anónimos (solo lectura) para Twitch (IRC por WebSocket) y Kick (Pusher, no oficial)."""
import asyncio
import json
import logging
import random
import uuid

import websockets

from config import CFG

log = logging.getLogger("chat")
TWITCH_WS = "wss://irc-ws.chat.twitch.tv:443"
IDLE_TIMEOUT = 360   # sin datos por 6 min => conexión muerta, reconectar


def _backoff(attempt):
    return min(300, 5 * 2 ** min(attempt, 6)) * random.uniform(0.8, 1.2)


# --- Twitch --------------------------------------------------------------
def parse_twitch(raw):
    """Devuelve lista de ('ping', payload) | ('msg', user, text) | ('reconnect',)."""
    out = []
    for line in raw.split("\r\n"):
        if not line:
            continue
        if line.startswith("PING"):
            out.append(("ping", line[5:] or ":tmi.twitch.tv"))
        elif " PRIVMSG " in line and line.startswith(":"):
            prefix, _, rest = line.partition(" PRIVMSG ")
            _, _, text = rest.partition(" :")
            out.append(("msg", prefix[1:].split("!", 1)[0], text))
        elif " RECONNECT" in line or line.startswith("RECONNECT"):
            out.append(("reconnect",))
    return out


async def twitch(channel, on_msg, url=TWITCH_WS):
    attempt = 0
    while True:
        try:
            async with websockets.connect(url, open_timeout=20, ping_interval=30) as ws:
                for line in ("PASS SCHMOOPIIE", f"NICK justinfan{random.randint(10000, 99999)}",
                             f"JOIN #{channel.lower().lstrip('#')}"):
                    await ws.send(line)
                log.info("twitch chat conectado (#%s)", channel)
                while True:
                    raw = await asyncio.wait_for(ws.recv(), IDLE_TIMEOUT)
                    attempt = 0
                    for ev in parse_twitch(raw if isinstance(raw, str) else raw.decode("utf-8", "replace")):
                        if ev[0] == "ping":
                            await ws.send("PONG " + ev[1])
                        elif ev[0] == "msg":
                            on_msg("twitch", ev[1], ev[2])
                        else:
                            raise ConnectionError("Twitch pidió RECONNECT")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("twitch chat: %s", e or type(e).__name__)
        attempt += 1
        await asyncio.sleep(_backoff(attempt))


# --- Kick ----------------------------------------------------------------
def parse_kick(raw):
    """Devuelve ('ping',) | ('msg', user, text) | ('error', data) | None."""
    try:
        m = json.loads(raw)
        ev = m.get("event", "")
        if ev == "pusher:ping":
            return ("ping",)
        if ev == "pusher:error":
            return ("error", m.get("data"))
        if ev.endswith("ChatMessageEvent"):
            d = m["data"]
            d = json.loads(d) if isinstance(d, str) else d
            return ("msg", d["sender"]["username"], d["content"])
    except (ValueError, KeyError, TypeError, AttributeError):
        log.debug("kick: mensaje no reconocido: %.200s", raw)
    return None


KICK_REALTIME = "https://web.kick.com/api/v1/realtime/connection"
KICK_FALLBACK = ("34bf7a0ff419a2a775b9", "us2")  # última key vista (13/11/2026)


def _kick_pusher_blocking():
    """Pide a Kick la key/cluster actuales de Pusher (Kick las rota; endpoint no oficial)."""
    from curl_cffi import requests as cr
    body = {"client": {"id": str(uuid.uuid4()), "type": "web"},
            "capabilities": {"accepted_providers": [{"provider": "pusher"}]}}
    r = cr.post(KICK_REALTIME, json=body, impersonate="chrome", timeout=20,
                headers={"Accept": "application/json", "x-app-platform": "web"})
    r.raise_for_status()
    for c in r.json()["data"]["connections"]:
        if c.get("provider") == "pusher":
            return c["credentials"]["app_key"], c["credentials"]["cluster"]
    raise RuntimeError("Kick no ofreció Pusher")


async def kick_pusher():
    """(key, cluster): la de config.yaml si está puesta; si no, la que da Kick ahora."""
    if CFG["kick_pusher_key"]:
        return CFG["kick_pusher_key"], CFG["kick_pusher_cluster"]
    try:
        return await asyncio.to_thread(_kick_pusher_blocking)
    except Exception as e:
        log.warning("kick: no pude pedir la key de Pusher (%s); uso la última conocida", e)
        return KICK_FALLBACK


def kick_ws_url(key, cluster):
    return f"wss://ws-{cluster}.pusher.com/app/{key}?protocol=7&client=js&version=8.4.0&flash=false"


def _kick_id_blocking(channel):
    from curl_cffi import requests as cr  # imita Chrome para pasar Cloudflare
    r = cr.get(f"https://kick.com/api/v2/channels/{channel}", impersonate="chrome", timeout=20)
    r.raise_for_status()
    return r.json()["chatroom"]["id"]


async def kick(channel, on_msg, chatroom_id=None, url=None):
    attempt = 0
    cid = chatroom_id
    while True:
        wait = None
        try:
            if not cid:
                cid = await asyncio.to_thread(_kick_id_blocking, channel)
                log.info("kick chatroom id = %s", cid)
            ws_url = url or kick_ws_url(*await kick_pusher())
            async with websockets.connect(ws_url, open_timeout=20, ping_interval=30) as ws:
                await ws.send(json.dumps({"event": "pusher:subscribe",
                                          "data": {"auth": "", "channel": f"chatrooms.{cid}.v2"}}))
                log.info("kick chat conectado (%s)", channel)
                while True:
                    raw = await asyncio.wait_for(ws.recv(), IDLE_TIMEOUT)
                    attempt = 0
                    ev = parse_kick(raw)
                    if not ev:
                        continue
                    if ev[0] == "ping":
                        await ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
                    elif ev[0] == "msg":
                        on_msg("kick", ev[1], ev[2])
                    elif ev[0] == "error":
                        code = (ev[1] or {}).get("code") if isinstance(ev[1], dict) else None
                        if code and 4000 <= code < 4100:  # key/cluster inválidos: no reintentar rápido
                            manual = bool(CFG["kick_pusher_key"])
                            wait = 1800 if manual else 300
                            log.error("kick chat: Pusher rechazó la key (%s). %s Reintento en %d min.", ev[1],
                                      "Revisá kick_pusher_key en config.yaml (o dejala vacía = automática)."
                                      if manual else "Se vuelve a pedir a Kick.", wait // 60)
                            break
                        raise ConnectionError(f"pusher error {ev[1]}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("kick chat: %s (si persiste, poné kick_chatroom_id en config.yaml)",
                        e or type(e).__name__)
        attempt += 1
        await asyncio.sleep(wait or _backoff(attempt))
