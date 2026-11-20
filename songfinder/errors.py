"""Verständliche Fehlermeldungen."""
from __future__ import annotations


def friendly_error(exc: Exception) -> str:
    from .pipeline import RecognitionError

    if isinstance(exc, RecognitionError) and "keine Anfragen an" in str(exc):
        return ("Shazam hat gerade zu viele Anfragen von deinem Internetanschluss bekommen und macht "
                "eine Pause. Bitte 15–30 Minuten warten und es dann erneut versuchen – am besten mit "
                "„Schnell“, das braucht die wenigsten Anfragen.")
    if isinstance(exc, RecognitionError) and "AudD meldet" in str(exc):
        detail = str(exc).split("Letzter Fehler:", 1)[-1].strip()
        return ("AudD konnte das Video nicht analysieren.\n\n" + detail +
                "\n\nTipp: Unter „Erweitert“ → „Token prüfen“ siehst du, was dein Token kann.")
    if isinstance(exc, RecognitionError):
        detail = str(exc).split("Letzter Fehler:", 1)[-1].strip()[:300]
        return ("Die Musik-Erkennung (Shazam) ist gerade nicht erreichbar. Bitte Internetverbindung "
                f"prüfen und es in ein paar Minuten noch einmal versuchen.\n\nTechnische Ursache: {detail}")
    msg = str(exc)
    low = msg.lower()
    if "not a bot" in low or "sign in to confirm" in low:
        return ("YouTube verlangt eine Anmeldung („Bot“-Prüfung). Wähle unter „Erweitert“ deinen "
                "Browser für Cookies aus (du musst dort bei YouTube eingeloggt sein) und versuche es erneut.")
    if "private video" in low:
        return "Das Video ist privat und kann nicht geladen werden."
    if "unsupported url" in low or "is not a valid url" in low:
        return "Das ist kein gültiger Video-Link."
    if "video unavailable" in low:
        return "Das Video ist nicht verfügbar (gelöscht, gesperrt oder in deinem Land blockiert)."
    if "javascript runtime" in low or "js runtime" in low or "n challenge" in low:
        return ("yt-dlp braucht eine JavaScript-Laufzeit. Bitte 'pip install -U deno yt-dlp[default]' "
                "ausführen und neu starten.")
    if any(t in low for t in ("unable to connect", "getaddrinfo", "connection refused",
                               "name or service not known", "timed out", "network is unreachable")):
        return ("Keine Verbindung zu YouTube. Bitte Internetverbindung prüfen (VPN/Firewall?) "
                "und erneut versuchen.")
    return msg.removeprefix("ERROR: ")
