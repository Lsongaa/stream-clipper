"""Una sola vez: python auth_tiktok.py  -> guarda tiktok_token.json
Registrá la app como "Desktop" en TikTok for Developers (Login Kit) con este Redirect URI:
    http://127.0.0.1:8765/callback/
Desktop exige PKCE; TikTok pide el code_challenge como SHA256 en HEX (no base64)."""
import hashlib
import os
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from uploaders.tiktok import API, save_token

REDIRECT = "http://127.0.0.1:8765/callback/"
KEY, SECRET = os.environ.get("TIKTOK_CLIENT_KEY"), os.environ.get("TIKTOK_CLIENT_SECRET")
if not KEY or not SECRET:
    raise SystemExit("Completá TIKTOK_CLIENT_KEY y TIKTOK_CLIENT_SECRET en .env")

verifier = secrets.token_urlsafe(64)[:64]                 # 43-128 caracteres no reservados
challenge = hashlib.sha256(verifier.encode()).hexdigest()
state = secrets.token_urlsafe(16)
result = {}


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        if u.path.rstrip("/") != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        q = parse_qs(u.query)  # parse_qs ya decodifica el code (la doc lo pide decodificado)
        result.update(code=q.get("code", [None])[0], state=q.get("state", [None])[0],
                      error=q.get("error_description", q.get("error", [None]))[0])
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Listo, volvé a la consola.".encode())

    def log_message(self, *a):
        pass


webbrowser.open("https://www.tiktok.com/v2/auth/authorize/?" + urlencode({
    "client_key": KEY, "scope": "user.info.basic,video.publish", "response_type": "code",
    "redirect_uri": REDIRECT, "state": state,
    "code_challenge": challenge, "code_challenge_method": "S256"}))
srv = HTTPServer(("127.0.0.1", 8765), H)
while "code" not in result:
    srv.handle_request()
if result["error"] or not result["code"]:
    raise SystemExit(f"TikTok devolvió error: {result['error']}")
if result["state"] != state:
    raise SystemExit("state no coincide: abortado")
r = requests.post(f"{API}/v2/oauth/token/", timeout=30, data={
    "client_key": KEY, "client_secret": SECRET, "code": result["code"],
    "grant_type": "authorization_code", "redirect_uri": REDIRECT, "code_verifier": verifier}).json()
if "access_token" not in r:
    raise SystemExit(f"Error: {r.get('error')} - {r.get('error_description')}")
save_token(r)
print("OK: tiktok_token.json guardado. Scopes:", r.get("scope"))
