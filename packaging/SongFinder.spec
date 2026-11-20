# PyInstaller-Bauplan für die Desktop-App.   Bauen:  pyinstaller packaging/SongFinder.spec
import os
import shutil
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent
IS_WIN, IS_MAC = sys.platform == "win32", sys.platform == "darwin"
SUFFIX = ".exe" if IS_WIN else ""

# Mitgelieferte Programme unter festem Namen in bin/ ablegen
stage = ROOT / "build" / "bin"
stage.mkdir(parents=True, exist_ok=True)
import imageio_ffmpeg
from deno import find_deno_bin

shutil.copy2(imageio_ffmpeg.get_ffmpeg_exe(), stage / f"ffmpeg{SUFFIX}")
shutil.copy2(find_deno_bin(), stage / f"deno{SUFFIX}")
for f in stage.iterdir():
    f.chmod(0o755)

datas = [(str(ROOT / "songfinder" / "static"), "songfinder/static")]
binaries = [(str(stage / f"ffmpeg{SUFFIX}"), "bin"), (str(stage / f"deno{SUFFIX}"), "bin")]
hidden = collect_submodules("songfinder") + collect_submodules("shazamio")
for pkg in ("yt_dlp_ejs", "shazamio", "shazamio_core", "webview", "certifi"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hidden += h
for dist in ("yt-dlp", "yt-dlp-ejs", "shazamio", "pywebview"):
    datas += copy_metadata(dist)

a = Analysis(
    [str(ROOT / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["imageio_ffmpeg", "torch", "torchaudio", "demucs", "tkinter", "matplotlib", "PyQt5", "PyQt6",
              "PySide2", "PySide6", "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
icon = str(ROOT / "packaging" / "icon.png")

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="SongFinder",
    console=False,          # kein Terminalfenster
    icon=icon,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="SongFinder", upx=False)

if IS_MAC:
    app = BUNDLE(
        coll,
        name="Song Finder.app",
        icon=icon,
        bundle_identifier="app.songfinder.desktop",
        info_plist={
            "CFBundleName": "Song Finder",
            "CFBundleDisplayName": "Song Finder",
            "CFBundleShortVersionString": os.environ.get("APP_VERSION", "1.0.0"),
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
