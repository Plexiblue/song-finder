"""Hält yt-dlp aktuell, ohne die App neu installieren zu müssen.

YouTube ändert regelmäßig Details, die yt-dlp dann nachzieht. Die App lädt deshalb beim Start
die neueste yt-dlp-Version von PyPI (reiner Python-Code als .whl/ZIP) in den Datenordner und
importiert sie von dort. Klappt etwas nicht, wird die mitgelieferte Version verwendet.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import urllib.request
import zipfile
from importlib import metadata
from pathlib import Path

from .paths import data_dir

PACKAGES = {"yt-dlp": "yt_dlp", "yt-dlp-ejs": "yt_dlp_ejs"}
_done = threading.Event()
_active: list[str] = []
status: dict = {"checked": False, "versions": {}, "error": None}


def _vkey(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v))


def _bundled_version(dist: str) -> str:
    try:
        return metadata.version(dist)
    except Exception:
        return "0"


def _cache() -> Path:
    p = data_dir() / "updates"
    p.mkdir(exist_ok=True)
    return p


def _valid_wheel(path: Path, module: str) -> bool:
    try:
        with zipfile.ZipFile(path) as z:
            return z.testzip() is None and f"{module}/__init__.py" in z.namelist()
    except Exception:
        return False


def _newest_cached(dist: str, module: str) -> tuple[str, Path] | None:
    best = None
    for whl in _cache().glob(f"{module}-*-py3-none-any.whl"):
        ver = whl.name.split("-")[1]
        if best is None or _vkey(ver) > _vkey(best[0]):
            best = (ver, whl)
    return best


def _download_latest(dist: str, module: str, timeout: float) -> None:
    with urllib.request.urlopen(f"https://pypi.org/pypi/{dist}/json", timeout=timeout) as r:
        info = json.load(r)
    version = info["info"]["version"]
    wheel = next((u for u in info["urls"] if u["filename"].endswith("py3-none-any.whl")), None)
    if not wheel:
        return
    target = _cache() / wheel["filename"]
    if target.exists() and _valid_wheel(target, module):
        return
    if _vkey(version) <= _vkey(_bundled_version(dist)):
        return
    tmp = target.with_suffix(".part")
    with urllib.request.urlopen(wheel["url"], timeout=timeout * 3) as r, open(tmp, "wb") as f:
        f.write(r.read())
    if _valid_wheel(tmp, module):
        tmp.replace(target)
        for old in _cache().glob(f"{module}-*-py3-none-any.whl"):  # alte Versionen aufräumen
            if old != target:
                old.unlink(missing_ok=True)
    else:
        tmp.unlink(missing_ok=True)


def _run(timeout: float) -> None:
    try:
        for dist, module in PACKAGES.items():
            try:
                _download_latest(dist, module, timeout)
            except Exception as exc:  # offline o.ä. – kein Problem
                status["error"] = f"{dist}: {exc}"
        for dist, module in PACKAGES.items():
            cached = _newest_cached(dist, module)
            if cached and _vkey(cached[0]) > _vkey(_bundled_version(dist)):
                if module in sys.modules:  # schon importiert → zu spät, beim nächsten Start
                    continue
                sys.path.insert(0, str(cached[1]))
                _active.append(str(cached[1]))
                status["versions"][dist] = cached[0]
            else:
                status["versions"][dist] = _bundled_version(dist)
    finally:
        status["checked"] = True
        _done.set()


_started = threading.Event()


def start(timeout: float = 6.0) -> None:
    _started.set()
    threading.Thread(target=_run, args=(timeout,), daemon=True, name="yt-dlp-update").start()


def wait(max_seconds: float = 20.0) -> None:
    """Vor dem ersten yt-dlp-Import aufrufen (wartet nur, wenn ein Update-Check läuft)."""
    if _started.is_set():
        _done.wait(max_seconds)


def disable() -> None:
    """Fällt auf die mitgelieferte Version zurück (z. B. falls das Update nicht importierbar ist)."""
    for p in _active:
        if p in sys.path:
            sys.path.remove(p)
    for name in list(sys.modules):
        if name.split(".")[0] in PACKAGES.values():
            del sys.modules[name]
    _active.clear()
