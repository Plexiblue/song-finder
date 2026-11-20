"""Lokale Web-Oberfläche:  python -m songfinder  →  http://127.0.0.1:5123"""
from __future__ import annotations

import json
import secrets
import threading
import traceback
import uuid
import webbrowser

from flask import Flask, Response, abort, jsonify, request

from . import __version__, audio
from .cli import keys_from_env
from .errors import friendly_error
from .export import to_csv, to_text
from .pipeline import Options, analyze

app = Flask(__name__, static_folder=None)
JOBS: dict[str, dict] = {}

# Zufälliger Schlüssel pro App-Start. Nur das eigene App-Fenster kennt ihn (steht in dessen
# Adresse). Webseiten im Browser können die App dadurch nicht fernsteuern.
ACCESS_KEY = secrets.token_urlsafe(24)
_LOCAL_HOSTS = {"127.0.0.1", "localhost"}
SETTING_KEYS = {"audd_token", "acr_host", "acr_key", "acr_secret", "cookies", "mode", "ai",
                "shazam", "seen_version"}


@app.before_request
def _guard():
    # Schutz gegen „DNS-Rebinding“: nur Anfragen an 127.0.0.1/localhost annehmen
    if (request.host or "").rsplit(":", 1)[0] not in _LOCAL_HOSTS:
        abort(403)
    if request.path.startswith("/api/"):
        key = request.headers.get("X-SF-Key") or request.args.get("k") or ""
        if not secrets.compare_digest(key, ACCESS_KEY):
            abort(403)


@app.after_request
def _headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _settings_file():
    from .paths import data_dir

    return data_dir() / "settings.json"


@app.get("/api/settings")
def get_settings():
    try:
        return jsonify(json.loads(_settings_file().read_text(encoding="utf-8")))
    except Exception:
        return jsonify({})


@app.post("/api/settings")
def save_settings():
    import os

    data = request.get_json(force=True, silent=True) or {}
    clean = {k: str(v)[:500] for k, v in data.items() if k in SETTING_KEYS and v is not None}
    f = _settings_file()
    tmp = f.with_suffix(".tmp")
    # Datei nur für den eigenen Benutzer lesbar (enthält ggf. den AudD-Token)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(clean, fh)
    os.replace(tmp, f)
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass
    return jsonify({"ok": True})


def _run_job(job_id: str, source: str, opts: Options) -> None:
    job = JOBS[job_id]

    def progress(pct: int, msg: str) -> None:
        job["pct"], job["msg"] = pct, msg

    def partial(data: dict) -> None:
        job["partial"] = data

    try:
        result = analyze(source, opts, progress, on_partial=partial)
        job["audio_path"] = result.pop("_playback", None)  # lokaler Pfad bleibt auf dem Server
        result["playable"] = bool(job["audio_path"])
        job["result"] = result
        job["state"], job["pct"], job["msg"] = "done", 100, "Fertig"
    except Exception as exc:
        traceback.print_exc()
        job["state"], job["error"] = "error", friendly_error(exc)


@app.get("/")
def index():
    # pkgutil funktioniert auch, wenn der Code aus einem Update-ZIP geladen wurde
    import pkgutil

    html = pkgutil.get_data("songfinder", "static/index.html")
    return Response(html, mimetype="text/html; charset=utf-8")


@app.get("/api/status")
def status():
    from . import updater
    from .paths import FROZEN, log_file

    return jsonify({
        "version": __version__,
        "ai_available": audio.demucs_available(),
        "app": FROZEN,
        "ytdlp": updater.status.get("versions", {}).get("yt-dlp"),
        "log": str(log_file()) if FROZEN else None,
        "update": _update_info(),
    })


def _update_info() -> dict:
    import os

    try:
        return json.loads(os.environ.get("SONGFINDER_UPDATE_INFO") or "{}")
    except Exception:
        return {}


@app.post("/api/audd/check")
def audd_token_check():
    import asyncio

    from .recognize import audd_check

    token = ((request.get_json(force=True, silent=True) or {}).get("token") or "").strip()
    if not token:
        return jsonify({"error": "Bitte zuerst einen Token eintragen."}), 400
    return jsonify(asyncio.run(audd_check(token)))


@app.post("/api/analyze")
def start():
    data = request.get_json(force=True) or {}
    source = (data.get("url") or "").strip()
    if not source:
        return jsonify({"error": "Bitte einen Link einfügen."}), 400
    keys = keys_from_env()
    keys.update({k: v.strip() for k, v in (data.get("keys") or {}).items() if v and v.strip()})
    opts = Options(
        mode=data.get("mode", "normal"),
        use_ai=data.get("ai", "auto"),
        use_side=bool(data.get("side", True)),
        use_shazam=bool(data.get("shazam", False)),
        keys=keys,
        cookies_browser=data.get("cookies") or None,
    )
    job_id = uuid.uuid4().hex[:10]
    JOBS[job_id] = {"state": "running", "pct": 0, "msg": "Starte …", "source": source}
    threading.Thread(target=_run_job, args=(job_id, source, opts), daemon=True).start()
    return jsonify({"job": job_id})


@app.get("/api/job/<job_id>")
def job(job_id: str):
    j = JOBS.get(job_id)
    if not j:
        return jsonify({"error": "Unbekannter Auftrag"}), 404
    return jsonify({k: v for k, v in j.items() if k not in ("source", "audio_path")})


def export_body(job_id: str, fmt: str) -> tuple[str, str, str] | None:
    """Gibt (Dateiname, Inhalt, MIME-Typ) zurück."""
    j = JOBS.get(job_id)
    if not j or "result" not in j:
        return None
    r = j["result"]
    name = "songs-" + (r["video"].get("id") or job_id)
    if fmt == "csv":
        return f"{name}.csv", to_csv(r), "text/csv; charset=utf-8"
    if fmt == "txt":
        return f"{name}.txt", to_text(r), "text/plain; charset=utf-8"
    return f"{name}.json", json.dumps(r, ensure_ascii=False, indent=2), "application/json"


@app.get("/api/job/<job_id>/<fmt>")
def export(job_id: str, fmt: str):
    if fmt == "audio":  # Tonspur zum Anhören (mit Spulen dank Range-Anfragen)
        from flask import send_file

        path = (JOBS.get(job_id) or {}).get("audio_path")
        if not path:
            abort(404)
        return send_file(path, mimetype="audio/mp4", conditional=True)
    out = export_body(job_id, fmt)
    if not out:
        return jsonify({"error": "Kein Ergebnis"}), 404
    name, body, mime = out
    return Response(body, mimetype=mime, headers={"Content-Disposition": f'attachment; filename="{name}"'})


def quiet_server() -> None:
    import logging

    import flask.cli

    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    flask.cli.show_server_banner = lambda *a, **k: None


def run(host: str = "127.0.0.1", port: int = 5123, open_browser: bool = True) -> None:
    url = f"http://{host}:{port}/?k={ACCESS_KEY}"
    print(f"\n  Song Finder läuft auf {url}\n  (Fenster offen lassen, Beenden mit Strg+C)\n")
    quiet_server()
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    run()
