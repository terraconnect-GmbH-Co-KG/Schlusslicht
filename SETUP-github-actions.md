# Tägliche Aktualisierung von schlusslicht.de — Einrichtung

Dieser GitHub-Actions-Workflow baut die Seite **jeden Morgen automatisch neu**:
Er recherchiert per Anthropic-API mit Websuche tagesaktuelle Meldungen für alle
24 Rubriken sowie 3 Hintergrundstorys, baut sie fest in `index.html` ein und
committet das Ergebnis. Die veröffentlichte Seite ist danach vollständig
statisch — kein API-Schlüssel im Browser.

---

## Dateien & Ablageorte

Die beiden Dateien gehören an genau diese Stellen im Repository:

```
dein-repo/
├── .github/
│   └── workflows/
│       └── daily-update.yml      ← der Workflow
├── build/
│   └── generate.py               ← das Build-Skript
├── index.template.html           ← deine bisherige index.html, umbenannt
└── index.html                    ← wird vom Workflow erzeugt/überschrieben
```

---

## Einrichtung in 4 Schritten

### 1. Vorlage anlegen
Benenne deine aktuelle `index.html` in **`index.template.html`** um und lege sie
ins Repository-Wurzelverzeichnis. Diese Datei bleibt unverändert und dient als
Vorlage. (Fehlt sie, nutzt das Skript ersatzweise `index.html`.)

### 2. Dateien hinzufügen
Lege `daily-update.yml` unter `.github/workflows/` und `generate.py` unter
`build/` ab — genau wie oben gezeigt.

### 3. API-Schlüssel als Secret hinterlegen
Im Repository: **Settings → Secrets and variables → Actions → New repository secret**

- **Name:** `ANTHROPIC_API_KEY`
- **Wert:** dein Anthropic-API-Schlüssel

Der Schlüssel ist damit nur dem Workflow zugänglich und steht **niemals** im
veröffentlichten HTML.

### 4. Workflow-Schreibrechte prüfen
Unter **Settings → Actions → General → Workflow permissions** muss
**„Read and write permissions"** aktiviert sein, damit der Bot das neue
`index.html` committen darf.

---

## Betrieb

- **Automatisch:** täglich um 04:10 UTC (ca. 05:10/06:10 deutscher Zeit).
  Geplante Läufe können von GitHub um einige Minuten verschoben werden.
- **Manuell:** Reiter **Actions → „Tägliche Aktualisierung" → Run workflow**.
- Gibt es keine inhaltlichen Änderungen, wird nichts committet.

### Zeitpunkt ändern
Den Cron-Ausdruck in `daily-update.yml` anpassen (immer UTC):

```yaml
- cron: '10 4 * * *'   # Minute Stunde Tag Monat Wochentag
```

---

## Veröffentlichung (GitHub Pages)

Soll die Seite direkt über GitHub Pages laufen:
**Settings → Pages → Source: Deploy from a branch → Branch: main / root.**
Jeder Commit des Workflows aktualisiert dann automatisch die Live-Seite.

Liegt die Seite auf einem eigenen Hoster, ergänze im Workflow nach dem
`git push` einen Deploy-Schritt (z. B. FTP- oder rsync-Upload).

---

## Hinweise

- **Modell:** In `build/generate.py` ist `claude-sonnet-4-20250514` eingestellt.
  Bei Bedarf die Variable `MODEL` oben im Skript anpassen.
- **Kosten:** Pro Tag zwei API-Aufrufe mit Websuche. Überschaubar, aber nicht
  kostenlos — Abrechnung über dein Anthropic-Konto.
- **Ruhende Repos:** GitHub deaktiviert geplante Workflows nach 60 Tagen ohne
  Repository-Aktivität. Die täglichen Commits des Bots halten ihn aktiv.
- **Fällt eine Recherche aus,** bleibt der betreffende Teil auf dem Stand der
  Vorlage; die Seite bleibt immer gültig und online.
- **Live-Daten** (Besucherzähler, Sport, Wechselkurse) laufen weiterhin direkt
  im Browser — diese APIs brauchen keinen Schlüssel und bleiben aktiv.
