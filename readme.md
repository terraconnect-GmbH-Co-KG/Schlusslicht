# SCHLUSSLICHT — Das Magazin der Letzten

> *Hinten ist auch eine Richtung.*

Ein Proof of Concept von **[TerraConnect](https://terraconnect.de)** — Federführung: **Burkhard Frie**

---

## Was ist das?

**schlusslicht.de** ist ein deutschsprachiges Satire-Nachrichtenmagazin, das täglich die Schlusslichter in 24 Kategorien dokumentiert — die schlechtesten Tabellenstände, die gescheiterten Missionen, die korruptesten Länder, die sterbenden Sprachen. Echte Daten, echte Quellen, trockener Kommentar.

Technisch ist die Seite vollständig statisch — kein Backend, kein CMS, kein laufender Server. Trotzdem ist der Inhalt täglich neu recherchiert und eingebaut.

---

## Der Ablauf: Wie die Seite täglich neu entsteht

```
GitHub Actions (Cron 04:10 UTC)
        │
        ▼
Python-Script  rebuild/generate.py
        │
        ├── API-Call 1: 24 Rubriken + Spotlight + Ticker
        │         │
        │         └── OpenRouter API  ──►  LLM (z. B. DeepSeek / Gemini / Claude)
        │                                        │
        │                                        └── openrouter:web_search (Server Tool)
        │                                                 │
        │                                                 └── aktuelle Websuche
        │                                                 └── strukturiertes JSON zurück
        │
        ├── API-Call 2: 3 Hintergrundstorys
        │         └──  (gleiche Pipeline)
        │
        ▼
BeautifulSoup  →  JSON direkt in HTML-Template-Elemente injiziert
        │         (CSS-Selektoren, kein JavaScript-Block)
        ▼
Fertige  index.html  — vollständig statisch, kein API-Key im Browser
        │
        ├──► git commit + push  (nur bei Änderungen)
        ├──► GitHub Actions Artifact  (90 Tage)
        └──► GitHub Release  tag: tagesausgabe-YYYY-MM-DD
                             asset: index.html
```

Kein Node.js. Kein Build-System. Kein Framework. Nur Python, `requests` und `beautifulsoup4`.

---

## Proof of Concept: Was hier demonstriert wird

| Konzept | Umsetzung |
|---|---|
| **LLM als Redakteur** | Das Modell entscheidet, welche Meldung zur Rubrik passt, und formuliert den Kommentar |
| **Web Search als Tool Call** | `openrouter:web_search` — der LLM sucht selbständig, kein manuelles Scraping |
| **Static Site Generation per KI** | Kein CMS, kein Template-Engine — BeautifulSoup injiziert direkt in DOM-Elemente |
| **Zero-Runtime-Kosten** | Die fertige Seite ist reines HTML, läuft auf jedem Hoster / GitHub Pages |
| **Automatisierter CI/CD-Loop** | GitHub Actions triggert täglich, committed das Ergebnis und erstellt ein Release |
| **Modell-Agnostik** | Ein Zeile in `rebuild/generate.py` tauscht das Modell — OpenRouter unterstützt 300+ |

---

## Tech Stack

| Schicht | Technologie |
|---|---|
| Hosting | Statisches HTML (GitHub Pages / beliebiger Hoster) |
| Build-Trigger | GitHub Actions (Cron + `workflow_dispatch`) |
| Build-Script | Python 3.12 · `requests` · `beautifulsoup4` |
| LLM-Gateway | [OpenRouter](https://openrouter.ai) ([AnthropicAPi](https://platform.claude.com/)) |
| Web Search | `openrouter:web_search` (Server-seitiger Tool Call) |
---

## Setup

```bash
# Lokal ausführen
pip install requests beautifulsoup4
OPENROUTER_API_KEY="sk-or-..." python rebuild/generate.py
```

Für den automatischen GitHub-Actions-Build: Repository forken, unter **Settings → Secrets → Actions** den Secret `OPENROUTER_API_KEY` anlegen, fertig.

---

## Projektstruktur

```
rebuild/generate.py          # Build-Script: LLM-Call + HTML-Injection
index.template.html          # HTML-Vorlage mit statischem Fallback-Inhalt
index.html                   # Täglich generierte Ausgabe (wird committed)
.github/workflows/
  daily-update.yml           # Cron-Workflow: Build → Commit → Release
biome.json                   # Formatter-Config (HTML/JSON/CSS/JS)
```

---

*Ein Projekt von [TerraConnect](https://terraconnect.de) · Federführung Burkhard Frie*
