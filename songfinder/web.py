"""Lokale Web-Oberfläche:  python -m songfinder  →  http://127.0.0.1:5123"""
from __future__ import annotations

import json
import threading
import traceback
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from . import __version__, audio
from .cli import keys_from_env
from .errors import friendly_error
from .export import to_csv, to_text
from .pipeline import Options, analyze

STATIC = Path(__file__).parent / "static"
app = Flask(__name__, static_folder=None)
JOBS: dict[str, dict] = {}


def _run_job(job_id: str, source: str, opts: Options) -> None:
    job = JOBS[job_id]

    def progress(pct: int, msg: str) -> None:
        job["pct"], job["msg"] = pct, msg

    try:
        job["result"] = analyze(source, opts, progress)
        job["state"], job["pct"], job["msg"] = "done", 100, "Fertig"
    except Exception as exc:
        traceback.print_exc()
        job["state"], job["error"] = "error", friendly_error(exc)


@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html")


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
    })


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
    return jsonify({k: v for k, v in j.items() if k != "source"})


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
    url = f"http://{host}:{port}"
    print(f"\n  Song Finder läuft auf {url}\n  (Fenster offen lassen, Beenden mit Strg+C)\n")
    quiet_server()
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    run()
