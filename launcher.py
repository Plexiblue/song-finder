"""Einstiegspunkt der Desktop-App – mit automatischem Code-Update.

Ablauf beim Start:
  1. Kurz (max. ~5 s) prüfen, ob es neueren Programmcode gibt (update.json im neuesten Release).
  2. Falls ja und zur App passend: ZIP laden (~100 KB), Prüfsumme kontrollieren, im Datenordner ablegen.
  3. Neueste gültige Version (heruntergeladen oder eingebaut) starten.
  4. Schlägt der Start einer heruntergeladenen Version fehl, wird sie gesperrt und die eingebaute genommen.

Diese Datei nutzt bewusst nur die Python-Standardbibliothek und importiert ``songfinder`` erst,
nachdem feststeht, welche Version geladen wird.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import traceback
import urllib.request
import zipfile
from pathlib import Path

import appinfo

UPDATE_URL = os.environ.get(
    "SONGFINDER_UPDATE_URL",
    "https://github.com/Plexiblue/song-finder/releases/latest/download/update.json")


def _vkey(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "0"))


def _data_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    d = base / "SongFinder" / "code"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ssl_context():
    import ssl

    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _bad_versions(d: Path) -> set[str]:
    try:
        return set(json.loads((d / "bad.json").read_text()))
    except Exception:
        return set()


def _mark_bad(d: Path, version: str) -> None:
    bad = _bad_versions(d) | {version}
    (d / "bad.json").write_text(json.dumps(sorted(bad)))


def _cached(d: Path) -> list[tuple[str, Path]]:
    out = []
    for z in d.glob("songfinder-*.zip"):
        v = z.stem.split("-", 1)[1]
        out.append((v, z))
    return sorted(out, key=lambda x: _vkey(x[0]), reverse=True)


def check_for_update(d: Path, timeout: float = 5.0) -> dict:
    """Lädt neueren Code, falls vorhanden. Gibt Infos für die Oberfläche zurück."""
    info: dict = {}
    try:
        with urllib.request.urlopen(UPDATE_URL, timeout=timeout, context=_ssl_context()) as r:
            meta = json.load(r)
    except Exception as exc:
        info["check_error"] = f"{type(exc).__name__}: {exc}"
        return info
    latest = str(meta.get("version", "0"))
    info["latest"] = latest
    best_local = max([appinfo.VERSION] + [v for v, _ in _cached(d)], key=_vkey)
    if _vkey(latest) <= _vkey(best_local) or latest in _bad_versions(d):
        return info
    if int(meta.get("min_runtime", 1)) > appinfo.RUNTIME:
        # Neuer Code braucht neue mitgelieferte Programme → nur über neue App-Version
        info["full_update"] = {"version": latest, "page": meta.get("page")}
        return info
    try:
        with urllib.request.urlopen(meta["url"], timeout=timeout * 4, context=_ssl_context()) as r:
            data = r.read()
        if hashlib.sha256(data).hexdigest() != meta.get("sha256"):
            raise ValueError("Prüfsumme stimmt nicht")
        tmp = d / f"songfinder-{latest}.part"
        tmp.write_bytes(data)
        with zipfile.ZipFile(tmp) as z:
            if z.testzip() is not None or "songfinder/__init__.py" not in z.namelist():
                raise ValueError("ZIP unvollständig")
        tmp.replace(d / f"songfinder-{latest}.zip")
        for v, z in _cached(d)[2:]:  # nur die zwei neuesten behalten
            z.unlink(missing_ok=True)
    except Exception as exc:
        info["download_error"] = f"{type(exc).__name__}: {exc}"
    return info


def _purge_songfinder() -> None:
    for name in list(sys.modules):
        if name == "songfinder" or name.startswith("songfinder."):
            del sys.modules[name]


def main() -> None:
    selftest = len(sys.argv) >= 2 and sys.argv[1] == "--selftest"
    d = _data_dir()
    info = {} if selftest else check_for_update(d)

    chosen = None
    for v, z in ([] if selftest else _cached(d)):  # neueste zuerst, kaputte überspringen
        if _vkey(v) <= _vkey(appinfo.VERSION) or v in _bad_versions(d):
            continue
        sys.path.insert(0, str(z))
        try:
            import songfinder.app  # noqa: F401
            from songfinder import __version__
            if __version__ != v:
                raise ImportError(f"Version im ZIP ({__version__}) passt nicht")
            chosen = (v, z)
            info["running"], info["updated"] = v, True
            break
        except Exception:
            traceback.print_exc()
            _mark_bad(d, v)
            sys.path.remove(str(z))
            _purge_songfinder()

    if not chosen:
        info["running"] = appinfo.VERSION
    info["bundled"] = appinfo.VERSION
    os.environ["SONGFINDER_UPDATE_INFO"] = json.dumps(info)

    from songfinder.app import main as app_main

    if os.environ.get("SONGFINDER_DRYRUN"):  # nur für Tests: zeigen, was geladen würde
        import songfinder

        print(json.dumps({**info, "module": songfinder.__file__}))
        return
    try:
        app_main()
    except Exception:
        traceback.print_exc()
        if info.get("updated"):  # neue Version stürzt ab → sperren und eingebaute starten
            _mark_bad(d, info["running"])
            os.execv(sys.executable, [sys.executable, *sys.argv[1:]] if not getattr(sys, "frozen", False)
                     else [sys.executable])
        raise


if __name__ == "__main__":
    main()
