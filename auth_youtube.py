"""Una sola vez: python auth_youtube.py  -> abre el navegador y guarda youtube_token.json.
Necesita client_secret.json (OAuth "App de escritorio") en esta carpeta."""
from google_auth_oauthlib.flow import InstalledAppFlow

from uploaders.youtube import SCOPES, SECRET, save_token

if not SECRET.exists():
    raise SystemExit("Falta client_secret.json (Google Cloud > Credenciales > OAuth > App de escritorio)")
creds = InstalledAppFlow.from_client_secrets_file(str(SECRET), SCOPES).run_local_server(
    host="localhost", port=0, prompt="consent", access_type="offline")
save_token(creds)
print("OK: youtube_token.json guardado")
