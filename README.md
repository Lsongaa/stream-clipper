# stream-clipper

Graba tu stream (Twitch o Kick) en un buffer de segmentos de 10 s. Cuando detecta un pico en el chat (o alguien escribe `!clip`), corta ~42 s, los pasa a vertical 1080x1920 y los transcribe con Whisper (en tu PC). Después Claude los clasifica (gracioso / triste / epico / fail / random), les pone score, título y hashtags. Los clips se guardan en `clips/<categoria>/` y se publican en YouTube Shorts, TikTok e Instagram Reels con tope diario. Incluye un panel web local para revisarlos.

```
streamlink | ffmpeg ─► buffer/*.ts ─┐
chat Twitch/Kick ─► detector ───────┴► cortador (vertical) ─► Whisper + Claude ─► clips/ + clips.db ─► publicador
                                                                                     panel web ◄─┘
```

## Instalación (Windows)

1. Instalá **Python 3.11 o más nuevo** desde python.org (tildá "Add python.exe to PATH").
2. Instalá **ffmpeg** (el build completo trae libass, que se usa para los subtítulos):
   ```
   winget install Gyan.FFmpeg
   ```
   Cerrá y volvé a abrir la consola para que tome el PATH. Si no lo encuentra, poné la carpeta `bin` de ffmpeg en `ffmpeg_path` dentro de `config.yaml`.
3. En la carpeta del proyecto, creá el entorno e instalá las dependencias (incluye streamlink):
   ```
   python -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   ```
4. Copiá `.env.example` a `.env` y completalo (ver **Credenciales**). Editá `config.yaml`: como mínimo, el canal y `record_from`.
5. Probalo a mano: `run.bat`. Abrí el panel en http://127.0.0.1:8787 y escribí `!clip` en tu chat. A los ~20 s se corta el clip, y después de la transcripción aparece en el panel.

La primera vez, Whisper descarga el modelo (`small` pesa ~480 MB).

## Credenciales

Los secretos van **solo** en `.env` y en los archivos de token que se generan en la carpeta (`youtube_token.json`, `tiktok_token.json`, `ig_session.json`). No los compartas ni los subas a git: ya están en `.gitignore`. Los logs tapan claves y tokens automáticamente.

### Claude (Anthropic)
`ANTHROPIC_API_KEY` en `.env` (se saca de console.anthropic.com). Sin clave, los clips se guardan igual pero quedan en `random` con score 5. Como 5 está debajo de `min_score`, no se suben.

### YouTube (YouTube Data API v3)
1. En Google Cloud Console, creá un proyecto y habilitá **YouTube Data API v3**.
2. Configurá la **pantalla de consentimiento OAuth** (tipo Externo) y agregate como usuario de prueba.
   - **Importante:** si la app queda en estado **"Testing"**, Google vence el refresh token **a los 7 días** y vas a tener que repetir el paso 4 cada semana. Para evitarlo, pasala a **"En producción"**. Para uso propio no hace falta verificarla; vas a ver un aviso de "app no verificada" al autorizar.
3. En Credenciales, creá un **ID de cliente OAuth → App de escritorio**, descargá el JSON y guardalo como `client_secret.json` en esta carpeta.
4. Corré una vez `.venv\Scripts\python auth_youtube.py`. Se abre el navegador; elegí el canal. El token queda en `youtube_token.json`.

Privacidad: `upload.youtube_privacy` (`public` / `unlisted` / `private`).

### TikTok (Content Posting API)
1. En developers.tiktok.com, creá una app con los productos **Login Kit** y **Content Posting API**, con **Direct Post** habilitado y el scope `video.publish`.
2. En Login Kit, plataforma **Desktop**, registrá exactamente este Redirect URI: `http://127.0.0.1:8765/callback/`
3. Poné `TIKTOK_CLIENT_KEY` y `TIKTOK_CLIENT_SECRET` en `.env`.
4. Corré `.venv\Scripts\python auth_tiktok.py` y autorizá. Usa PKCE, que es obligatorio en apps Desktop. El token queda en `tiktok_token.json`.
   - El access token dura 2 h y se renueva solo. El refresh token dura **180 días**: antes de que venza, repetí este paso.

### Instagram (instagrapi, NO oficial)
`IG_USER` y `IG_PASS` en `.env`. La API oficial de Instagram exige que el video esté en una URL pública, por eso se usa instagrapi, que imita la app. **Hay riesgo de bloqueo o challenge**: usá una cuenta secundaria. Iniciá sesión una vez desde el celular en la misma red y, si pide verificación, resolvela en la app. La sesión se reutiliza desde `ig_session.json`, así hay menos logins.

### Chat
- **Twitch:** se conecta anónimo (solo lectura) por el IRC WebSocket oficial `wss://irc-ws.chat.twitch.tv`. El login anónimo `justinfan` no figura en la doc oficial (la doc pide token), pero funciona y está probado.
- **Kick:** no hay forma oficial de leer el chat sin un servidor público: la API oficial de Kick solo manda el chat por webhooks a una URL pública. Se usa el mismo websocket (Pusher) que usa la web de Kick, y eso **no es oficial**.
  - Kick rota la key de Pusher. La key vieja `32cbd69e…` dejó de funcionar en 2026. Por eso, en cada conexión el programa pide la key actual a `web.kick.com/api/v1/realtime/connection`, el mismo endpoint que usa la web de Kick.
  - Si ese pedido falla, usa la última key conocida (`34bf7a0f…`, cluster `us2`, probada el 13/11/2026).
  - Si querés fijarla a mano, completá `kick_pusher_key` / `kick_pusher_cluster`. Lo recomendado es dejarla vacía.
  - La web de Kick también ofrece Centrifugo como proveedor. Si algún día deja de ofrecer Pusher, este lector va a dejar de funcionar y avisará en el log. El resto del programa sigue andando, y la **grabación** desde Kick (streamlink) no depende de esto.

## Funcionamiento 24/7

### run.bat
Relanza `main.py` si se cae: espera 15 s y lo vuelve a abrir, y anota cada caída en `logs\run_bat.log`. No relanza si el error es de configuración (código 2), si ya hay otra instancia corriendo (código 3) o si saliste con Ctrl+C.

Dentro del programa, cada tarea (grabador, chats, detector, cortador, analizador, publicador) corre supervisada. Si una falla, se reinicia sola con espera creciente y no tira a las demás. **Ctrl+C** apaga todo de forma limpia: mata streamlink y ffmpeg, y espera a que termine una subida o transcripción que esté en curso.

### Programador de tareas (arranque automático)
1. Abrí el **Programador de tareas** y elegí **Crear tarea** (no "tarea básica").
2. **General:** nombre "Stream Clipper", con "Ejecutar solo cuando el usuario haya iniciado sesión". Así ve tu PATH y tu carpeta de usuario.
3. **Desencadenadores → Nuevo:** "Al iniciar sesión" (tu usuario). Opcional: retrasar 1 minuto.
4. **Acciones → Nuevo:** programa `D:\Trabajo\clipper\run.bat`, y en **"Iniciar en"** `D:\Trabajo\clipper`.
5. **Condiciones:** destildá "Iniciar la tarea solo si el equipo está conectado a la corriente alterna".
6. **Configuración:** destildá "Detener la tarea si se ejecuta durante más de…". En "Si la tarea ya se está ejecutando", elegí "No iniciar una nueva instancia".

También se puede crear por consola:
```
schtasks /Create /TN "Stream Clipper" /TR "\"D:\Trabajo\clipper\run.bat\"" /SC ONLOGON /RL LIMITED
```
(después ajustá "Iniciar en" y las condiciones desde la interfaz).

Evitá que la PC se suspenda: `powercfg /change standby-timeout-ac 0`.

### Logs
En consola y en `logs\clipper.log`. El archivo rota a los 5 MB y guarda los últimos 5. Las claves y tokens se reemplazan por `***`.

## Panel web y aprobación manual
- `http://127.0.0.1:8787`: solo se puede abrir desde esta PC (escucha en 127.0.0.1 y rechaza otros hosts).
- Filtrá por categoría, reproducí los clips y mirá el estado de cada subida.
- **Descartar** borra el archivo y el clip no se sube nunca.
- Con `upload.require_approval: true`, **nada se sube sin que lo apruebes** en el panel. En ese modo no se usa `min_score`: si lo aprobaste, se sube.
- El panel también se puede abrir solo: `.venv\Scripts\python panel.py`.

## Subtítulos
Con `subtitles: true`, Whisper saca timestamps por palabra y se queman en el video, en grupos de hasta 4 palabras, en mayúsculas y en el tercio inferior. Se agrega un re-encode de unos segundos por clip.

## Ajustes útiles (`config.yaml`)
- `detector.min_msgs` / `detector.zscore`: sensibilidad. Si tu chat es chico, bajá `min_msgs` (por ejemplo a 4).
- `detector.manual_users`: quiénes pueden usar `!clip` (vacío = cualquiera, con 20 s de cooldown).
- `upload.min_score`, `max_per_day_per_platform`, `min_gap_minutes`, `retry_minutes`, `max_attempts`.
- `whisper_model`: `tiny` / `base` / `small` / `medium`. Más grande es más preciso y más lento (corre en CPU).

## Cola de publicación
- Elige el clip con mejor score, y entre empates el más viejo, que todavía no se haya subido a esa plataforma.
- Respeta el tope diario (se reinicia a medianoche), la separación mínima entre subidas y una espera tras cada error. Hace como máximo `max_attempts` intentos por clip y plataforma.
- **Nunca repite:** si el programa se corta en medio de una subida, esa subida queda como `interrupted` y no se reintenta (pudo haberse publicado). Revisala a mano en la plataforma.

## Limitaciones (verificadas en la doc oficial el 13/11/2026)
- **Cuota de YouTube:** los 1600 unidades por subida (≈6 por día) quedaron viejos. Desde el 04/12/2025, una subida cuesta ~100 unidades, y desde el 01/06/2026 `videos.insert` tiene su **propio balde de cuota: 100 subidas por día** por proyecto, aparte de las 10.000 unidades del resto de la API. El tope de `max_per_day_per_platform: 6` queda como límite propio para no spamear: subilo si querés.
- **YouTube sin auditar:** desde el **02/11/2026**, los videos subidos desde proyectos no verificados **ya no quedan forzados a privado** (la regla de julio de 2020 se revirtió). Igual, YouTube puede ponerlos en privado según la configuración de tu cuenta.
- **TikTok sin auditar:** todo lo que publica una app no auditada queda en **privado** (`SELF_ONLY`), y además **la cuenta tiene que ser privada**. Si no, la API responde `unaudited_client_can_only_post_to_private_accounts`. Para publicar en público hay que pedir la auditoría de la app y después cambiar `tiktok_privacy: PUBLIC_TO_EVERYONE`. Antes de cada subida, el programa consulta qué privacidades permite tu cuenta y avisa si la configurada no está permitida.
- **Instagram:** usa instagrapi (no oficial) y tiene **riesgo de bloqueo**.
- **Kick:** el chat usa el websocket no oficial de la web de Kick. Puede romperse si Kick lo cambia (ver **Chat**).
- La detección depende del chat. Si el stream está offline, los picos se ignoran porque no hay video en el buffer.
- Todo corre en CPU. En la PC donde se probó: corte vertical de 42 s ≈ 10 s y Whisper `small` ≈ 12 s por clip.
- Los clips quedan en `clips/` hasta que los descartes desde el panel: revisá el espacio en disco de vez en cuando.

## Tests
```
.venv\Scripts\python -m pytest -q
```
Cubren: detector (picos y cooldown), selección de segmentos, cola de publicación (orden, tope diario, separación, reintentos, no repetir, aprobación manual), corte vertical con ffmpeg real (ffprobe confirma 1080x1920), subtítulos quemados, grabador con un stream falso (incluido el apagado de subprocesos), chat de Twitch y Kick contra servidores WebSocket simulados (con reconexión), clasificador con la API de Anthropic simulada, uploaders con HTTP simulado (YouTube, TikTok, Instagram), panel web y que los logs no muestren secretos.
