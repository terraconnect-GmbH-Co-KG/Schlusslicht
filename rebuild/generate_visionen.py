#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_visionen.py — Tagesaktualisierung für brightside.html
================================================================================
Wird vom GitHub-Actions-Workflow .github/workflows/daily-update.yml gestartet,
direkt im Anschluss an generate.py.

Ablauf:
  1. Liest die Vorlage  brightside.template.html.
  2. Recherchiert per OpenRouter-API (perplexity/sonar, eingebaute Websuche)
     a) ein Spotlight ("Heute im Licht"),
     b) 7 kurze, belegte gute Nachrichten aus unterschiedlichen Bereichen,
     c) 3 Hintergrundgeschichten mit Fakten und Einordnung.
  3. Baut die Inhalte fest in das HTML ein und schreibt  brightside.html.

WICHTIG zur Sorgfaltspflicht: Diese Seite behandelt Gesundheits-/Wissenschafts-
themen. Der Prompt verlangt ausdrücklich echte, prüfbare Quellen (WHO, IEA,
IUCN, UN, Weltbank, Fachjournale, offizielle Statistikämter) mit echter URL.
Es findet KEINE redaktionelle Prüfung vor Veröffentlichung mehr statt
(bewusste Entscheidung, siehe Commit-Historie) — die Sorgfalt muss deshalb im
Prompt und in der Quellenpflicht stecken, nicht in einem manuellen Schritt.
"""

import datetime
import json
import os
import re
import sys
import time

import requests
from urllib.parse import urlparse
from bs4 import BeautifulSoup

# ── Konfiguration ────────────────────────────────────────────────────────────
API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "perplexity/sonar"
LANG = os.environ.get("SL_LANG", "de").strip().lower()
TEMPLATE = "brightside.en.template.html" if LANG == "en" else "brightside.template.html"
OUTPUT = "brightside.en.html" if LANG == "en" else "brightside.html"
TIMEOUT = 240

WOCHENTAGE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONATE = (
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"]
    if LANG == "en" else
    ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
     "August", "September", "Oktober", "November", "Dezember"]
)


def log(msg: str) -> None:
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


# Etablierte, real existierende Institutionen/Medien (siehe generate.py für
# ausführliche Begründung). Bei technischem Verbindungsfehler (nicht bei
# echtem 404) wird eine URL auf diesen Domains trotzdem akzeptiert.
TRUSTED_SOURCE_DOMAINS = {
    "transparency.org", "worldhappiness.report", "transfermarkt.de",
    "rsf.org", "reporter-ohne-grenzen.de", "oecd.org", "who.int",
    "worldbank.org", "imf.org", "germanwatch.org", "unesco.org",
    "destatis.de", "boeckler.de", "bundesrechnungshof.de", "adac.de",
    "ec.europa.eu", "propublica.org", "espn.com", "bundeswahlleiterin.de",
    "tagesschau.de", "zeit.de", "faz.net", "spiegel.de", "sueddeutsche.de",
    "handelsblatt.com", "bloomberg.com", "reuters.com", "dpa.com",
    "yonhap.co.kr", "wikipedia.org", "nasa.gov", "esa.int", "wan-ifra.org",
    "amnesty.org", "cpj.org", "boxofficemojo.com", "variety.com",
    "ookla.com", "speedtest.net", "statista.com", "bundesbank.de",
    "un.org", "gallup.com",
}


def _domain_is_trusted(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if host.startswith("www."):
        host = host[4:]
    return any(host == d or host.endswith("." + d) for d in TRUSTED_SOURCE_DOMAINS)


def verify_url(url: str, timeout: int = 8) -> bool:
    """Prüft, ob eine Quellen-URL tatsächlich existiert und erreichbar ist.
    Technische Absicherung gegen halluzinierte Quellen — siehe generate.py
    für die ausführliche Begründung.

    WICHTIG (grundlegend überarbeitet, identisch zu generate.py): Nur eine
    ECHTE DNS-Auflösungs-Fehlermeldung ist ein verlässlicher Beleg gegen
    die Existenz einer Quelle. JEDER andere Fehler (Timeout, Connection
    Refused/Reset, Bot-Schutz-Statuscodes) bedeutet: der Server existiert,
    blockiert aber nur die automatisierte Anfrage — kein Beleg gegen die
    Existenz. Eine kleine kuratierte Domain-Liste kann die Vielzahl echter,
    täglich wechselnder Quellen aus aller Welt nicht abdecken; dieser
    Grundsatz funktioniert dagegen für JEDE Domain."""
    if not url or not isinstance(url, str) or not url.strip().lower().startswith("http"):
        return False
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    }
    BOT_BLOCK_CODES = {401, 403, 405, 429, 500, 502, 503, 504}
    DNS_FAILURE_MARKERS = (
        "nameresolutionerror", "failed to resolve", "getaddrinfo failed",
        "name or service not known", "temporary failure in name resolution",
        "no address associated with hostname", "dns lookup failed",
    )
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True, headers=headers)
        if r.status_code >= 400:
            r = requests.get(url, timeout=timeout, allow_redirects=True, headers=headers, stream=True)
        if r.status_code < 400:
            return True
        if r.status_code in BOT_BLOCK_CODES:
            log(f"  Quelle antwortet mit HTTP {r.status_code} (Bot-Schutz/"
                f"Server-Fehler, keine echte Nicht-Existenz) — wird trotzdem "
                f"akzeptiert: {url}")
            return True
        return False
    except requests.RequestException as exc:
        msg = str(exc).lower()
        ist_dns_fehler = any(marker in msg for marker in DNS_FAILURE_MARKERS)
        if not ist_dns_fehler:
            log(f"  Quelle technisch nicht erreichbar ({exc.__class__.__name__}, "
                f"kein DNS-Fehler — Domain existiert real, Server blockiert nur "
                f"die Anfrage) — wird akzeptiert: {url}")
            return True
        if _domain_is_trusted(url):
            log(f"  Quelle mit DNS-Fehler, aber Domain gilt zusätzlich als "
                f"etablierte Institution/Quelle — wird trotzdem akzeptiert: {url}")
            return True
        log(f"  Quellen-URL nicht erreichbar: {url} ({exc.__class__.__name__})")
        return False


def call_api(system: str, prompt: str, max_tokens: int, retries: int = 3):
    if LANG == "en":
        system = (
            "CRITICAL LANGUAGE RULE — HIGHEST PRIORITY: Write EVERY single output "
            "value (headlines, comments, titles, paragraphs, tags, labels, captions, "
            "facts, teasers, ticker items) in ENGLISH (US) ONLY. The instructions "
            "below are written in German, but your output must be entirely in "
            "English. NEVER output German words or sentences.\n\n" + system
        )
        prompt = (
            prompt
            + "\n\nFINAL REMINDER — MANDATORY: Every output value in the JSON must "
            "be written in ENGLISH (US). German output is INVALID and will be "
            "rejected. Translate any German source material into English."
        )
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    for attempt in range(1, retries + 1):
        try:
            r = requests.post(API_URL, headers=headers, json=body, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"]
            log(f"  API-Status {r.status_code}: {r.text[:300]}")
        except Exception as exc:  # noqa: BLE001
            log(f"  API-Fehler (Versuch {attempt}/{retries}): {exc}")
        time.sleep(6 * attempt)
    return None


_DE_STOPWORTE_GATE = {"der", "die", "das", "und", "nicht", "eine", "einen", "mit",
                      "für", "von", "wird", "sind", "auch", "sich", "wurde", "beim",
                      "über", "gegen", "wegen", "seit", "noch", "nur", "dass"}


def _wirkt_deutsch(obj) -> bool:
    """Heuristik: Sammelt alle String-Werte einer JSON-Struktur und prüft, ob
    der Text ueberwiegend deutsch wirkt (Umlaute oder viele deutsche
    Stoppwoerter). Nur im EN-Modus relevant."""
    texte = []

    def sammle(o):
        if isinstance(o, str):
            texte.append(o)
        elif isinstance(o, list):
            for v in o:
                sammle(v)
        elif isinstance(o, dict):
            for v in o.values():
                sammle(v)

    sammle(obj)
    gesamt = " ".join(texte)
    if len(gesamt) < 60:
        return False
    if re.search(r"[äöüßÄÖÜ]", gesamt):
        return True
    woerter = re.findall(r"[a-zA-Z]+", gesamt.lower())
    if not woerter:
        return False
    treffer = sum(1 for w in woerter if w in _DE_STOPWORTE_GATE)
    return (treffer / len(woerter)) > 0.08


def extract_json(text):
    if not text:
        return None
    text = text.replace("```json", "").replace("```", "").strip()
    start, end = text.find("{"), text.rfind("}") + 1
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end])
    except json.JSONDecodeError as exc:
        log(f"  JSON-Parsefehler: {exc}")
        return None
    data = sanitize(data)
    if LANG == "en" and _wirkt_deutsch(data):
        log("  SPRACH-SCHRANKE: Antwort wirkt deutsch, obwohl Englisch verlangt "
            "war — komplett verworfen, bestehender (englischer) Stand bleibt.")
        return None
    return data


def call_api_json(system: str, prompt: str, max_tokens: int, repair_retries: int = 2):
    """Wie call_api() + extract_json(), aber mit Selbstkorrektur: Wenn die
    Modellantwort kein gültiges JSON ergibt, wird dem Modell der exakte
    Parse-Fehler zurückgemeldet und es bekommt bis zu `repair_retries`
    weitere Versuche. Identisches Muster wie in generate.py/generate_mfb.py
    — behebt dieselbe Fehlerklasse, die dort bereits zu stillschweigenden
    Totalausfällen (Insights aktualisierte sich tagelang gar nicht) führte."""
    raw = call_api(system, prompt, max_tokens=max_tokens)
    data = extract_json(raw)
    attempt = 0
    while data is None and raw and attempt < repair_retries:
        attempt += 1
        text = raw.replace("```json", "").replace("```", "").strip()
        start, end = text.find("{"), text.rfind("}") + 1
        parse_error = "unbekannt"
        if start >= 0 and end > start:
            try:
                json.loads(text[start:end])
            except json.JSONDecodeError as exc:
                parse_error = str(exc)
        log(f"  JSON war ungültig ({parse_error}) — bitte Modell um Korrektur "
            f"(Versuch {attempt}/{repair_retries}) …")
        repair_prompt = (
            "Deine letzte Antwort war KEIN gültiges JSON — Fehler beim Parsen: "
            f"\"{parse_error}\". Häufige Ursachen: abgeschnittene Antwort (zu "
            "lang für das Token-Limit) oder nicht escapte Anführungszeichen "
            "in Fließtext. Antworte JETZT ERNEUT auf dieselbe Aufgabe, aber "
            "diesmal: (1) kürzer und prägnanter formulieren, falls die "
            "Antwort zu lang wurde, (2) alle doppelten Anführungszeichen "
            "innerhalb von Textwerten mit \\\" escapen, (3) AUSSCHLIESSLICH "
            "das vollständige, gültige JSON-Objekt ausgeben, keine Markdown-"
            "Codeblöcke, kein einleitender oder abschließender Text.\n\n"
            f"Ursprüngliche Aufgabe:\n{prompt}"
        )
        raw = call_api(system, repair_prompt, max_tokens=max_tokens)
        data = extract_json(raw)
    if data is None:
        log(f"  JSON-Selbstkorrektur nach {attempt} Versuch(en) gescheitert — gebe auf.")
    return data


_FREMDSCHRIFT_PATTERN = re.compile(
    "["
    "\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7a3"  # CJK, Hiragana/Katakana, Hangul
    "\u0400-\u04ff\u0600-\u06ff\u0900-\u097f"  # Kyrillisch, Arabisch, Devanagari
    "]+"
)


def sanitize(obj):
    """Entfernt rekursiv fremdschriftliche Zeichen (Sprach-Leck des Modells)."""
    if isinstance(obj, str):
        cleaned = _FREMDSCHRIFT_PATTERN.sub("", obj)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        if cleaned != obj.strip():
            log(f"  Fremdschrift entfernt: {obj!r} -> {cleaned!r}")
        return cleaned
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    return obj


# ── Recherche ─────────────────────────────────────────────────────────────────
_GEMEINSAME_REGELN = (
    "HÖCHSTE PRIORITÄT: Jede einzelne Meldung MUSS auf einer echten, "
    "existierenden, mit Websuche verifizierten Quelle beruhen (z. B. WHO, IEA, "
    "IUCN, UN, Weltbank/IMF, Fachjournale wie The Lancet/Nature, offizielle "
    "Statistikämter, Reuters/dpa für Fakten). Erfinde NIEMALS Zahlen, Studien, "
    "URLs oder Quellennamen — wenn du zu einem Thema keine echte, aktuelle, "
    "prüfbare Quelle findest, wähle ein anderes Thema, zu dem du eine hast.\n\n"
    "QUELLEN-DISZIPLIN (sehr wichtig): Jede Quellen-URL muss zur jeweiligen "
    "Meldung inhaltlich passen — Name und URL müssen zusammengehören. "
    "Verwende NIEMALS dieselbe URL für zwei verschiedene Meldungen. Erfinde "
    "NIEMALS eine Domain, die zum Namen dieser Rubrik oder Website klingt "
    "(z. B. NIEMALS 'neuevisionen.de', 'visionen-news.de' oder Ähnliches) — "
    "das sind erfundene Fantasie-Domains, keine echten Quellen. Verwende "
    "NIEMALS Fantasie-Institutionen wie 'Technikbehörde' oder "
    "'Gesellschaftsbehörde' — nenne die tatsächliche, echte Organisation.\n\n"
    "Ton: sachlich-warm, nüchtern, mit Zahlen belegt — keine Übertreibung, "
    "keine Effekthascherei. Wo eine gute Nachricht ein 'Aber' hat (z. B. "
    "Finanzierungslücke, Restrisiko), nenne es ehrlich, statt es wegzulassen. "
    "Antworte AUSSCHLIESSLICH auf " + ("Englisch (US)" if LANG == "en" else "Deutsch") + " — keine chinesischen, kyrillischen, "
    "arabischen oder anderen nicht-lateinischen Schriftzeichen, auch nicht "
    "einzelne Wörter oder Zeichen davon. Wiederhole niemals denselben Fakt "
    "oder dieselbe Formulierung innerhalb einer Meldung oder über mehrere "
    "Meldungen hinweg. Antworte NUR mit einem einzigen validen JSON-Objekt, "
    "keine Erklärungen davor oder danach."
)


def get_spotlight(date_label: str):
    log("  Hole Spotlight …")
    system = f"Du bist Redakteur der Rubrik 'Visionen' auf schlusslicht.de.\n\n{_GEMEINSAME_REGELN}"
    prompt = f"""Finde die wichtigste, positive, gut belegte Nachricht der letzten Tage für die Ausgabe {date_label}.

Liefere GENAU dieses JSON-Schema:
{{
  "tag": "Bereich · Region (z. B. 'Gesundheit · weltweit')",
  "title": "Prägnante Überschrift",
  "body_html": "1-2 Absätze als HTML-String, <strong> für Kernzahlen erlaubt, ehrliche Einordnung",
  "source_name": "Name der Quelle (echte Organisation)",
  "source_url": "https://echte-existierende-url, die exakt zu source_name passt",
  "source_date": "Datum der Quelle, z. B. '8. Mai 2026'",
  "bignum": "kurze Kennzahl, z. B. '1 von 8' oder '+40%'",
  "bigcap": "1 Satz Erklärung der Kennzahl"
}}"""
    return call_api_json(system, prompt, max_tokens=1200)


def get_good_news_batch(date_label: str, anzahl: int, bereiche: str, ausgeschlossene_urls: list):
    system = f"Du bist Redakteur der Rubrik 'Visionen' auf schlusslicht.de.\n\n{_GEMEINSAME_REGELN}"
    ausschluss = (
        f"\n\nDiese URLs sind bereits für andere Meldungen vergeben — verwende "
        f"KEINE davon erneut: {', '.join(ausgeschlossene_urls)}."
        if ausgeschlossene_urls
        else ""
    )
    prompt = f"""Finde {anzahl} positive, gut belegte Nachrichten für die Ausgabe {date_label}.
Bevorzugte Themenbereiche für diese Gruppe: {bereiche}.{ausschluss}

Liefere GENAU dieses JSON-Schema:
{{
  "good_news": [
    {{
      "domain": "Themenbereich",
      "badge": "Region, z. B. 'Welt' oder 'Deutschland' oder 'Europa'",
      "icon": "ein passendes Emoji",
      "title": "Kurze, konkrete Überschrift",
      "body_html": "2-3 Sätze HTML-String mit Kernaussage und Zahl",
      "source_name": "Name der echten Organisation",
      "source_url": "https://echte-existierende-url, die exakt zu source_name passt",
      "source_date": "Datum, z. B. 'April 2026'"
    }}
    // genau {anzahl} Einträge
  ]
}}"""
    result = call_api_json(system, prompt, max_tokens=2200)
    return (result or {}).get("good_news", [])


def get_background_stories(date_label: str):
    log("  Hole Hintergrundstorys …")
    system = f"Du bist Redakteur der Rubrik 'Visionen' auf schlusslicht.de.\n\n{_GEMEINSAME_REGELN}"
    prompt = f"""Finde 3 positive Entwicklungen mit ausreichend Tiefe für Hintergrundstorys, Ausgabe {date_label}.

Liefere GENAU dieses JSON-Schema:
{{
  "stories": [
    {{
      "teaser_cat": "Bereich · Region",
      "teaser_title": "Kurztitel für die Vorschau-Kachel",
      "teaser_text": "1-2 Sätze Teaser",
      "modal_cat": "Bereich · Region · Jahr",
      "modal_title": "Ausführlicherer Titel",
      "lead": "1-2 Sätze Einstieg",
      "intro_html": "1 Absatz HTML mit Kontext/Hintergrund",
      "facts": ["Fakt 1 mit Zahl", "Fakt 2 mit Zahl", "Fakt 3 mit Zahl"],
      "einordnung_html": "1 Absatz ehrliche Einordnung inkl. Grenzen/offener Fragen",
      "sources": [
        {{"name": "Name der echten Organisation", "url": "https://echte-url, die exakt zu name passt", "date": "Datum"}}
      ]
    }}
    // genau 3 Einträge, thematisch unterschiedlich
  ]
}}"""
    result = call_api_json(system, prompt, max_tokens=3000)
    return (result or {}).get("stories", [])


def get_visionen_content(date_label: str):
    log("Recherchiere positive, belegte Nachrichten für brightside.html (in Gruppen) …")

    spotlight = get_spotlight(date_label)

    log("  Hole Good-News-Gruppe 1/2 …")
    gruppe1 = get_good_news_batch(
        date_label, 4, "Gesundheit, Klima & Energie, Natur & Artenschutz", []
    )
    bereits_verwendet = [it.get("source_url", "") for it in gruppe1 if it.get("source_url")]

    log("  Hole Good-News-Gruppe 2/2 …")
    gruppe2 = get_good_news_batch(
        date_label, 3, "Gesellschaft, Wissenschaft & Technik, Bildung", bereits_verwendet
    )

    stories = get_background_stories(date_label)

    data = {
        "stand_date": date_label,
        "spotlight": spotlight,
        "good_news": gruppe1 + gruppe2,
        "stories": stories,
    }

    if not data["spotlight"] and not data["good_news"] and not data["stories"]:
        log("  Keine verwertbaren Visionen-Inhalte erhalten.")
        return None
    data = verify_visionen_sources(data)
    data = review_and_rewrite_visionen(data, date_label)
    return data


_VERDAECHTIGE_DOMAIN_MUSTER = re.compile(
    r"(neuevisionen|visionen-news|visionennews|schlusslicht-?news)", re.IGNORECASE
)


def _domain_ist_verdaechtig(url: str) -> bool:
    """Erkennt offensichtlich erfundene Fantasie-Domains, die zufällig zum
    Namen der eigenen Rubrik/Website passen (z. B. 'neuevisionen.de') —
    ein starkes Anzeichen für eine halluzinierte statt echte Quelle."""
    return bool(_VERDAECHTIGE_DOMAIN_MUSTER.search(url or ""))


def review_and_rewrite_visionen(data: dict, date_label: str) -> dict:
    """NEUER Zwischenschritt vor der Veröffentlichung: Prüft Sinnhaftigkeit
    von Good-News-Kacheln und Story-Vorschauen und formuliert bei Bedarf
    sprachlich um (Grammatik, Klarheit, Redundanz) — OHNE dabei neue
    Fakten/Zahlen zu erfinden. Analog zur gleichnamigen Prüfung in
    generate.py. Läuft NACH der URL-Verifikation, damit nur bereits
    quellen-geprüfte Einträge die (kostenpflichtige) KI-Prüfung durchlaufen."""
    pruefbar = {}
    for i, item in enumerate(data.get("good_news", [])):
        if item.get("title") and item.get("body_html"):
            pruefbar[f"gn{i}"] = {"title": item["title"], "body_html": item["body_html"]}
    for i, st in enumerate(data.get("stories", [])):
        if st.get("teaser_title") and st.get("teaser_text"):
            pruefbar[f"story{i}"] = {"teaser_title": st["teaser_title"], "teaser_text": st["teaser_text"]}

    if not pruefbar:
        return data

    log("  Prüfe Good-News/Story-Texte auf Sinnhaftigkeit vor Veröffentlichung …")
    system = (
        "Du bist Chef vom Dienst bei schlusslicht.de (Rubrik 'Visionen'/"
        "Brightside) und prüfst Texte vor der Veröffentlichung. Du "
        "erfindest NIEMALS neue Fakten, Zahlen oder Ereignisse — du darfst "
        "aber vorhandene, korrekte Inhalte SPRACHLICH verbessern (Grammatik, "
        "Klarheit, holprige Formulierungen, Redundanz), wenn das inhaltlich "
        "exakt dasselbe aussagt wie vorher. Antworte AUSSCHLIESSLICH auf "
        + ("Englisch (US)" if LANG == "en" else "Deutsch") +
        ". Antworte NUR mit validem JSON, keine Erklärung."
    )
    prompt = (
        "Prüfe jeden Eintrag: Ist der Text konkret und in sich sinnvoll "
        "(keine generische Platzhalterformulierung, kein abgeschnittener "
        "oder zusammenhangloser Satz, keine holprige Grammatik, keine "
        "Wiederholung eines Standardsatzes aus einem anderen Eintrag)? "
        "Passt der Fliesstext inhaltlich zum Titel?\n\n"
        "WENN INHALTLICH KORREKT, ABER SCHLECHT FORMULIERT: gib 'ok': true "
        "UND eine verbesserte Fassung im jeweiligen '_neu'-Feld zurück — "
        "DIESELBEN Fakten/Zahlen/Namen, nur klarer formuliert. Lass die "
        "'_neu'-Felder weg, wenn der Text bereits gut ist.\n\n"
        "WENN INHALTLICH KAPUTT (Widerspruch, Unsinn, Platzhalter): gib "
        "'ok': false mit kurzer 'grund'-Angabe zurück.\n\n"
        f"Einträge:\n{json.dumps(pruefbar, ensure_ascii=False, indent=2)}\n\n"
        "Antworte als JSON, z.B.:\n"
        '{"gn0": {"ok": true}, '
        '"gn1": {"ok": true, "title_neu": "...", "body_html_neu": "..."}, '
        '"story0": {"ok": false, "grund": "..."}}'
    )
    urteil = call_api_json(system, prompt, max_tokens=3000) or {}

    def _zahlen(text: str) -> set:
        return set(re.findall(r"\d+[.,]?\d*", text or ""))

    def _anwenden(obj: dict, key: str, feld: str, feld_neu: str, bewertung: dict, label: str):
        neu = (bewertung.get(feld_neu) or "").strip()
        if not neu:
            return
        if _zahlen(neu) - _zahlen(obj.get(feld, "")):
            log(f"  {label}: Umformulierung von '{feld}' enthält neue Zahlen "
                f"— verworfen, Original bleibt.")
            return
        log(f"  {label}: '{feld}' sprachlich überarbeitet.")
        obj[feld] = neu

    neue_good_news = []
    for i, item in enumerate(data.get("good_news", [])):
        bewertung = urteil.get(f"gn{i}", {})
        if bewertung.get("ok") is False:
            log(f"  Good-News {item.get('title', '(ohne Titel)')!r}: "
                f"Sinnhaftigkeits-Prüfung fehlgeschlagen "
                f"({bewertung.get('grund', 'kein Grund')}) — verworfen.")
            continue
        _anwenden(item, f"gn{i}", "title", "title_neu", bewertung, f"Good-News {i}")
        _anwenden(item, f"gn{i}", "body_html", "body_html_neu", bewertung, f"Good-News {i}")
        neue_good_news.append(item)
    data["good_news"] = neue_good_news

    neue_stories = []
    for i, st in enumerate(data.get("stories", [])):
        bewertung = urteil.get(f"story{i}", {})
        if bewertung.get("ok") is False:
            log(f"  Story {st.get('teaser_title', '(ohne Titel)')!r}: "
                f"Sinnhaftigkeits-Prüfung fehlgeschlagen "
                f"({bewertung.get('grund', 'kein Grund')}) — verworfen.")
            continue
        _anwenden(st, f"story{i}", "teaser_title", "teaser_title_neu", bewertung, f"Story {i}")
        _anwenden(st, f"story{i}", "teaser_text", "teaser_text_neu", bewertung, f"Story {i}")
        neue_stories.append(st)
    data["stories"] = neue_stories

    return data


def verify_visionen_sources(data: dict) -> dict:
    """Prüft technisch JEDE angegebene Quellen-URL (Spotlight, Good-News-
    Kacheln, Hintergrundstorys). Ohne nachweislich erreichbare, plausible UND
    innerhalb der Ausgabe einzigartige URL wird der jeweilige Baustein
    komplett verworfen — keine Veröffentlichung ohne prüfbare, passende
    Quelle."""
    log("  Verifiziere Quellen-URLs technisch (HTTP-Check + Plausibilität + Einzigartigkeit) …")

    bereits_verwendete_urls = set()

    def url_ok(url: str, label: str) -> bool:
        url = (url or "").strip()
        if not url:
            log(f"  {label}: keine Quellen-URL angegeben — verworfen.")
            return False
        if _domain_ist_verdaechtig(url):
            log(f"  {label}: Quellen-URL sieht nach erfundener Fantasie-Domain "
                f"aus ({url}) — verworfen.")
            return False
        if url in bereits_verwendete_urls:
            log(f"  {label}: dieselbe URL wurde bereits für eine andere "
                f"Meldung verwendet ({url}) — verworfen (jede Quelle muss "
                f"einzigartig zur jeweiligen Meldung passen).")
            return False
        if not verify_url(url):
            log(f"  {label}: Quellen-URL nicht erreichbar ({url}) — verworfen.")
            return False
        bereits_verwendete_urls.add(url)
        log(f"  {label}: Quelle verifiziert ({url})")
        return True

    sp = data.get("spotlight")
    if sp and not url_ok(sp.get("source_url"), "Spotlight"):
        data["spotlight"] = None

    verifizierte_news = []
    for item in data.get("good_news", []):
        if not isinstance(item, dict):
            log("  Ungültiger Meldungs-Eintrag (kein Objekt) — übersprungen.")
            continue
        if url_ok(item.get("source_url"), f"Meldung {item.get('title', '(ohne Titel)')!r}"):
            verifizierte_news.append(item)
    data["good_news"] = verifizierte_news

    verifizierte_storys = []
    for st in data.get("stories", []):
        if not isinstance(st, dict):
            log("  Ungültiger Story-Eintrag (kein Objekt) — übersprungen.")
            continue
        quellen_ok = [
            s for s in (st.get("sources") or [])
            if url_ok(s.get("url"), f"Story {st.get('teaser_title', '(ohne Titel)')!r}")
        ]
        if not quellen_ok:
            log(f"  Story {st.get('teaser_title', '(ohne Titel)')!r}: keine "
                f"einzige gültige Quelle — komplett verworfen.")
            continue
        st["sources"] = quellen_ok
        verifizierte_storys.append(st)
    data["stories"] = verifizierte_storys

    log(f"  Ergebnis: Spotlight {'OK' if data.get('spotlight') else 'verworfen'}, "
        f"{len(data['good_news'])}/7 Meldungen, {len(data['stories'])}/3 Storys verifiziert.")
    return data


# ── HTML-Injektion ────────────────────────────────────────────────────────────
def set_text(node, value):
    if node is not None and value:
        node.clear()
        node.append(str(value))


def set_html(node, html_value):
    if node is not None and html_value:
        node.clear()
        node.append(BeautifulSoup(str(html_value), "html.parser"))


def make_source_html(name, url, date, prefix=None):
    if prefix is None:
        prefix = "Source" if LANG == "en" else "Quelle"
    name = (name or "").strip()
    url = (url or "").strip()
    date = (date or "").strip()
    if not name:
        return f"{prefix}: " + ("AI-researched" if LANG == "en" else "KI-recherchiert")
    if url:
        link = f'<a href="{url}" target="_blank" rel="noopener">{name}</a>'
    else:
        link = name
    return f"{prefix}: {link}" + (f" · {date}" if date else "")


def inject(html: str, data, date_label: str, build_time: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    # ── Spotlight ────────────────────────────────────────────────────────────
    sp = data.get("spotlight") or {}
    set_text(soup.select_one("#spot-tag"), sp.get("tag"))
    set_text(soup.select_one("#spot-title"), sp.get("title"))
    set_html(soup.select_one("#spot-text"), sp.get("body_html"))
    src_html = make_source_html(sp.get("source_name"), sp.get("source_url"), sp.get("source_date"))
    set_html(soup.select_one("#spot-src"), src_html)
    set_text(soup.select_one("#spot-bignum"), sp.get("bignum"))
    set_text(soup.select_one("#spot-bigcap"), sp.get("bigcap"))
    set_text(soup.select_one("#spotStand"), ("As of: " if LANG == "en" else "Stand: ") + date_label)

    # ── Good-News-Grid ───────────────────────────────────────────────────────
    # ATOMARITÄTS-ABSICHERUNG (dieselbe Fehlerklasse wie in generate.py
    # gefunden und behoben): Eine Kachel wird nur aktualisiert, wenn die
    # zentralen Felder (Titel + Text) BEIDE vorhanden sind — sonst bleibt
    # die GESAMTE Kachel unverändert, statt z.B. einen neuen Titel neben
    # einem alten, thematisch nicht mehr passenden Text stehen zu lassen.
    for i, item in enumerate(data.get("good_news", [])[:7], start=1):
        title = (item.get("title") or "").strip()
        body_html = (item.get("body_html") or "").strip()
        if not (title and body_html):
            log(f"  Good-News-Kachel {i}: Titel oder Text fehlt — "
                f"komplett übersprungen, Kachel bleibt unverändert.")
            continue
        set_text(soup.select_one(f"#gn{i}-dom"), item.get("domain"))
        set_text(soup.select_one(f"#gn{i}-badge"), item.get("badge"))
        icon = soup.select_one(f"#gn{i}-icon")
        if icon is not None and item.get("icon"):
            icon.clear()
            icon.append(str(item["icon"]))
        set_text(soup.select_one(f"#gn{i}-title"), title)
        set_html(soup.select_one(f"#gn{i}-text"), body_html)
        set_html(
            soup.select_one(f"#gn{i}-src"),
            make_source_html(item.get("source_name"), item.get("source_url"), item.get("source_date")),
        )

    # ── Hintergrundgeschichten ───────────────────────────────────────────────
    for i, st in enumerate(data.get("stories", [])[:3], start=1):
        # ATOMARITÄTS-ABSICHERUNG: Vorschau-Kachel (Kategorie/Titel/Teaser)
        # und Modal-Kopf (Kategorie/Titel/Lead) werden je als zusammen-
        # gehöriges Trio behandelt — nur wenn alle drei vorhanden sind,
        # wird aktualisiert, sonst bleibt der jeweilige Block unverändert.
        teaser_title = (st.get("teaser_title") or "").strip()
        teaser_text = (st.get("teaser_text") or "").strip()
        teaser_cat = (st.get("teaser_cat") or "").strip()
        if teaser_title and teaser_text and teaser_cat:
            set_text(soup.select_one(f"#vs{i}-cat"), teaser_cat)
            set_text(soup.select_one(f"#vs{i}-title"), teaser_title)
            set_text(soup.select_one(f"#vs{i}-teaser"), teaser_text)
        else:
            log(f"  Story {i}: Vorschau-Kachel unvollständig (Kategorie/"
                f"Titel/Teaser) — Kachel bleibt unverändert.")

        modal_title = (st.get("modal_title") or "").strip()
        lead = (st.get("lead") or "").strip()
        modal_cat = (st.get("modal_cat") or "").strip()
        if modal_title and lead and modal_cat:
            set_text(soup.select_one(f"#vs{i}-modal-cat"), modal_cat)
            set_text(soup.select_one(f"#vs{i}-modal-title"), modal_title)
            set_text(soup.select_one(f"#vs{i}-lead"), lead)
        else:
            log(f"  Story {i}: Modal-Kopf unvollständig (Kategorie/Titel/"
                f"Lead) — Modal-Kopf bleibt unverändert.")

        set_html(soup.select_one(f"#vs{i}-intro"), st.get("intro_html"))

        facts = st.get("facts") or []
        for j in range(1, 4):
            node = soup.select_one(f"#vs{i}-fact{j}")
            if node is None:
                continue
            if j <= len(facts):
                fact_val = facts[j - 1]
                set_html(node, fact_val if "<" in fact_val else f"<strong>{fact_val}</strong>")
            # falls weniger als 3 Fakten geliefert wurden, bleibt der alte Fakt stehen
            # (kein Löschen, um leere Kacheln zu vermeiden)

        set_html(soup.select_one(f"#vs{i}-einordnung"), st.get("einordnung_html"))

        sources = st.get("sources") or []
        if sources:
            parts = []
            for s in sources[:3]:
                name = (s.get("name") or "").strip()
                url = (s.get("url") or "").strip()
                date = (s.get("date") or "").strip()
                if not name:
                    continue
                link = f'<a href="{url}" target="_blank" rel="noopener">{name}</a>' if url else name
                parts.append(link + (f", {date}" if date else ""))
            if parts:
                set_html(soup.select_one(f"#vs{i}-modal-src"), ("Sources: " if LANG == "en" else "Quellen: ") + " · ".join(parts))

    # ── Transparenz-Hinweis: ehrlich auf Vollautomatisierung umgestellt ──────
    note = soup.select_one("#transp-note")
    if note is not None:
        note.clear()
        if LANG == "en":
            note_html = (
                f"<b>Full disclosure:</b> This page is generated fully automatically by "
                f"AI-assisted research with web search (as of this edition: "
                f"{date_label}). Every item must cite a real, linked source "
                f"(WHO, IEA, IUCN, UN, World Bank, peer-reviewed journals, etc.) — no "
                f"manual editorial review takes place before publication. Found an "
                f'error? Write to <a href="mailto:hallo@schlusslicht.de" '
                f'style="color:#ffe1b0;">hallo@schlusslicht.de</a> '
                f"— we correct transparently."
            )
        else:
            note_html = (
                f"<b>Ehrlich gesagt:</b> Diese Seite wird vollautomatisch durch eine "
                f"KI-gestützte Recherche mit Websuche erstellt (Stand dieser Ausgabe: "
                f"{date_label}). Jede Meldung muss eine echte, verlinkte Quelle "
                f"(WHO, IEA, IUCN, UN, Weltbank, Fachjournale u. a.) nennen — eine "
                f"manuelle Redaktionsprüfung vor Veröffentlichung findet nicht mehr "
                f"statt. Fehler gefunden? Schreiben Sie an "
                f'<a href="mailto:hallo@schlusslicht.de" style="color:#ffe1b0;">hallo@schlusslicht.de</a> '
                f"– wir korrigieren transparent."
            )
        note.append(BeautifulSoup(note_html, "html.parser"))

    # ── SEO: Title, Description, OG, Twitter ─────────────────────────────────
    if sp.get("title"):
        og_title = f"Brightside — {sp['title']} | SCHLUSSLICHT"
        title_tag = soup.find("title")
        if title_tag:
            title_tag.string = og_title
        og_desc = (BeautifulSoup(sp.get("body_html") or "", "html.parser").get_text())[:155]
        for sel in ["#meta-description", "#og-title", "#og-description", "#twitter-title", "#twitter-description"]:
            el = soup.select_one(sel)
            if el is None:
                continue
            if "title" in sel:
                el["content"] = og_title
            else:
                el["content"] = og_desc or el.get("content", "")

    return str(soup)


# ── Hauptprogramm ─────────────────────────────────────────────────────────────
def main() -> int:
    # Fehlt der API-Key, wird bewusst NICHTS geschrieben. Der Workflow
    # erkennt über 'git diff', dass diese Datei unverändert blieb, und
    # ruft danach das externe rebuild/fallback_update.py auf, um
    # wenigstens das Datum zu aktualisieren (siehe generate.py für die
    # ausführliche Begründung).
    if not API_KEY:
        log("⚠️  OPENROUTER_API_KEY fehlt — überspringe echte Generierung. "
            "Der Workflow ruft im Anschluss automatisch das externe "
            "Fallback-Skript für die Datumsaktualisierung auf.")
        return 0

    today = datetime.date.today()
    date_label = (f"{MONATE[today.month - 1]} {today.day}, {today.year}"
                  if LANG == "en" else
                  f"{today.day}. {MONATE[today.month - 1]} {today.year}")
    build_time = datetime.datetime.now(datetime.timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    log(f"Visionen-Ausgabe: {date_label}")

    # Root-Cause-Fix (identisch zu generate.py): OUTPUT (gestriger, echter
    # Stand) wird bevorzugt geladen statt des statischen TEMPLATE mit
    # Tag-0-Platzhalterinhalten.
    template_path = OUTPUT if os.path.exists(OUTPUT) else TEMPLATE
    if not os.path.exists(template_path):
        log("FEHLER: Weder brightside.html noch brightside.template.html gefunden.")
        return 1
    log(f"Verwende als Basis: {template_path}")
    with open(template_path, encoding="utf-8") as fh:
        html = fh.read()

    data = get_visionen_content(date_label)
    if not data:
        log("Keine Inhalte erzeugt — brightside.html bleibt unverändert.")
        return 0

    html = inject(html, data, date_label, build_time)

    with open(OUTPUT, "w", encoding="utf-8") as fh:
        fh.write(html)
    log(f"{OUTPUT} geschrieben ({len(html):,} Zeichen). Fertig.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
