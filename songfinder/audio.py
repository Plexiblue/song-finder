"""Audio dekodieren, in Fenster schneiden und Varianten für die Erkennung erzeugen.

Varianten pro Fenster:
  * ``music`` – KI-getrennte Musikspur ohne Stimme (Demucs, optional)
  * ``mix``   – Originalton (Mono)
  * ``side``  – Links minus Rechts. Sprecher sind fast immer mittig gemischt,
                Musik ist stereo; die Differenz entfernt also die Stimme
                (kostenlos, ohne KI).
"""
from __future__ import annotations

import io
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import soundfile as sf

SR = 16_000  # Shazam arbeitet intern mit 16 kHz
WINDOW_SECONDS = 12  # Länge, die Shazam-Clients senden

ProgressFn = Callable[[float, str], None]


def ffmpeg_exe() -> str:
    from .paths import bundled_bin

    exe = bundled_bin("ffmpeg") or shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "ffmpeg wurde nicht gefunden. Bitte 'pip install imageio-ffmpeg' ausführen."
        ) from exc


def to_wav(src: Path, dst: Path, sr: int = SR, channels: int = 2) -> Path:
    """Beliebige Audio-/Videodatei in WAV (16 bit) umwandeln."""
    cmd = [
        ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src), "-vn", "-ac", str(channels), "-ar", str(sr),
        "-c:a", "pcm_s16le", str(dst),
    ]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # Windows: kein schwarzes Fenster
    proc = subprocess.run(cmd, capture_output=True, text=True, creationflags=flags,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg konnte die Datei nicht lesen: {proc.stderr.strip()[-400:]}")
    return dst


PLAYABLE = {".m4a", ".mp4", ".mov", ".mp3", ".aac", ".wav"}


def keep_for_playback(src: Path, cache_dir: Path, keep: int = 5) -> Path:
    """Legt die Tonspur zum Anhören in der App ab (nur die letzten ``keep`` Analysen)."""
    import shutil as _sh
    import uuid

    cache_dir.mkdir(parents=True, exist_ok=True)
    dst = cache_dir / f"{uuid.uuid4().hex[:12]}.m4a"
    if src.suffix.lower() == ".m4a":
        _sh.copy2(src, dst)
    else:
        cmd = [ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
               "-vn", "-ac", "2", "-c:a", "aac", "-b:a", "128k", str(dst)]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(cmd, capture_output=True, text=True, creationflags=flags,
                              encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg (Wiedergabe): {proc.stderr.strip()[-300:]}")
    old = sorted(cache_dir.glob("*.m4a"), key=lambda p: p.stat().st_mtime, reverse=True)[keep:]
    for p in old:
        p.unlink(missing_ok=True)
    return dst


def to_mp3(src: Path, dst: Path) -> Path:
    """Kompakte Mono-MP3 für den Upload zu AudD (15 min ≈ 7 MB)."""
    cmd = [ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
           "-vn", "-ac", "1", "-ar", "22050", "-b:a", "64k", str(dst)]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(cmd, capture_output=True, text=True, creationflags=flags,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg (MP3): {proc.stderr.strip()[-300:]}")
    return dst


def wav_bytes(samples: np.ndarray, sr: int = SR) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, samples, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def _normalize(x: np.ndarray) -> np.ndarray:
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    if peak < 1e-6:
        return x
    return (x * (0.89 / peak)).astype(np.float32)


def rms_db(x: np.ndarray) -> float:
    if not x.size:
        return -120.0
    return 20 * np.log10(float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) + 1e-9)


@dataclass
class Window:
    start: float  # Sekunden im Video
    end: float


def plan_windows(duration: float, step: float, window: float = WINDOW_SECONDS) -> list[Window]:
    if duration <= window:
        return [Window(0.0, duration)]
    starts = list(np.arange(0.0, duration - window + 0.01, step))
    if duration - window - starts[-1] > step / 2:  # Ende nicht verpassen
        starts.append(duration - window)
    return [Window(float(s), float(min(s + window, duration))) for s in starts]


class AudioSource:
    """Zugriff auf Fenster des Originals (und optional der Musikspur)."""

    def __init__(self, stereo_wav: Path, music_wav: Path | None = None):
        self.stereo_wav = stereo_wav
        self.music_wav = music_wav
        info = sf.info(str(stereo_wav))
        self.duration = info.frames / info.samplerate
        self.channels = info.channels

    def _read(self, path: Path, w: Window) -> np.ndarray:
        data, _ = sf.read(
            str(path), start=int(w.start * SR), stop=int(w.end * SR), dtype="float32", always_2d=True
        )
        return data

    def variants(self, w: Window, order: list[str]) -> dict[str, np.ndarray]:
        """Liefert die angefragten Varianten (Mono, normalisiert). Stille wird ausgelassen."""
        out: dict[str, np.ndarray] = {}
        stereo = self._read(self.stereo_wav, w)
        mono = stereo.mean(axis=1)
        for name in order:
            if name == "mix":
                sig = mono
            elif name == "side":
                if stereo.shape[1] < 2:
                    continue
                side = (stereo[:, 0] - stereo[:, 1]) / 2
                # Nur sinnvoll, wenn die Musik wirklich stereo ist
                if rms_db(side) < -45 or rms_db(side) < rms_db(mono) - 24:
                    continue
                sig = side
            elif name == "music":
                if not self.music_wav:
                    continue
                sig = self._read(self.music_wav, w).mean(axis=1)
            else:
                continue
            if rms_db(sig) < -50:  # praktisch still
                continue
            out[name] = _normalize(sig)
        return out


# --------------------------------------------------------------------------- #
# KI-Stimmentrennung (optional)
# --------------------------------------------------------------------------- #

def demucs_available() -> bool:
    try:
        import demucs  # noqa: F401
        import torch  # noqa: F401

        return True
    except Exception:
        return False


def separate_music(src: Path, dst: Path, workdir: Path, progress: ProgressFn | None = None,
                   chunk_seconds: int = 120) -> Path:
    """Entfernt die Stimme mit Demucs (htdemucs) und schreibt die Musik als 16-kHz-Mono-WAV.

    Wird in Blöcken verarbeitet, damit auch lange Videos wenig Arbeitsspeicher brauchen.
    """
    import torch
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    try:
        import julius
    except ImportError:  # pragma: no cover - julius ist eine Demucs-Abhängigkeit
        julius = None

    model = get_model("htdemucs")
    model.eval()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device)
    model_sr = model.samplerate
    keep = [i for i, s in enumerate(model.sources) if s != "vocals"]

    hq = to_wav(src, workdir / "hq.wav", sr=model_sr, channels=2)
    info = sf.info(str(hq))
    total = info.frames
    block = chunk_seconds * model_sr

    with sf.SoundFile(str(dst), "w", samplerate=SR, channels=1, subtype="PCM_16") as out:
        for pos in range(0, total, block):
            data, _ = sf.read(str(hq), start=pos, stop=min(pos + block, total),
                              dtype="float32", always_2d=True)
            wav = torch.from_numpy(data.T.copy())  # (2, n)
            ref = wav.mean(0)
            mean, std = ref.mean(), ref.std() + 1e-8
            with torch.no_grad():
                sources = apply_model(model, ((wav - mean) / std)[None].to(device),
                                      split=True, overlap=0.25, progress=False)[0]
            sources = sources.cpu() * std + mean
            music = sources[keep].sum(0).mean(0)  # Mono
            if julius is not None:
                music = julius.resample_frac(music, model_sr, SR)
            else:
                idx = np.linspace(0, music.shape[-1] - 1, int(music.shape[-1] * SR / model_sr))
                music = torch.from_numpy(np.interp(idx, np.arange(music.shape[-1]), music.numpy()))
            out.write(np.clip(music.numpy(), -1, 1).astype(np.float32))
            if progress:
                progress(min(1.0, (pos + block) / total), "KI trennt Stimme und Musik …")

    hq.unlink(missing_ok=True)
    return dst
