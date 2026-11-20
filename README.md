# 🎵 Song Finder

YouTube-Link einfügen → Liste aller Songs im Video, mit Zeitstempel und Lizenz-Plattform
(Epidemic Sound, Artlist, Lickd, Uppbeat, Soundstripe, Musicbed …).
Funktioniert auch bei leiser Hintergrundmusik unter Stimme und Soundeffekten.

## Installieren (Mac)

1. Unter **Releases** (rechts auf der GitHub-Seite) die neueste Version öffnen und die passende DMG laden:
   * **Apple Silicon (M1, M2, M3, M4 …)** → `SongFinder-Mac-AppleSilicon.dmg`
   * **Intel-Mac** → `SongFinder-Mac-Intel.dmg`

   Welcher Mac? Apple-Menü  → „Über diesen Mac“ → steht bei „Chip“ *Apple M…*, nimm Apple Silicon.
2. DMG öffnen und **Song Finder** auf **Programme** ziehen.
3. Song Finder öffnen. Beim allerersten Mal sagt macOS, die App könne nicht überprüft werden
   (sie ist nicht bei Apple registriert, das kostet 99 $/Jahr). Dann:
   **Systemeinstellungen → Datenschutz & Sicherheit → ganz unten „Dennoch öffnen“**. Nur einmal nötig.

Die App braucht kein Python und kein Terminal. Den YouTube-Downloader (yt-dlp) hält sie beim Start
selbst aktuell. Protokoll für die Fehlersuche: `~/Library/Application Support/SongFinder/songfinder.log`.

Neue App-Versionen baut GitHub automatisch bei jeder Änderung im `main`-Branch
(`.github/workflows/build-mac.yml`).

## Wie es funktioniert

1. **Tonspur laden** – nur das Audio wird mit yt-dlp heruntergeladen (geht auch mit einer lokalen
   Videodatei: einfach den Dateipfad statt des Links einfügen).
2. **Musik freilegen** – pro Ausschnitt werden bis zu drei Varianten probiert:
   * **KI-Musikspur** (optional, Demucs): entfernt Stimmen per KI. Hilft am meisten bei Voice-over.
   * **Original**
   * **Stereo-Trick**: Sprecher sind fast immer mittig gemischt, Musik ist stereo. Links minus Rechts
     löscht die Stimme, ganz ohne KI.
3. **Erkennen** – alle 5–15 s wird ein 12-s-Ausschnitt an Shazam geschickt (kostenlos). Epidemic Sound
   und Artlist veröffentlichen ihre Songs auf Spotify/Apple Music, deshalb kennt Shazam die meisten.
4. **Zusammenführen** – Treffer werden zu Songs mit Zeitbereichen (z. B. `0:30–2:10`) zusammengefasst.
   Ein Song, der in mehreren Ausschnitten erkannt wird, gilt als sicher; Einzeltreffer werden
   separat als „unsicher“ gelistet.
5. **Plattform bestimmen** – über das Label (z. B. „Epidemic Sound“, „Artlist Original“) und über Links
   in der Videobeschreibung. Für jeden Song gibt es Such-Links zu den Plattformen.

## Gründlichkeit

| Modus      | Raster | Dauer für ein 15-min-Video* | Wann?                                   |
|------------|--------|-----------------------------|-----------------------------------------|
| Schnell    | 15 s   | ~1–2 min                    | Musik läuft lange am Stück              |
| Normal     | 10 s   | ~2–4 min                    | Standard                                |
| Gründlich  | 5 s    | ~5–8 min                    | viele kurze Einspieler / Übergänge      |

\* ohne KI-Trennung. Die KI braucht auf einem normalen Laptop zusätzlich etwa 20–30 % der
Videolänge (mit NVIDIA-Grafikkarte viel schneller).

## Grenzen (ehrlich)

* **Shazam muss den Song kennen.** Epidemic Sound und Artlist sind gut abgedeckt. Exklusive Libraries,
  die nicht auf Streamingdiensten sind (z. B. Teile von Musicbed oder Audio Network), findet Shazam teils
  nicht. Dann helfen die optionalen kostenpflichtigen Dienste unter „Erweitert“:
  * **AudD** (audd.io) – Token eintragen
  * **ACRCloud** (acrcloud.com) – Host, Access Key, Secret eintragen; sehr großer Katalog
  Sie werden nur für Ausschnitte gefragt, bei denen Shazam nichts gefunden hat.
* **Lickd** lizenziert bekannte Chart-Songs. Diese erkennt das Tool (Titel/Künstler), aber ob ein Creator
  sie über Lickd lizenziert hat, sieht man dem Audio nicht an. Nutze dafür den Link „Auf Lickd suchen“.
* Stark geschnittene, beschleunigte oder hochgepitchte Musik wird schlechter erkannt.
* Zeitangaben sind auf ca. ±5 s genau (Raster der Ausschnitte).
* Shazam wird über eine inoffizielle Schnittstelle angesprochen (Bibliothek `shazamio`). Shazam erlaubt
  nur eine begrenzte Zahl Anfragen pro Internetanschluss. Das Tool fragt deshalb nacheinander, macht bei
  „zu viele Anfragen“ automatisch Pausen (15 s bis 2 min) und wird dann langsamer. Blockiert Shazam
  dauerhaft, zeigt es die bis dahin gefundenen Songs; nach 15–30 Minuten geht es wieder.

## Probleme?

| Meldung                                    | Lösung                                                                   |
|--------------------------------------------|--------------------------------------------------------------------------|
| „Bot“-Prüfung / *Sign in to confirm*       | Unter „Erweitert“ deinen Browser für Cookies wählen (dort bei YouTube eingeloggt sein) |
| Download klappt plötzlich nicht mehr       | App neu starten, sie aktualisiert yt-dlp automatisch                     |

## Ohne App starten (für Entwickler)

`Start-Mac.command` doppelklicken (braucht Python 3.10+). Optional `KI-nachruesten-Mac.command`
für die KI-Stimmentrennung (Demucs). Die gibt es nur in dieser Variante, weil sie die App um ~1 GB
vergrößern würde.

## Kommandozeile (optional)

```bash
.venv/bin/python -m songfinder "https://www.youtube.com/watch?v=…" --modus gruendlich --csv songs.csv
```
Optionen: `--ki an|aus|auto`, `--json datei.json`.
API-Schlüssel per Umgebungsvariable: `AUDD_TOKEN`, `ACR_HOST`, `ACR_ACCESS_KEY`, `ACR_ACCESS_SECRET`.

Bitte nur für Videos nutzen, bei denen du das Herunterladen darfst (z. B. eigene Videos oder zur
Recherche der Musik-Lizenzen).
