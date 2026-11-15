"""Panel web local (solo 127.0.0.1): ver clips por categoría, reproducir, aprobar y descartar.
Arranca solo con main.py, o aparte con: python panel.py"""
import logging
import threading
from pathlib import Path

from flask import Flask, abort, jsonify, render_template_string, request, send_file
from werkzeug.serving import make_server

import db
from config import CATEGORIES, CFG

log = logging.getLogger("panel")
app = Flask(__name__)

PAGE = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Clipper</title>
<style>
body{font-family:system-ui,sans-serif;margin:0;background:#111;color:#eee}
header{padding:12px 16px;background:#1c1c1c;position:sticky;top:0;z-index:1}
a{color:#9cf;text-decoration:none;margin-right:12px}a.on{color:#fff;font-weight:bold}
.note{font-size:13px;color:#fc6;margin-top:6px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px;padding:16px}
.card{background:#1c1c1c;border-radius:8px;padding:8px}
video{width:100%;aspect-ratio:9/16;background:#000;border-radius:6px}
.t{font-weight:600;margin:6px 0 2px}.m{font-size:12px;color:#aaa}
button{margin:6px 6px 0 0;padding:6px 10px;border:0;border-radius:5px;cursor:pointer}
.ok{background:#2a7}.no{background:#a33;color:#fff}.st{font-size:12px;margin-top:4px}
</style></head><body>
<header>
<a href="/" class="{{ 'on' if not cat }}">Todos</a>
{% for c in cats %}<a href="/?cat={{c}}" class="{{ 'on' if c==cat }}">{{c}}</a>{% endfor %}
<div class="note">{% if approval %}Modo aprobación manual: solo se suben los clips aprobados.
{% else %}Subida automática (score ≥ {{min_score}}). Aprobar no es necesario.{% endif %}</div>
</header>
<div class="grid">
{% for c in clips %}
<div class="card" id="c{{c.id}}">
  <video src="/video/{{c.id}}" controls preload="none"></video>
  <div class="t">{{c.title}}</div>
  <div class="m">{{c.category}} · score {{c.score}} · {{c.when}} · <b>{{c.status}}</b></div>
  <div class="st">{% for u in c.ups %}{{u.platform}}: {{u.status}}{% if u.status=='ok' %} ({{u.info}}){% endif %}<br>{% endfor %}</div>
  {% if c.status!='approved' %}<button class="ok" onclick="act({{c.id}},'approve')">Aprobar</button>{% endif %}
  <button class="no" onclick="act({{c.id}},'discard')">Descartar</button>
</div>
{% else %}<p>No hay clips todavía.</p>{% endfor %}
</div>
<script>
async function act(id, a){
  if(a==='discard' && !confirm('¿Descartar y borrar el archivo del clip?')) return;
  const r = await fetch('/api/clips/'+id+'/'+a, {method:'POST', headers:{'X-Clipper':'1'}});
  if(r.ok) location.reload(); else alert('Error '+r.status);
}
</script></body></html>"""


@app.before_request
def only_local():
    # evita DNS rebinding: solo se acepta el host local
    port = CFG["panel"]["port"]
    if request.host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
        abort(403)
    # evita CSRF: un sitio externo no puede mandar este header sin preflight CORS
    if request.method == "POST" and request.headers.get("X-Clipper") != "1":
        abort(403)


@app.get("/")
def index():
    import time
    cat = request.args.get("cat")
    cat = cat if cat in CATEGORIES else None
    clips = db.list_clips(cat)
    for c in clips:
        c["when"] = time.strftime("%d/%m %H:%M", time.localtime(c["created"]))
        c["ups"] = db.uploads_for(c["id"])
    return render_template_string(PAGE, clips=clips, cats=CATEGORIES, cat=cat,
                                  approval=CFG["upload"]["require_approval"],
                                  min_score=CFG["upload"]["min_score"])


@app.get("/video/<int:clip_id>")
def video(clip_id):
    c = db.get_clip(clip_id)
    if not c or not Path(c["path"]).exists():
        abort(404)
    return send_file(c["path"], mimetype="video/mp4", conditional=True)  # soporta Range (seek)


@app.post("/api/clips/<int:clip_id>/<action>")
def clip_action(clip_id, action):
    c = db.get_clip(clip_id)
    if not c:
        abort(404)
    if action == "approve":
        db.set_status(clip_id, "approved")
    elif action == "discard":
        db.set_status(clip_id, "discarded")
        try:
            Path(c["path"]).unlink(missing_ok=True)
        except OSError as e:  # en Windows falla si el archivo está abierto
            log.warning("no se pudo borrar %s: %s", c["path"], e)
    else:
        abort(400)
    log.info("clip %s: %s", clip_id, action)
    return jsonify(ok=True)


class Panel:
    """Servidor en un hilo aparte; stop() lo apaga limpio."""

    def __init__(self, port=None):
        self.srv = make_server("127.0.0.1", port or CFG["panel"]["port"], app, threaded=True)
        self.th = threading.Thread(target=self.srv.serve_forever, name="panel", daemon=True)

    def start(self):
        self.th.start()
        log.info("panel en http://127.0.0.1:%s", self.srv.server_port)
        return self

    def stop(self):
        self.srv.shutdown()


if __name__ == "__main__":
    from config import setup_logging
    setup_logging()
    p = Panel().start()
    try:
        p.th.join()
    except KeyboardInterrupt:
        p.stop()
