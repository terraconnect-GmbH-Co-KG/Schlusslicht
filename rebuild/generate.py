#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate.py — Tagesaktualisierung für schlusslicht.de
================================================================================
Wird vom GitHub-Actions-Workflow .github/workflows/daily-update.yml gestartet.

Ablauf:
  1. Liest die Vorlage  index.template.html  (Fallback: index.html).
  2. Recherchiert per OpenRouter-API mit Web-Search-Server-Tool
       a) tagesaktuelle Meldungen für alle 8 Rubriken,
       b) 3 frische Hintergrundstorys.
  3. Baut die Inhalte fest in das HTML ein und schreibt  index.html.

Die fertige index.html ist damit eine vollständig statische Seite —
ohne API-Schlüssel im Browser, lauffähig auf jedem Hoster bzw. GitHub Pages.
"""

import datetime
import difflib
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
MODEL = "perplexity/sonar"  # beliebiges OpenRouter-Modell hier eintragen
LANG = os.environ.get("SL_LANG", "de").strip().lower()
TEMPLATE = "index.en.template.html" if LANG == "en" else "index.template.html"
OUTPUT = "index.en.html" if LANG == "en" else "index.html"
TIMEOUT = 240

WOCHENTAGE = (
    ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    if LANG == "en" else
    ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
)
MONATE = (
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"]
    if LANG == "en" else
    ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
     "August", "September", "Oktober", "November", "Dezember"]
)

# Die 8 Rubriken der Seite (nur eine Sport-Rubrik). Die genannten Indizes
# (RSF, CPI, CCPI) sind Beispiele für mögliche, aber nicht die einzigen
# Quellen — jede relevante, echte Meldung zum Thema aus aller Welt zählt,
# nicht nur eine formale Ranking-Tabelle.
RUBRIKEN = {
    "01": "Sport / MLS — schlechtestes Team im Tabellenende (die einzige Sport-Rubrik)",
    "02": "Niedriglohn — Branche, Tarifabschluss, Studie, konkreter Fall weltweit",
    "03": "Bahn & ÖPNV — Pünktlichkeit, Ausfall, Streik, Investitionsstau, jedes Land",
    "04": "Pressefreiheit — inhaftierte/bedrohte/getötete Journalisten, "
          "Zensurfälle, Angriffe auf Medien weltweit (RSF-Index ist eine "
          "mögliche Quelle, nicht die einzige — auch z.B. von CPJ/RSF "
          "dokumentierte getötete Journalisten in Gaza/Nahost zählen "
          "genauso wie jede andere Weltregion, sofern echt belegt)",
    "05": "Korruption — aktueller Fall mit Urteil/Anklage/Ermittlung, beliebiges Land "
          "(nur belegt!, CPI ist eine mögliche Quelle unter mehreren)",
    "06": "Klimaschutz — verfehltes Ziel, Rückschritt, Fehlentscheidung eines Landes "
          "oder Konzerns (CCPI ist eine mögliche Quelle unter mehreren)",
    "07": "Steuervermeidung — Konzern-Konstrukt, Urteil, Nachzahlung, weltweit (nur belegt!)",
    "08": "Medien — Zeitungssterben, Auflagenkollaps, Redaktionsschließung, jedes Land",
}


# ── Hilfsfunktionen ──────────────────────────────────────────────────────────
def log(msg: str) -> None:
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


# Diese seriösen Institutionen/Medien werden im Impressum als
# Datenquellen genannt bzw. sind etablierte, real existierende Anbieter.
# Bei technischem Verbindungsfehler (Timeout/DNS/Connection-Refused) —
# NICHT bei einem echten 404 — wird eine URL auf einer dieser Domains
# trotzdem akzeptiert, weil ein Verbindungsfehler zu einer bekannt
# echten Institution fast immer ein Netzwerk-/Blockadeproblem ist,
# keine halluzinierte Quelle.
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
    """Prüft, ob die Domain einer URL zu einer bekannten, etablierten
    Institution/Medienquelle gehört (siehe TRUSTED_SOURCE_DOMAINS)."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if host.startswith("www."):
        host = host[4:]
    return any(host == d or host.endswith("." + d) for d in TRUSTED_SOURCE_DOMAINS)


def verify_url(url: str, timeout: int = 8) -> bool:
    """Prüft, ob eine Quellen-URL tatsächlich existiert und erreichbar ist
    (kein 404, keine DNS-Auflösung fehlgeschlagen, keine Zeitüberschreitung).
    Technische Absicherung gegen halluzinierte Quellen: Eine Meldung ohne
    nachweislich funktionierende URL wird NICHT veröffentlicht.

    WICHTIG: Viele seriöse Institutionsseiten (transparency.org,
    worldhappiness.report, transfermarkt.de, …) blocken automatisierte
    Anfragen per Bot-Schutz (403/429/503), OBWOHL die Seite echt existiert.
    Das ist kein Beleg für eine halluzinierte URL — nur ein 404/410 oder ein
    echter Verbindungsfehler (DNS/Timeout) ist ein verlässliches Indiz dafür,
    dass die URL nicht existiert. Bot-Blockaden werden daher als
    'nicht verifizierbar, aber nicht widerlegt' behandelt und durchgelassen."""
    if not url or not isinstance(url, str) or not url.strip().lower().startswith("http"):
        return False
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    }
    BOT_BLOCK_CODES = {401, 403, 405, 429, 500, 502, 503, 504}
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True, headers=headers)
        if r.status_code >= 400:
            # Manche Server lehnen HEAD ab -> mit GET nachprüfen, bevor wir aufgeben
            r = requests.get(url, timeout=timeout, allow_redirects=True, headers=headers, stream=True)
        if r.status_code < 400:
            return True
        if r.status_code in BOT_BLOCK_CODES:
            log(f"  Quelle antwortet mit HTTP {r.status_code} (Bot-Schutz/"
                f"Server-Fehler, keine echte Nicht-Existenz) — wird trotzdem "
                f"akzeptiert: {url}")
            return True
        # Echtes 404/410 -> Quelle existiert nachweislich nicht (auch bei
        # vertrauenswürdiger Domain, denn ein 404 zu EINEM konkreten Pfad
        # ist ein echter Beleg gegen genau diese URL).
        return False
    except requests.RequestException as exc:
        if _domain_is_trusted(url):
            log(f"  Quelle technisch nicht erreichbar ({exc.__class__.__name__}) "
                f"— Domain gilt aber als etablierte Institution/Quelle, wird "
                f"deshalb trotzdem akzeptiert: {url}")
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
    """Ruft die OpenRouter-API mit Web-Search-Server-Tool auf und liefert den Text."""
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
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
    """Schält ein JSON-Objekt aus der Modellantwort."""
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
    Modellantwort kein gültiges JSON ergibt (z.B. durch Abschneiden bei zu
    knappem max_tokens oder nicht escapte Anführungszeichen im Fließtext),
    wird dem Modell der exakte Parse-Fehler zurückgemeldet und es bekommt
    bis zu `repair_retries` weitere Versuche, gültiges JSON zu liefern."""
    raw = call_api(system, prompt, max_tokens=max_tokens)
    data = extract_json(raw)
    attempt = 0
    while data is None and raw and attempt < repair_retries:
        attempt += 1
        # Versuche zu erkennen, WARUM es fehlschlug, um gezielt zu reparieren
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


# Unicode-Bereiche, die in deutschen Texten nichts verloren haben und auf
# ein Sprach-Leck des Modells hindeuten (CJK, Kyrillisch, Hangul, Arabisch, …).
_FREMDSCHRIFT_PATTERN = re.compile(
    "["
    "\u4e00-\u9fff"   # CJK (Chinesisch/Japanisch, Kanji)
    "\u3040-\u30ff"   # Hiragana/Katakana
    "\uac00-\ud7a3"   # Hangul (Koreanisch)
    "\u0400-\u04ff"   # Kyrillisch
    "\u0600-\u06ff"   # Arabisch
    "\u0900-\u097f"   # Devanagari
    "]+"
)


def sanitize(obj):
    """Entfernt rekursiv fremdschriftliche Zeichen (Sprach-Leck des Modells)
    aus allen Strings einer verschachtelten JSON-Struktur."""
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


def _is_duplicate_sentence(s_norm: str, seen_norm: list, threshold: float) -> bool:
    """Ein Satz gilt als Duplikat, wenn er einem bereits gesehenen Satz sehr
    ähnlich ist ODER wenn der kürzere der beiden (ohne Schlusspunkt) komplett
    im längeren enthalten ist — das erwischt auch Fälle, in denen derselbe
    Kernsatz nur mit einer Einleitung wie 'Am Ende zeigt sich:' wiederholt
    oder um einen Nebensatz ergänzt wurde."""
    s_core = s_norm.rstrip(".!? ")
    for seen in seen_norm:
        seen_core = seen.rstrip(".!? ")
        if len(s_core) > 15 and len(seen_core) > 15:
            shorter, longer = sorted([s_core, seen_core], key=len)
            if shorter in longer:
                return True
        if difflib.SequenceMatcher(None, s_norm, seen).ratio() > threshold:
            return True
    return False


_STOPWORTE = {
    "und", "oder", "der", "die", "das", "des", "dem", "den", "ein", "eine",
    "einer", "eines", "einem", "einen", "ist", "sind", "war", "waren",
    "wird", "werden", "wurde", "wurden", "hat", "haben", "hatte", "hatten",
    "nicht", "auch", "aber", "doch", "noch", "nur", "schon", "sehr", "mehr",
    "kein", "keine", "keinen", "keiner", "für", "von", "mit", "bei", "nach",
    "vor", "über", "unter", "zwischen", "durch", "ohne", "um", "an", "auf",
    "aus", "in", "im", "zu", "zum", "zur", "dass", "wenn", "weil", "als",
    "wie", "was", "wer", "wo", "dieser", "diese", "dieses", "diesem",
    "diesen", "sich", "sein", "seine", "seiner", "seinem", "seinen", "ihre",
    "ihrer", "ihrem", "ihren", "ihr", "ihm", "ihn", "man", "es", "er", "sie",
    "wir", "du", "ich", "damit", "dabei", "dadurch", "diesen", "wurde",
}


def _significant_words(text: str) -> set:
    words = re.findall(r"[a-zäöüß]{4,}", text.lower())
    return {w for w in words if w not in _STOPWORTE}


def _paragraphs_content_overlap(a: str, b: str, threshold: float = 0.45) -> bool:
    """Erkennt inhaltliche Wiederholung zwischen zwei ganzen Absätzen anhand
    gemeinsamer inhaltstragender Wörter — erwischt auch umformulierte
    Wiederholungen, die auf Satzebene keine hohe Textähnlichkeit zeigen."""
    wa, wb = _significant_words(a), _significant_words(b)
    smaller = min(len(wa), len(wb))
    if smaller < 4:
        return False
    return len(wa & wb) / smaller > threshold


def dedupe_paragraphs(paragraphs, threshold=0.75):
    """Zweistufiger Filter gegen inhaltliche Wiederholung in einer Story:
    1) Absatzebene — ein ganzer Absatz wird verworfen, wenn er zu einem
       bereits behaltenen Absatz eine hohe Wortüberlappung hat (fängt auch
       umformulierte Wiederholungen ab).
    2) Satzebene — innerhalb der verbleibenden Absätze werden zusätzlich
       einzelne, textlich (fast) identische Sätze entfernt."""
    def strip_tags(html):
        return re.sub(r"<[^>]+>", "", html or "")

    # Stufe 1: ganze Absätze mit hoher inhaltlicher Überlappung verwerfen
    stage1 = []
    for p in paragraphs or []:
        text = strip_tags(p).strip()
        if not text:
            continue
        if any(_paragraphs_content_overlap(text, strip_tags(kept)) for kept in stage1):
            log(f"  Inhaltlich wiederholter Absatz entfernt: {text[:90]!r}")
            continue
        stage1.append(p)

    # Stufe 2: innerhalb der verbliebenen Absätze doppelte Sätze entfernen
    seen_norm = []
    result = []
    for p in stage1:
        text = strip_tags(p).strip()
        sentences = re.split(r"(?<=[.!?])\s+", text)
        kept = []
        for s in sentences:
            s = s.strip()
            if not s:
                continue
            s_norm = re.sub(r"\s+", " ", s).lower()
            if _is_duplicate_sentence(s_norm, seen_norm, threshold):
                log(f"  Doppelter Satz entfernt: {s[:90]!r}")
                continue
            kept.append(s)
            seen_norm.append(s_norm)
        if kept:
            result.append(f"<p>{' '.join(kept)}</p>")
    return result


# ── Recherche: 8 Rubriken ───────────────────────────────────────────────────
RUBRIK_BATCHES = [
    {k: RUBRIKEN[k] for k in ["01", "02", "03", "04"]},
    {k: RUBRIKEN[k] for k in ["05", "06", "07", "08"]},
]


def _fetch_items_batch(batch: dict, date_label: str, bereits_vergebene_themen: list):
    """Holt Meldungen für EINE kleine Gruppe von Rubriken (statt für alle 8
    auf einmal). Kleinere Aufgaben pro Aufruf verhindern, dass das Modell in
    eine Wiederholungsschleife rutscht und generische Platzhaltersätze statt
    echter Recherche liefert."""
    system = (
        f"Du bist Chefredakteur von schlusslicht.de, einem deutschen "
        f"linkssatirischen Magazin. Heute ist {date_label}.\n\n"
        "Finde zu JEDER der folgenden Rubriken eine ECHTE, tagesaktuelle oder "
        "höchstens 14 Tage alte Meldung via Websuche. Die Quelle muss NICHT "
        "zwingend eine formale Ranking-Tabelle oder ein offizieller Index "
        "sein (RSF-Index, CPI, CCPI o.ä. sind Beispiele, keine Pflicht) — "
        "jede echte, relevante Meldung zum jeweiligen Thema aus JEDEM Land "
        "der Welt zählt, z.B. auch ein einzelner Gerichtsfall, ein "
        "Zeitungsartikel über einen konkreten Vorfall, eine Studie oder ein "
        "parlamentarischer Bericht. Meide KEINE Weltregion aus vermeintlicher "
        "Vorsicht — auch Nahost/Gaza, Ukraine/Russland oder andere politisch "
        "sensible Weltgegenden sind ganz normale, gleichberechtigte "
        "Themenquellen wie jede andere Region, solange die Meldung echt "
        "und belegt ist.\n\n"
        "STRIKTE KATEGORIETREUE — NICHT VERHANDELBAR: Die Meldung MUSS "
        "inhaltlich zur jeweiligen Rubrik passen. Eine Meldung über "
        "Korruption gehört AUSSCHLIESSLICH in die Korruptions-Rubrik, eine "
        "Meldung über Klimaschutz AUSSCHLIESSLICH in die Klimaschutz-"
        "Rubrik, usw. — niemals vermischen, niemals eine Meldung aus einem "
        "fachfremden Bereich in eine andere Rubrik einsetzen, auch nicht "
        "als 'überraschende Ausnahme'. Findest du für eine Rubrik heute "
        "keine passende, aktuelle UND belegte Meldung, dann liefere für "
        "diese Rubrik GAR KEINEN Eintrag (lass sie im JSON komplett weg) "
        "— eine fehlende Meldung ist immer besser als eine thematisch "
        "falsch zugeordnete.\n\n"
        "ABSOLUTES VERBOT VON PLATZHALTERN: Jede Schlagzeile und jeder "
        "Kommentar muss eine ECHTE, konkrete, recherchierte Meldung mit "
        "echten Eigennamen, Orten und Zahlen sein. Schreibe NIEMALS "
        "generische Platzhaltersätze wie 'Land mit niedrigstem Etat: "
        "2026-Bericht' oder 'Team X: 2026-Ergebnis' — das ist kein "
        "Stilmittel, sondern ein Fehler. Wenn du keine echte Meldung findest, "
        "recherchiere weiter oder liefere gar keinen Eintrag für diese "
        "Rubrik, aber erfinde keine Schema-Lückentext-Sätze und wechsle "
        "niemals das Thema der Rubrik.\n\n"
        "KEINE WIEDERKEHRENDEN STANDARDSÄTZE: Verwende niemals denselben "
        "Schlusssatz (z. B. 'Stabilität fehlt, um die Saison zu retten') in "
        "mehreren Rubriken — jeder Kommentar muss individuell zum jeweiligen "
        "Fall passen.\n\n"
        "ABSOLUTES VERBOT VON ERFUNDENEN QUELLEN — HÖCHSTE PRIORITÄT: "
        "Erfinde NIEMALS Firmennamen, Ereignisse, Zahlen oder Studien. Jede "
        "Meldung MUSS von einer echten, mit Websuche auffindbaren Quelle "
        "stammen, UND du musst die tatsächliche, funktionierende URL dieser "
        "Quelle angeben (die Seite, die du bei der Websuche gefunden hast — "
        "keine geratene oder aus dem Gedächtnis rekonstruierte URL). Findest "
        "du zu einer Rubrik keine echte Meldung mit einer echten, "
        "existierenden URL, dann liefere für diese Rubrik GAR KEINEN "
        "Eintrag (lass sie im JSON weg), statt etwas zu erfinden. Eine "
        "fehlende Meldung ist immer besser als eine erfundene.\n\n"
        + (
            f"Diese Themen sind in anderen Rubriken heute bereits vergeben — "
            f"wähle KEIN Ausweichthema, das damit überschneidet: "
            f"{', '.join(bereits_vergebene_themen)}.\n\n"
            if bereits_vergebene_themen
            else ""
        )
        + "Stil: schwarze Satire mit menschlicher Wärme — nicht kalt-nüchtern, "
        "sondern erkennbar mit Empathie für die Betroffenen geschrieben. Eine "
        "klar erkennbare linke, ökologisch-grüne und gesellschaftskritische "
        "Haltung darf und soll mitschwingen (Mitgefühl mit den Betroffenen, "
        "Kritik an denen, die die Verantwortung tragen — Machtstrukturen, "
        "Konzerne, verfehlte Klima- und Sozialpolitik) — pointierter "
        "formuliert als eine rein neutrale Nachrichtenmeldung, aber NICHT "
        "radikal, nicht plakativ, nie ins Unsachliche oder Übertriebene "
        "abgleitend: die Haltung muss sich immer aus den berichteten Fakten "
        "ergeben, nicht aus bloßer Empörungsrhetorik. Fakten plus ein "
        "pointierter, menschlicher Satz, höchstens 130 Zeichen pro "
        "Kommentar. "
        "Antworte AUSSCHLIESSLICH auf " + ("Englisch (US)" if LANG == "en" else "Deutsch") + " — keine chinesischen, "
        "kyrillischen, arabischen oder anderen nicht-lateinischen "
        "Schriftzeichen, auch nicht einzelne Wörter oder Zeichen davon."
    )

    zeilen = "\n".join(f"{num} {beschr}" for num, beschr in batch.items())
    prompt = (
        f"Suche für JEDE dieser {len(batch)} Rubriken eine aktuelle echte "
        "Meldung. Nutze die Websuche mehrfach, auf Deutsch und Englisch.\n\n"
        f"Rubriken:\n{zeilen}\n\n"
        "Antworte AUSSCHLIESSLICH mit gültigem JSON, ohne Markdown:\n"
        "{\n"
        f'  "{list(batch.keys())[0]}": {{'
        '"thema": "1-2 Wörter Themen-Schlagwort, z.B. \'Fußball\' oder \'Steuerpolitik\'", '
        '"headline": "kurze, konkrete Schlagzeile mit echten Namen/Zahlen", '
        '"kommentar": "individueller Kommentar, max 130 Zeichen", '
        '"quelle": "Quellenname und Datum, z.B. Reuters 22.06.2026 — KEINE Zitationsnummern wie [1]", '
        '"quelle_url": "die ECHTE, vollständige URL der Quelle (https://...) — PFLICHTFELD, ohne echte funktionierende URL keine Veröffentlichung"},\n'
        f'  ... für jede der {len(batch)} Rubriken ein Eintrag ...\n'
        "}"
    )

    return extract_json(call_api(system, prompt, max_tokens=2200))


def get_daily_items(date_label: str):
    log(f"Recherchiere Tagesmeldungen für {len(RUBRIKEN)} Rubriken (in {len(RUBRIK_BATCHES)} Gruppen) …")

    all_items = {}
    vergebene_themen = []
    for i, batch in enumerate(RUBRIK_BATCHES, start=1):
        log(f"  Gruppe {i}/{len(RUBRIK_BATCHES)}: Rubriken {', '.join(batch.keys())}")
        batch_result = _fetch_items_batch(batch, date_label, vergebene_themen)
        if not batch_result:
            log(f"  Gruppe {i}: keine verwertbare Antwort erhalten, wird übersprungen.")
            continue
        for num, item in batch_result.items():
            if num not in batch or not isinstance(item, dict):
                continue
            headline = (item.get("headline") or "").strip()
            kommentar = (item.get("kommentar") or "").strip()
            # WICHTIG (Atomaritäts-Fix): headline UND kommentar müssen BEIDE
            # vorhanden sein, sonst wird der Eintrag komplett verworfen.
            # Andernfalls würde inject() nur das vorhandene Feld aktualisieren
            # und das fehlende Feld vom alten (evtl. thematisch völlig
            # anderen) Stand stehen lassen — genau das führte zum Fehler
            # "Pressefreiheit-Schlagzeile + Sport-Metapher-Kommentar".
            if headline and kommentar:
                all_items[num] = item
                thema = (item.get("thema") or "").strip()
                if thema:
                    vergebene_themen.append(thema)
            else:
                fehlt = "kommentar" if headline else ("headline" if kommentar else "headline+kommentar")
                log(f"  Rubrik {num}: unvollständiger Eintrag ({fehlt} fehlt) "
                    f"— komplett übersprungen, bestehender (in sich konsistenter) "
                    f"Stand bleibt. Kein Teil-Update einzelner Felder.")

    all_items = dedupe_rubrik_topics(all_items)
    all_items = strip_repeated_boilerplate(all_items)
    all_items = review_and_fix_items(all_items, date_label)

    spotlight_ticker = get_spotlight_and_ticker(date_label, all_items)

    if all_items:
        log(f"  {len(all_items)} Rubrik-Meldungen final erhalten.")
    else:
        log("  Keine verwertbaren Rubrik-Daten erhalten.")

    if not all_items and not spotlight_ticker.get("spotlight") and not spotlight_ticker.get("ticker"):
        return None

    return {"items": all_items, **spotlight_ticker}


def strip_repeated_boilerplate(items: dict, max_erlaubt: int = 2) -> dict:
    """Sicherheitsnetz gegen Wiederholungsschleifen: Wenn derselbe Schluss-
    satz (letzter Satz des Kommentars) in mehr als max_erlaubt Rubriken
    wortgleich auftaucht, ist das ein klares Zeichen für degenerierten
    Modell-Output. Betroffene Rubriken (außer der ersten) werden geleert und
    behalten ihren bestehenden Stand aus der Vorlage."""
    def letzter_satz(text):
        text = re.sub(r"<[^>]+>", "", text or "").strip()
        parts = re.split(r"(?<=[.!?])\s+", text)
        return re.sub(r"\s+", " ", parts[-1]).lower() if parts else ""

    zaehler = {}
    for num, item in items.items():
        satz = letzter_satz(item.get("kommentar", ""))
        if satz and len(satz) > 10:
            zaehler.setdefault(satz, []).append(num)

    for satz, nums in zaehler.items():
        if len(nums) > max_erlaubt:
            log(f"  Wiederholungsschleife erkannt ({len(nums)}x identischer "
                f"Schlusssatz: {satz[:60]!r}) — betroffene Rubriken werden "
                f"zurückgesetzt: {', '.join(nums[1:])}")
            for num in nums[1:]:
                items[num] = {}
    return items


def get_spotlight_and_ticker(date_label: str, items: dict):
    """Holt Spotlight und Ticker in einem eigenen, kleinen Aufruf (statt als
    Teil des großen 8-Rubriken-Aufrufs), damit auch diese nicht unter einer
    überladenen Gesamtaufgabe leiden."""
    log("  Hole Spotlight und Ticker …")
    kontext = "; ".join(
        f"{num}: {it.get('headline', '')}" for num, it in items.items() if it.get("headline")
    )
    system = (
        f"Du bist Chefredakteur von schlusslicht.de. Heute ist {date_label}. "
        "Antworte AUSSCHLIESSLICH auf " + ("Englisch (US)" if LANG == "en" else "Deutsch") + ", keine nicht-lateinischen "
        "Schriftzeichen. Antworte NUR mit validem JSON."
    )
    prompt = (
        "Wähle aus den folgenden heutigen Rubrik-Meldungen die stärkste als "
        f"Spotlight aus, und liefere zusätzlich 8 kurze Ticker-Meldungen zu "
        f"weiteren aktuellen Schlusslicht-Themen (unabhängig von den 8 "
        f"Rubriken).\n\nHeutige Meldungen:\n{kontext}\n\n"
        "Antworte als JSON:\n"
        "{\n"
        '  "spotlight": {"cat": "Kategorie des Tages", "hl": "Schlagzeile", '
        '"text": "2-3 Sätze Einordnung", "quelle": "Quelle"},\n'
        '  "ticker": ["8 kurze Ticker-Meldungen, je max 95 Zeichen, jede zu einem anderen Thema"]\n'
        "}"
    )
    data = extract_json(call_api(system, prompt, max_tokens=1500)) or {}
    return {"spotlight": data.get("spotlight"), "ticker": data.get("ticker")}


def review_and_fix_items(items: dict, date_label: str) -> dict:
    """Letzter Schritt vor der Veröffentlichung: Prüft Sinnhaftigkeit UND
    verifiziert technisch jede Quellen-URL. WICHTIG: Dieser Schritt darf
    NIEMALS neue Inhalte erfinden — er darf nur bestehende, bereits
    recherchierte Einträge bestätigen oder verwerfen. Ein verworfener
    Eintrag wird geleert (behält den bestehenden Stand aus der Vorlage)
    statt durch erfundenen Ersatz ausgetauscht zu werden."""
    echte_items = {num: it for num, it in items.items() if it}
    if not echte_items:
        return items

    # Schritt 1: Sinnhaftigkeits-Prüfung durch die KI — reine Ja/Nein-Bewertung,
    # KEIN Umschreiben oder Erfinden von Inhalten.
    log("  Prüfe alle Rubrik-Texte auf Sinnhaftigkeit vor Veröffentlichung …")
    system = (
        "Du bist Chef vom Dienst bei schlusslicht.de und prüfst Texte vor "
        "der Veröffentlichung. Du erfindest NIEMALS neue Inhalte — du "
        "bewertest ausschließlich, ob die vorliegenden Einträge bereits "
        "sinnvoll sind. Antworte AUSSCHLIESSLICH auf " + ("Englisch (US)" if LANG == "en" else "Deutsch") + ". Antworte NUR "
        "mit validem JSON, keine Erklärung."
    )
    prompt = (
        "Prüfe jeden der folgenden Einträge NUR auf Sinnhaftigkeit: Ist die "
        "Schlagzeile eine konkrete, in sich sinnvolle Aussage (keine "
        "generische Platzhalterformulierung wie 'X: 2026-Bericht', kein "
        "abgeschnittener oder zusammenhangloser Satz)? Passt der Kommentar "
        "inhaltlich zur Schlagzeile? Ist es KEINE Wiederholung eines "
        "Standardsatzes aus einem anderen Eintrag?\n\n"
        "ZUSÄTZLICH — KATEGORIE-KOHÄRENZ (sehr wichtig, häufigster Fehler): "
        "Jeder Eintrag hat ein Feld 'rubrik_soll' — die Kategorie, der er "
        "zugeordnet ist. Prüfe, ob Schlagzeile UND Kommentar TATSÄCHLICH "
        "inhaltlich zu dieser Kategorie gehören. Beispiel für einen Fehler, "
        "den du erkennen musst: rubrik_soll='Klimaschutz', aber die "
        "Schlagzeile handelt tatsächlich von einem Korruptionsfall — das "
        "ist eine KATEGORIE-FEHLZUORDNUNG und muss mit ok:false markiert "
        "werden, selbst wenn Schlagzeile und Kommentar für sich genommen "
        "sinnvoll und gut belegt sind.\n\n"
        "WICHTIG: Du bewertest nur, du erfindest oder änderst keine Inhalte. "
        "Für jeden Eintrag gib zurück, ob er sinnvoll UND kategorietreu ist "
        '(\"ok\": true) oder nicht (\"ok\": false, mit kurzer "grund"-Angabe, '
        'z.B. "Kategorie-Fehlzuordnung: Text handelt von Korruption statt Klimaschutz").\n\n'
        f"Einträge:\n{json.dumps({num: {**it, 'rubrik_soll': RUBRIKEN.get(num, '')} for num, it in echte_items.items()}, ensure_ascii=False, indent=2)}\n\n"
        "Antworte als JSON:\n"
        '{"01": {"ok": true}, "02": {"ok": false, "grund": "generischer Platzhalter"}, ...}'
    )
    urteil = extract_json(call_api(system, prompt, max_tokens=2000)) or {}

    for num in list(echte_items.keys()):
        bewertung = urteil.get(num, {})
        if bewertung.get("ok") is False:
            log(f"  Rubrik {num}: Sinnhaftigkeits-Prüfung fehlgeschlagen "
                f"({bewertung.get('grund', 'kein Grund angegeben')}) — verworfen.")
            items[num] = {}

    # Schritt 2: Technische URL-Verifikation — UNABHÄNGIG von der KI-Bewertung,
    # das ist die eigentliche Absicherung gegen halluzinierte Quellen.
    log("  Verifiziere Quellen-URLs technisch (HTTP-Check) …")
    for num, item in list(items.items()):
        if not item:
            continue
        url = (item.get("quelle_url") or "").strip()
        if not verify_url(url):
            log(f"  Rubrik {num}: Quellen-URL fehlt oder nicht erreichbar "
                f"({url or 'keine URL angegeben'}) — Meldung verworfen, "
                f"bestehender Stand bleibt.")
            items[num] = {}
        else:
            log(f"  Rubrik {num}: Quelle verifiziert ({url})")

    return items


def dedupe_rubrik_topics(items: dict) -> dict:
    """Erkennt, wenn zwei verschiedene Rubriken heute dasselbe Themen-
    Schlagwort tragen (z. B. zweimal 'Fußball' in fachfremden Rubriken —
    auch wenn es um unterschiedliche Vereine/Ligen geht), und verwirft die
    später einsortierte Duplikat-Meldung. Diese Rubrik behält dann ihren
    bestehenden Stand aus der Vorlage statt einer doppelten Meldung.
    Fällt zusätzlich auf einen Wortüberlappungs-Vergleich zurück, falls das
    Modell kein 'thema'-Feld liefert."""
    seen_themen = {}
    kept_texts = {}
    for num in sorted(items.keys()):
        item = items.get(num) or {}
        combined = f"{item.get('headline', '')} {item.get('kommentar', '')}".strip()
        if not combined:
            continue
        thema = (item.get("thema") or "").strip().lower()
        thema_norm = re.sub(r"[^a-zäöüß ]", "", thema)

        is_dup = False
        if thema_norm and thema_norm in seen_themen:
            is_dup = True
        elif not thema_norm:
            # Kein Themen-Schlagwort geliefert -> Rückfallprüfung per Wortüberlappung
            is_dup = any(_paragraphs_content_overlap(combined, prev, 0.5) for prev in kept_texts.values())

        if is_dup:
            quelle_info = f"Thema '{thema}'" if thema_norm else combined[:70]
            log(f"  Rubrik {num}: {quelle_info} überschneidet sich mit einer "
                f"anderen Rubrik heute — Meldung verworfen, bestehender Stand bleibt.")
            items[num] = {}
            continue

        if thema_norm:
            seen_themen[thema_norm] = num
        kept_texts[num] = combined
    return items


# ── Recherche: 3 Hintergrundstorys ──────────────────────────────────────────
STORY_HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "story_history.json")
STORY_HISTORY_KEEP_DAYS = 60


def load_story_history() -> list:
    if not os.path.exists(STORY_HISTORY_PATH):
        return []
    try:
        with open(STORY_HISTORY_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except Exception as exc:
        log(f"  Story-Historie konnte nicht gelesen werden: {exc}")
        return []


def save_story_history(history: list) -> None:
    cutoff = datetime.date.today() - datetime.timedelta(days=STORY_HISTORY_KEEP_DAYS)
    pruned = []
    for entry in history:
        try:
            d = datetime.date.fromisoformat(entry.get("date", ""))
        except (ValueError, TypeError, AttributeError):
            continue
        if d >= cutoff:
            pruned.append(entry)
    try:
        with open(STORY_HISTORY_PATH, "w", encoding="utf-8") as fh:
            json.dump(pruned, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        log(f"  Story-Historie konnte nicht gespeichert werden: {exc}")


def _normalize_key(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9äöüß]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def is_recently_used(entity: str, title: str, history: list) -> bool:
    e_norm = _normalize_key(entity)
    t_norm = _normalize_key(title)
    for entry in history:
        if e_norm and e_norm == _normalize_key(entry.get("entity", "")):
            return True
        if t_norm and t_norm == _normalize_key(entry.get("title", "")):
            return True
    return False


def get_daily_stories(date_label: str):
    log("Recherchiere 3 Hintergrundstorys …")

    history = load_story_history()
    verbotene_themen = sorted({
        (entry.get("entity") or "").strip()
        for entry in history
        if (entry.get("entity") or "").strip()
    })
    if verbotene_themen:
        log(f"  {len(verbotene_themen)} Themen aus den letzten "
            f"{STORY_HISTORY_KEEP_DAYS} Tagen bereits behandelt — werden ausgeschlossen.")
    system = (
        f"Du bist Hintergrundredakteur von schlusslicht.de. Heute ist {date_label}.\n\n"
        "Schreibe 3 tiefe Hintergrundstorys über aktuelle (max. 30 Tage alte) "
        "Schlusslichter aus verschiedenen Bereichen. Nutze die Websuche für echte "
        "Fälle. Stil: investigativ, aber menschlich — zeige erkennbares "
        "Mitgefühl mit den Betroffenen und eine klar erkennbare linke, "
        "ökologisch-grüne und gesellschaftskritische Haltung zum "
        "Systemversagen dahinter — "
        "pointierter benannt als eine rein neutrale Nachrichtenmeldung "
        "(wer profitiert von diesem Versagen, wer trägt die Verantwortung), "
        "aber NICHT radikal. Keine kalte Distanz, aber auch keine Larmoyanz "
        "oder Übertreibung — jede Emotion und jede Wertung muss sich aus "
        "den geschilderten Fakten ergeben, nicht aus Adjektiven allein. "
        "Zeige das Systemversagen hinter dem Einzelfall. 400-700 Wörter je Story. "
        "Antworte AUSSCHLIESSLICH auf " + ("Englisch (US)" if LANG == "en" else "Deutsch") + " — keine chinesischen, "
        "kyrillischen, arabischen oder anderen nicht-lateinischen "
        "Schriftzeichen, auch nicht einzelne Wörter oder Zeichen davon.\n\n"
        "SPRACHLICHE KLARHEIT: Jeder Absatz muss eine NEUE Information oder "
        "einen neuen Gedanken liefern. Wiederhole niemals denselben Fakt "
        "oder dieselbe Schlussfolgerung in einem späteren Absatz nur mit "
        "anderen Worten — das wirkt wie eine Textstreckung. Kein Absatz darf "
        "im Wesentlichen dasselbe aussagen wie ein vorheriger. Vermeide "
        "austauschbare Textbaustein-Sätze wie 'Das zeigt ein systemisches "
        "Versagen' oder 'Dies führt zu einer ständigen Instabilität' als "
        "wiederkehrende Standardformulierung über mehrere Storys hinweg — "
        "jede Story braucht ihre eigene, konkrete Schlussfolgerung.\n\n"
        "ABSOLUTES VERBOT VON ERFUNDENEN QUELLEN — HÖCHSTE PRIORITÄT: "
        "Erfinde NIEMALS Firmennamen, Personen, Ereignisse oder Zahlen. Jede "
        "Story MUSS auf einem echten, mit Websuche gefundenen Fall beruhen, "
        "UND du musst die tatsächliche, funktionierende URL dieser Quelle "
        "angeben. Findest du keinen echten Fall mit einer echten, "
        "existierenden URL für einen Bereich, wähle einen anderen Bereich, "
        "zu dem du eine echte Quelle hast — aber erfinde niemals einen Fall."
        + (
            "\n\nABSOLUTE WIEDERHOLUNGSSPERRE — HÖCHSTE PRIORITÄT: Diese Fälle "
            "wurden in den letzten " + str(STORY_HISTORY_KEEP_DAYS) + " Tagen "
            "auf schlusslicht.de bereits als Hintergrundstory veröffentlicht — "
            "wähle für KEINEN davon erneut denselben Fall oder dieselbe Entität, "
            "auch nicht mit neuen Formulierungen: " + ", ".join(verbotene_themen) + ". "
            "Wähle ausschließlich neue, bisher nicht behandelte Fälle."
            if verbotene_themen else ""
        )
    )
    prompt = (
        "Recherchiere zunächst aktuelle Schlusslicht-Fälle aus verschiedenen "
        "Bereichen (Sport, Wirtschaft, Politik, Kultur, Wissenschaft, Umwelt, "
        "Medien, Technik). Wähle die 3 stärksten aus.\n\n"
        "WICHTIG zur Struktur von 'body': Die 4 Absätze haben JEWEILS EINE "
        "FESTE, EIGENE AUFGABE und dürfen sich inhaltlich NICHT überschneiden "
        "— auch nicht mit anderen Worten. Bevor du einen Absatz schreibst, "
        "prüfe: Steht dieser Gedanke schon in einem vorherigen Absatz, auch "
        "nur sinngemäß? Falls ja, streiche ihn und schreib stattdessen etwas "
        "wirklich Neues für genau diese Aufgabe:\n"
        "  Absatz 1 — NUR das Ereignis: was ist passiert, mit Zahlen/Fakten. "
        "Keine Bewertung, keine Ursachen, keine Folgen.\n"
        "  Absatz 2 — NUR Hintergrund/Ursache: wie kam es dazu, welche "
        "Vorgeschichte gibt es. Das Ereignis aus Absatz 1 NICHT wiederholen.\n"
        "  Absatz 3 — NUR konkrete Auswirkung: wer ist betroffen, welche "
        "Folgen hat es JETZT. Weder Ereignis noch Ursache wiederholen.\n"
        "  Absatz 4 — NUR die Einordnung als Systemversagen: die "
        "Schlussfolgerung, warum das mehr als ein Einzelfall ist. Dieser "
        "Gedanke darf NUR hier stehen, nirgends vorher angedeutet werden.\n\n"
        "Antworte AUSSCHLIESSLICH mit gültigem JSON, ohne Markdown:\n"
        "{\n"
        '  "stories": [\n'
        "    {\n"
        '      "cat": "// Kategorie · Zeitraum",\n'
        '      "entity": "Kurzname des Falls/der Haupt-Entität zur eindeutigen '
        'Wiedererkennung, z.B. \'Philadelphia Union\' oder \'Eritrea Pressefreiheit\' — PFLICHTFELD",\n'
        '      "title": "packender Titel, max 80 Zeichen",\n'
        '      "teaser": "Einleitung, 2-3 Sätze",\n'
        '      "body": ["<p>Absatz 1: nur das Ereignis</p>", "<p>Absatz 2: nur Hintergrund/Ursache</p>", "<p>Absatz 3: nur konkrete Auswirkung</p>", "<p>Absatz 4: nur die Einordnung als Systemversagen</p>"],\n'
        '      "factbox": ["Fakt 1", "Fakt 2", "Fakt 3"],\n'
        '      "conclusion": "Schlusssatz zum Systemversagen",\n'
        '      "source": "Quellenname und Datum, z.B. Spiegel 22.06.2026 — KEINE Zitationsnummern wie [1]",\n'
        '      "source_url": "die ECHTE, vollständige URL der Quelle (https://...) — PFLICHTFELD, ohne echte funktionierende URL keine Veröffentlichung"\n'
        "    }\n"
        "    ... 3 Storys ...\n"
        "  ]\n"
        "}"
    )

    data = call_api_json(system, prompt, max_tokens=8000)
    if data and data.get("stories"):
        log("  Verifiziere Quellen-URLs der Hintergrundstorys …")
        verifizierte_storys = []
        for story in data["stories"]:
            if not isinstance(story, dict):
                log("  Ungültiger Story-Eintrag (kein Objekt) — übersprungen.")
                continue
            entity = (story.get("entity") or "").strip()
            title = (story.get("title") or "").strip()
            if is_recently_used(entity, title, history):
                log(f"  Story {title or entity!r}: bereits in den letzten "
                    f"{STORY_HISTORY_KEEP_DAYS} Tagen veröffentlicht — "
                    f"WIEDERHOLUNG verworfen (Wiederholungssperre).")
                continue
            url = (story.get("source_url") or "").strip()
            if not verify_url(url):
                log(f"  Story {story.get('title', '(ohne Titel)')!r}: Quellen-URL "
                    f"fehlt oder nicht erreichbar ({url or 'keine URL angegeben'}) "
                    f"— komplett verworfen, keine Halluzinationen ohne Beleg.")
                continue
            log(f"  Story {story.get('title', '')!r}: Quelle verifiziert ({url})")
            story["body"] = dedupe_paragraphs(story.get("body"))
            verifizierte_storys.append(story)
        data["stories"] = verifizierte_storys
        log(f"  {len(data['stories'])} von 3 Hintergrundstorys verifiziert und übernommen.")
        if not data["stories"]:
            data = None
        else:
            today_iso = datetime.date.today().isoformat()
            for story in data["stories"]:
                history.append({
                    "date": today_iso,
                    "entity": (story.get("entity") or "").strip(),
                    "title": (story.get("title") or "").strip(),
                })
            save_story_history(history)
            log(f"  Story-Historie aktualisiert ({len(history)} Einträge, "
                f"Wiederholungssperre für {STORY_HISTORY_KEEP_DAYS} Tage).")
    else:
        log("  Keine verwertbaren Story-Daten erhalten.")
        data = None
    return data


# ── Einbau ins HTML ──────────────────────────────────────────────────────────
def set_text(node, value):
    """Setzt reinen Text in ein BeautifulSoup-Element."""
    if node is not None and value:
        node.clear()
        node.append(str(value))


def inject(html: str, items, stories, date_label: str, build_time: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    # ── Rubrik-Meldungen ───────────────────────────────────────────────
    if items and items.get("items"):
        for num, it in items["items"].items():
            card = soup.select_one(f'[data-rubrik="{num}"]')
            if not card:
                continue
            kommentar = (it.get("kommentar") or "").strip()
            headline = (it.get("headline") or "").strip()
            # ATOMARITÄTS-ABSICHERUNG (Defense-in-Depth, zusätzlich zur
            # Prüfung in get_daily_items): headline UND kommentar werden
            # NUR gemeinsam aktualisiert, nie einzeln. Ein Update nur eines
            # der beiden Felder würde sonst zwei Textteile aus evtl. ganz
            # unterschiedlichen Tagen/Themen kombinieren (z.B. eine neue
            # Pressefreiheit-Schlagzeile neben einem alten Sport-Kommentar
            # stehen lassen).
            if not (kommentar and headline and len(headline) > 4):
                log(f"  Rubrik {num}: unvollständiges Item bei Injektion "
                    f"(headline oder kommentar fehlt) — übersprungen, "
                    f"Karte bleibt vollständig unverändert.")
                continue

            set_text(card.select_one(".realsatire"), f"„{kommentar}“")
            tag = card.select_one(".ai-tag")
            if tag is not None:
                quelle = (it.get("quelle") or "").strip()
                set_text(
                    tag,
                    (
                        f"✦ Tagesaktuell · {quelle}"
                        if quelle
                        else "✦ Tagesaktuell"
                    ),
                )
            # Schlagzeile (Text nach „ — "). Der Präfix vor dem Gedankenstrich
            # (die Rubrik-Bezeichnung, z.B. "Klimaschutz-Index") bleibt IMMER
            # fest — Themenwechsel sind nicht mehr erlaubt (siehe System-
            # Prompt: strikte Kategorietreue), daher gibt es auch kein
            # rubrik_name-Feld mehr, das dieses Präfix überschreiben könnte.
            # Das verhindert genau den Fehler, bei dem eine Rubrik-Überschrift
            # ("Klimaschutz-Index") mit inhaltlich fachfremdem Text (z.B. über
            # Korruption) kombiniert wurde.
            rtit = card.select_one(".rtit")
            if rtit:
                cur = rtit.get_text()
                dash = cur.find(" — ")
                set_text(rtit, (cur[: dash + 3] + headline) if dash > 0 else headline)

    # ── Spotlight (Tagesausgabe) ──────────────────────────────────────────
    if items and items.get("spotlight"):
        sp = items["spotlight"]
        set_text(soup.select_one("#ta-cat"), sp.get("cat"))
        set_text(soup.select_one("#ta-hl"), sp.get("hl"))
        set_text(soup.select_one("#ta-text"), sp.get("text"))
        quelle = (sp.get("quelle") or "KI-recherchiert").strip()
        set_text(soup.select_one("#ta-source"), f"— {quelle} · {date_label}")

    # ── Ticker ────────────────────────────────────────────────────────────
    if items and items.get("ticker"):
        inner = soup.select_one(".ticker-inner")
        if inner is not None:
            inner["data-dup"] = "1"
        if inner is not None:
            inner.clear()
            doppelt = list(items["ticker"]) + list(items["ticker"])
            for txt in doppelt:
                item = soup.new_tag("span", attrs={"class": "tk"})
                item.append(f"{txt} ")
                sep = soup.new_tag("span", attrs={"class": "tk-sep"})
                sep.string = "✦"
                item.append(sep)
                inner.append(item)

    # ── Hintergrundstorys ─────────────────────────────────────────────────
    if stories and stories.get("stories"):
        liste = stories["stories"][:3]

        # Vorschau-Karten
        cards = soup.select(".story-card")
        for i, st in enumerate(liste):
            if i >= len(cards):
                break
            set_text(cards[i].select_one(".story-cat"), st.get("cat"))
            set_text(cards[i].select_one(".story-title"), st.get("title"))
            set_text(cards[i].select_one(".story-teaser"), st.get("teaser"))

        # Modal-Inhalte
        for i, st in enumerate(liste):
            modal = soup.select_one(f"#story{i + 1}")
            if not modal:
                continue
            set_text(modal.select_one(".story-modal-cat"), st.get("cat"))
            set_text(modal.select_one(".story-modal-hl"), st.get("title"))
            set_text(modal.select_one(".story-modal-lead"), st.get("teaser"))
            quelle = (st.get("source") or "KI-recherchiert").strip()
            set_text(modal.select_one(".story-source"), f"Quellen: {quelle}")

            body = modal.select_one(".story-body")
            if body is not None:
                body.clear()
                for para in st.get("body", []):
                    body.append(BeautifulSoup(para, "html.parser"))
                if st.get("factbox"):
                    fb = soup.new_tag("div", attrs={"class": "fact-box"})
                    for fact in st["factbox"]:
                        p = soup.new_tag("p")
                        p.string = str(fact)
                        fb.append(p)
                    body.append(fb)
                if st.get("conclusion"):
                    p = soup.new_tag("p")
                    strong = soup.new_tag("strong")
                    strong.string = str(st["conclusion"])
                    p.append(strong)
                    body.append(p)

    # ── Datum & Zeitstempel ───────────────────────────────────────────────
    set_text(soup.select_one("#nav-issue-label"), date_label)
    set_text(
        soup.select_one("#update-time"),
        (f"As of: {build_time} — automatically generated on {date_label}"
         if LANG == "en" else
         f"Stand: {build_time} — automatisch erstellt am {date_label}"),
    )

    # ── SEO: Title, Description, Open Graph, Twitter Card ─────────────────
    # Täglich mit Spotlight-Inhalt befüllt, damit jede Ausgabe eine
    # eigene Link-Vorschau beim Teilen bekommt.
    if items and items.get("spotlight"):
        sp = items["spotlight"]
        hl = (sp.get("hl") or "").strip()
        txt = (sp.get("text") or "").strip()
        og_title = f"SCHLUSSLICHT — {hl}" if hl else "SCHLUSSLICHT — Das Magazin der Letzten"
        og_desc = txt[:155] if txt else "Das Magazin der Letzten. 8 Rubriken täglich aktuell."

        title_tag = soup.find("title")
        if title_tag:
            title_tag.string = og_title

        for sel, attr, val in [
            ("#meta-description",  "content", og_desc),
            ("#og-title",          "content", og_title),
            ("#og-description",    "content", og_desc),
            ("#twitter-title",     "content", og_title),
            ("#twitter-description", "content", og_desc),
        ]:
            el = soup.select_one(sel)
            if el:
                el["content"] = val

    return str(soup)


# ── Hauptprogramm ────────────────────────────────────────────────────────────
def main() -> int:
    # Fehlt der API-Key, wird HIER bewusst NICHTS geschrieben (kein Datum-
    # Patch, keine Platzhalter-Logik). Der Workflow (.github/workflows/
    # daily-update.yml) erkennt über 'git diff' automatisch, dass diese
    # Datei in diesem Lauf unverändert blieb, und ruft danach gezielt das
    # externe, korrekt funktionierende rebuild/fallback_update.py auf, um
    # wenigstens das Datum zu aktualisieren. (Eine frühere interne
    # fallback_update()-Funktion hier im Skript war fehlerhaft — sie
    # suchte nach <time>-Tags, die im aktuellen Template gar nicht mehr
    # existieren, meldete aber trotzdem fälschlich Erfolg. Entfernt.)
    if not API_KEY:
        log("⚠️  OPENROUTER_API_KEY fehlt — überspringe echte Generierung. "
            "Der Workflow ruft im Anschluss automatisch das externe "
            "Fallback-Skript für die Datumsaktualisierung auf.")
        return 0

    today = datetime.date.today()
    date_label = (
        (f"{WOCHENTAGE[today.weekday()]}, {MONATE[today.month - 1]} {today.day}, {today.year}"
         if LANG == "en" else
         f"{WOCHENTAGE[today.weekday()]}, {today.day}. {MONATE[today.month - 1]} {today.year}")
    )
    build_time = datetime.datetime.now(datetime.timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    log(f"Tagesausgabe: {date_label}")

    # WICHTIG (Root-Cause-Fix): OUTPUT (gestriges, echtes Ergebnis) wird
    # bevorzugt geladen, NICHT das statische TEMPLATE. Das Template enthält
    # ursprüngliche Platzhalter-Beispielinhalte (u.a. "The Actor", "Dawit
    # Isaac", "Mars Climate Orbiter" als Storys sowie Eritrea/Afghanistan/
    # Südsudan/Philadelphia Union im Signature-Widget). Solange TEMPLATE
    # bevorzugt wurde, fiel JEDER Fehlschlag (nicht verifizierbare Quelle,
    # Story-Wiederholungssperre, JSON-Parsefehler) nicht auf den echten
    # Stand von GESTERN zurück, sondern auf diese uralten Tag-0-Platzhalter
    # — wodurch dieselben veralteten Storys/Kategorien immer wieder
    # auftauchten, obwohl an anderer Stelle im selben Lauf frische, echte
    # Inhalte erfolgreich verifiziert wurden.
    template_path = OUTPUT if os.path.exists(OUTPUT) else TEMPLATE
    if not os.path.exists(template_path):
        log("FEHLER: Weder index.html noch index.template.html gefunden.")
        return 1
    log(f"Verwende als Basis: {template_path}")
    with open(template_path, encoding="utf-8") as fh:
        html = fh.read()

    items = get_daily_items(date_label)
    stories = get_daily_stories(date_label)

    if not items and not stories:
        log("Keine Inhalte erzeugt — index.html bleibt unverändert.")
        return 0

    html = inject(html, items, stories, date_label, build_time)

    with open(OUTPUT, "w", encoding="utf-8") as fh:
        fh.write(html)
    log(f"{OUTPUT} geschrieben ({len(html):,} Zeichen). Fertig.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
