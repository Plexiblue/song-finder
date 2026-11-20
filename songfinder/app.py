"""Desktop-App: öffnet die Oberfläche in einem eigenen Fenster (kein Browser, kein Terminal)."""
from __future__ import annotations

import sys
import threading
import traceback
import webbrowser
from pathlib import Path


def _redirect_output() -> None:
    """In der gepackten App gibt es keine Konsole – Meldungen landen in einer Logdatei."""
    from .paths import FROZEN, log_file

    if FROZEN or sys.stdout is None or sys.stderr is None:
        try:
            f = open(log_file(), "a", encoding="utf-8", buffering=1)  # noqa: SIM115
            sys.stdout = sys.stderr = f
        except Exception:
            pass


class Api:
    """Wird im Fenster als ``window.pywebview.api`` bereitgestellt."""

    def __init__(self) -> None:
        self.window = None

    def _dialog(self, kind: str, **kw):
        import webview

        const = getattr(getattr(webview, "FileDialog", None), kind, None)
        if const is None:  # ältere pywebview-Versionen
            const = getattr(webview, f"{kind}_DIALOG")
        res = self.window.create_file_dialog(const, **kw)
        if not res:
            return None
        return res if isinstance(res, str) else res[0]

    def save_export(self, job_id: str, fmt: str) -> str | None:
        from .web import export_body

        out = export_body(job_id, fmt)
        if not out:
            return None
        name, body, _ = out
        path = self._dialog("SAVE", save_filename=name, directory=str(Path.home() / "Downloads"))
        if not path:
            return None
        Path(path).write_text(body, encoding="utf-8")
        return path

    def pick_file(self) -> str | None:
        return self._dialog(
            "OPEN",
            file_types=("Video/Audio (*.mp4;*.mov;*.mkv;*.webm;*.m4a;*.mp3;*.wav;*.aac;*.flac)",
                        "Alle Dateien (*.*)"),
        )


def _use_bundled_certificates() -> None:
    """Die gepackte App findet die macOS-Zertifikate nicht von selbst (Python nutzt nicht den
    Schlüsselbund). Ohne das scheitern alle HTTPS-Verbindungen, die nicht – wie yt-dlp –
    selbst certifi verwenden, z. B. die zu Shazam."""
    import os

    try:
        import certifi

        if not os.environ.get("SSL_CERT_FILE") or not os.path.isfile(os.environ["SSL_CERT_FILE"]):
            os.environ["SSL_CERT_FILE"] = certifi.where()
    except Exception:
        traceback.print_exc()


def main() -> None:
    _use_bundled_certificates()
    if len(sys.argv) >= 3 and sys.argv[1] == "--selftest":
        sys.exit(selftest(sys.argv[2]))
    _redirect_output()
    from werkzeug.serving import make_server

    from . import updater
    from .web import app, quiet_server

    updater.start()
    quiet_server()
    server = make_server("127.0.0.1", 0, app, threaded=True)
    from .web import ACCESS_KEY

    url = f"http://127.0.0.1:{server.server_port}/?k={ACCESS_KEY}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Song Finder gestartet auf Port {server.server_port}")  # Schlüssel nicht ins Protokoll

    try:
        import webview

        try:
            webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
            webview.settings["ALLOW_DOWNLOADS"] = True
        except Exception:
            pass
        api = Api()
        api.window = webview.create_window(
            "Song Finder", url, js_api=api, width=1060, height=820, min_size=(420, 560),
            background_color="#f6f6f4",
        )
        webview.start()
    except Exception:
        # Kein eingebettetes Browserfenster möglich → normaler Browser
        traceback.print_exc()
        webbrowser.open(url)
        threading.Event().wait()
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()


def selftest(out_path: str) -> int:
    """Prüft die gepackte App ohne Fenster (für den automatischen Build)."""
    import subprocess
    import tempfile

    lines: list[str] = []
    ok = True

    def check(name: str, fn) -> None:
        nonlocal ok
        try:
            lines.append(f"OK   {name}: {fn()}")
        except Exception as exc:
            ok = False
            lines.append(f"FAIL {name}: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")

    def ffmpeg():
        from .audio import ffmpeg_exe

        exe = ffmpeg_exe()
        v = subprocess.run([exe, "-version"], capture_output=True, text=True).stdout.split("\n")[0]
        return f"{exe} | {v}"

    def deno():
        from .download import _js_runtimes

        path = _js_runtimes().get("deno", {}).get("path")
        v = subprocess.run([path, "--version"], capture_output=True, text=True).stdout.split("\n")[0]
        return f"{path} | {v}"

    def signature():
        import asyncio

        import numpy as np
        import soundfile as sf
        from shazamio_core import Recognizer

        from . import audio

        tmp = Path(tempfile.mkdtemp())
        t = np.arange(44100 * 20) / 44100
        sig = np.stack([np.sin(2 * np.pi * 440 * t), np.sin(2 * np.pi * 550 * t)], 1) * 0.4
        sf.write(tmp / "t.wav", sig, 44100)
        src = audio.AudioSource(audio.to_wav(tmp / "t.wav", tmp / "s.wav"))
        v = src.variants(audio.plan_windows(src.duration, 9)[0], ["mix", "side"])
        async def make_signature():
            return await Recognizer(segment_duration_seconds=12).recognize_bytes(
                value=audio.wav_bytes(v["mix"]))

        s = asyncio.run(make_signature())
        return f"Varianten {sorted(v)}, Signatur {s.signature.samples} samples"

    def certificates():
        import os
        import ssl

        paths = ssl.get_default_verify_paths()
        n = ssl.create_default_context().cert_store_stats()["x509_ca"]
        if n == 0:
            raise RuntimeError("keine CA-Zertifikate geladen")
        return (f"{n} CA-Zertifikate, SSL_CERT_FILE={os.environ.get('SSL_CERT_FILE')}, "
                f"OpenSSL-Standard={paths.openssl_cafile} (vorhanden: {os.path.isfile(paths.openssl_cafile)})")

    def shazam_online():
        import asyncio

        import numpy as np

        from . import audio
        from .recognize import ShazamRecognizer

        t = np.arange(audio.SR * 12) / audio.SR
        clip = (np.sin(2 * np.pi * 440 * t) * 0.4).astype(np.float32)

        async def ask():
            rec = ShazamRecognizer()
            try:
                return await asyncio.wait_for(rec.recognize(audio.wav_bytes(clip)), 60)
            finally:
                await rec.close()

        m = asyncio.run(ask())
        return f"Shazam erreichbar, Antwort: {m.title + ' – ' + m.artist if m else 'kein Treffer (erwartet)'}"

    def update():
        from . import updater

        updater._run(5.0)
        return updater.status

    def ytdlp():
        import yt_dlp
        import yt_dlp_ejs  # noqa: F401

        return f"{yt_dlp.version.__version__} aus {Path(yt_dlp.__file__).parent}"

    def web():
        from .web import ACCESS_KEY, app

        c = app.test_client()
        assert c.get("/").status_code == 200
        assert c.get("/api/status").status_code == 403, "API ohne Schlüssel erreichbar"
        assert c.get("/api/status", headers={"X-SF-Key": ACCESS_KEY},
                     base_url="http://evil.example").status_code == 403, "fremder Host akzeptiert"
        return c.get("/api/status", headers={"X-SF-Key": ACCESS_KEY}).get_json()

    def gui():
        import webview

        return getattr(webview, "__version__", "importiert")

    for name, fn in [("ffmpeg", ffmpeg), ("deno", deno), ("shazam-signatur", signature),
                     ("zertifikate", certificates),
                     ("shazam-online", shazam_online), ("update", update), ("yt-dlp", ytdlp), ("oberflaeche", web), ("pywebview", gui)]:
        check(name, fn)
    lines.append("SELFTEST " + ("BESTANDEN" if ok else "FEHLGESCHLAGEN"))
    Path(out_path).write_text("\n".join(lines), encoding="utf-8")
    return 0 if ok else 1
