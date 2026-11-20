"""Erkennungsdienste: Shazam (kostenlos) sowie optional AudD und ACRCloud (API-Schlüssel)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import time
from dataclasses import asdict, dataclass, field

import aiohttp


@dataclass
class Match:
    title: str
    artist: str
    provider: str
    album: str | None = None
    label: str | None = None
    released: str | None = None
    isrc: str | None = None
    url: str | None = None
    apple_url: str | None = None
    cover: str | None = None
    skew: float | None = None
    song_offset: float | None = None  # Position im Originalsong (Sekunden)
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return normalize(self.title) + "|" + normalize(self.artist)

    def to_dict(self) -> dict:
        return asdict(self)


def normalize(text: str | None) -> str:
    text = (text or "").lower()
    text = re.sub(r"\((feat|ft|with)[^)]*\)|\[[^\]]*\]", "", text)
    text = re.sub(r"[^\w]+", " ", text)
    return " ".join(text.split())


class RateLimiter:
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            delay = self._last + self.min_interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


class Recognizer:
    name = "base"

    async def recognize(self, wav: bytes) -> Match | None:  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# Shazam (inoffizielle Schnittstelle über shazamio)
# --------------------------------------------------------------------------- #

class ShazamBlocked(RuntimeError):
    """Shazam lehnt trotz langer Pausen weiterhin ab (Rate-Limit für diese IP-Adresse)."""


class ShazamRecognizer(Recognizer):
    """Fragt Shazam strikt nacheinander und passt das Tempo an.

    Antwortet Shazam mit „zu viele Anfragen“ (HTTP 429), wird eine Pause eingelegt, die bei jeder
    weiteren Ablehnung länger wird, und das Grundtempo dauerhaft gedrosselt.
    """

    name = "Shazam"
    PAUSES = (15, 30, 60, 90, 120)  # Sekunden; danach gilt Shazam als blockiert

    def __init__(self, min_interval: float = 1.0, on_wait=None):
        from aiohttp_retry import ExponentialRetry
        from shazamio import Shazam
        from shazamio.client import HTTPClient

        # 429 behandeln wir selbst (mit echten Pausen); Serverfehler kurz wiederholen
        self._client = HTTPClient(
            retry_options=ExponentialRetry(attempts=3, start_timeout=1, max_timeout=8,
                                           statuses={500, 502, 503, 504})
        )
        self._shazam = Shazam(language="de-DE", endpoint_country="DE", http_client=self._client)
        self.base_interval = min_interval
        self._limiter = RateLimiter(min_interval)
        self._lock = asyncio.Lock()
        self.on_wait = on_wait
        self.rate_limited = 0

    async def recognize(self, wav: bytes) -> Match | None:
        from shazamio.exceptions import RateLimited

        async with self._lock:  # nie zwei Anfragen gleichzeitig
            for pause in (*self.PAUSES, None):
                await self._limiter.wait()
                try:
                    answer = await self._shazam.recognize(wav)
                except RateLimited:
                    self.rate_limited += 1
                    if pause is None:
                        raise ShazamBlocked(
                            "Shazam nimmt von deinem Internetanschluss gerade keine Anfragen mehr an.")
                    # dauerhaft langsamer werden
                    self._limiter.min_interval = min(8.0, self._limiter.min_interval * 1.7 + 0.5)
                    if self.on_wait:
                        self.on_wait(pause)
                    await asyncio.sleep(pause)
                    continue
                # Nach Erfolg ganz langsam wieder schneller werden
                self._limiter.min_interval = max(self.base_interval, self._limiter.min_interval * 0.97)
                return parse_shazam(answer)
        return None

    async def close(self) -> None:
        try:
            await self._client.close()
        except Exception:
            pass


def parse_shazam(answer: dict) -> Match | None:
    track = (answer or {}).get("track")
    matches = (answer or {}).get("matches") or []
    if not track or not matches:
        return None
    meta: dict[str, str] = {}
    for section in track.get("sections", []):
        if section.get("type") == "SONG":
            for item in section.get("metadata", []):
                if item.get("title") and item.get("text"):
                    meta[item["title"].lower()] = item["text"]
    apple = None
    for opt in (track.get("hub") or {}).get("options", []) or []:
        for act in opt.get("actions", []) or []:
            if (act.get("uri") or "").startswith("https://music.apple.com"):
                apple = act["uri"]
    first = matches[0]
    skew = max(abs(first.get("timeskew", 0) or 0), abs(first.get("frequencyskew", 0) or 0))
    return Match(
        title=track.get("title") or "?",
        artist=track.get("subtitle") or "?",
        provider="Shazam",
        album=meta.get("album"),
        label=meta.get("label") or meta.get("plattenfirma") or meta.get("label/vertrieb"),
        released=meta.get("released") or meta.get("veröffentlicht"),
        isrc=track.get("isrc"),
        url=track.get("url"),
        apple_url=apple,
        cover=(track.get("images") or {}).get("coverart"),
        skew=skew,
        song_offset=first.get("offset"),
        extra={"genre": (track.get("genres") or {}).get("primary")},
    )


# --------------------------------------------------------------------------- #
# AudD (kostenpflichtig, https://audd.io)
# --------------------------------------------------------------------------- #

class AuddRecognizer(Recognizer):
    name = "AudD"

    def __init__(self, token: str, min_interval: float = 0.3):
        self.token = token
        self._session: aiohttp.ClientSession | None = None
        self._limiter = RateLimiter(min_interval)

    async def recognize(self, wav: bytes) -> Match | None:
        await self._limiter.wait()
        self._session = self._session or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        form = aiohttp.FormData()
        form.add_field("api_token", self.token)
        form.add_field("return", "apple_music,spotify")
        form.add_field("file", wav, filename="clip.wav", content_type="audio/wav")
        async with self._session.post("https://api.audd.io/", data=form) as resp:
            data = await resp.json(content_type=None)
        if data.get("status") != "success":
            err = (data.get("error") or {}).get("error_message", "unbekannter Fehler")
            raise RuntimeError(f"AudD: {err}")
        r = data.get("result")
        if not r:
            return None
        return Match(
            title=r.get("title") or "?", artist=r.get("artist") or "?", provider="AudD",
            album=r.get("album"), label=r.get("label"), released=r.get("release_date"),
            url=r.get("song_link"),
            apple_url=(r.get("apple_music") or {}).get("url"),
            isrc=(r.get("apple_music") or {}).get("isrc") or
                 ((r.get("spotify") or {}).get("external_ids") or {}).get("isrc"),
        )

    async def close(self) -> None:
        if self._session:
            await self._session.close()


# --------------------------------------------------------------------------- #
# ACRCloud (kostenpflichtig, großer Katalog inkl. vieler Production-Music-Libraries)
# --------------------------------------------------------------------------- #

class AcrCloudRecognizer(Recognizer):
    name = "ACRCloud"

    def __init__(self, host: str, access_key: str, access_secret: str, min_interval: float = 0.3):
        self.host = host.replace("https://", "").replace("http://", "").strip("/")
        self.access_key = access_key
        self.access_secret = access_secret
        self._session: aiohttp.ClientSession | None = None
        self._limiter = RateLimiter(min_interval)

    async def recognize(self, wav: bytes) -> Match | None:
        await self._limiter.wait()
        self._session = self._session or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        ts = str(int(time.time()))
        to_sign = "\n".join(["POST", "/v1/identify", self.access_key, "audio", "1", ts])
        sig = base64.b64encode(
            hmac.new(self.access_secret.encode(), to_sign.encode(), hashlib.sha1).digest()
        ).decode()
        form = aiohttp.FormData()
        form.add_field("sample", wav, filename="clip.wav", content_type="audio/wav")
        for k, v in {"access_key": self.access_key, "data_type": "audio",
                     "signature_version": "1", "signature": sig,
                     "sample_bytes": str(len(wav)), "timestamp": ts}.items():
            form.add_field(k, v)
        async with self._session.post(f"https://{self.host}/v1/identify", data=form) as resp:
            data = await resp.json(content_type=None)
        code = (data.get("status") or {}).get("code")
        if code == 1001:  # kein Treffer
            return None
        if code != 0:
            raise RuntimeError(f"ACRCloud: {(data.get('status') or {}).get('msg')}")
        music = ((data.get("metadata") or {}).get("music") or [None])[0]
        if not music:
            return None
        return Match(
            title=music.get("title") or "?",
            artist=", ".join(a.get("name", "") for a in music.get("artists", [])) or "?",
            provider="ACRCloud",
            album=(music.get("album") or {}).get("name"),
            label=music.get("label"),
            released=music.get("release_date"),
            isrc=(music.get("external_ids") or {}).get("isrc"),
            song_offset=(music.get("play_offset_ms") or 0) / 1000,
            extra={"score": music.get("score")},
        )

    async def close(self) -> None:
        if self._session:
            await self._session.close()


def build_recognizers(keys: dict | None, on_wait=None) -> list[Recognizer]:
    """Shazam ist immer dabei; AudD/ACRCloud nur, wenn Schlüssel vorhanden sind."""
    keys = keys or {}
    recs: list[Recognizer] = [ShazamRecognizer(on_wait=on_wait)]
    if keys.get("audd_token"):
        recs.append(AuddRecognizer(keys["audd_token"]))
    if keys.get("acr_host") and keys.get("acr_key") and keys.get("acr_secret"):
        recs.append(AcrCloudRecognizer(keys["acr_host"], keys["acr_key"], keys["acr_secret"]))
    return recs
