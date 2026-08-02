#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate.py — Tagesaktualisierung für schlusslicht.de
================================================================================
Wird vom GitHub-Actions-Workflow .github/workflows/daily-update.yml gestartet.

Ablauf:
  1. Liest die letzte echte Ausgabe  index.html  (Fallback: Template).
  2. Recherchiert per OpenRouter-API mit Web-Search-Server-Tool 3 frische,
     frei gewählte Schlusslicht-Meldungen (kein Themen-Pool, keine Rotation)
     samt je einer EINGEBETTETEN Hintergrundstory zum selben Fall.
  3. Baut die Inhalte fest in das HTML ein (3 feste Slots) und schreibt
     index.html. Eine kleine Historie-Datei (story_history.json) verhindert
     Wiederholungen an den Folgetagen.

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

# Keine feste Rubriken-Liste mehr — die KI wählt jeden Tag frei 3
# thematisch unterschiedliche Bereiche (Sport, Niedriglohn, Verkehr,
# Pressefreiheit, Korruption, Klimaschutz, Steuervermeidung, Medien, oder
# jeden anderen Bereich, in dem jemand/etwas nachweislich Schlusslicht ist).


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
    """Prüft, ob eine Quellen-URL tatsächlich existiert und erreichbar ist.
    Technische Absicherung gegen halluzinierte Quellen: Eine Meldung ohne
    nachweislich funktionierende URL wird NICHT veröffentlicht.

    WICHTIG (grundlegend überarbeitet): Die KI recherchiert täglich neue,
    unterschiedliche Quellen aus aller Welt — die überwältigende Mehrheit
    davon kann NIEMALS in einer kuratierten Liste vorab erfasst werden.
    GitHub-Actions-Server werden von sehr vielen Newsseiten per Bot-Schutz
    (Cloudflare, Akamai u.ä.) geblockt, OBWOHL die Quelle real existiert.
    Eine kleine Vertrauensliste (TRUSTED_SOURCE_DOMAINS) half nur bei den
    ~40 gelisteten Domains — bei jeder anderen echten, aber geblockten
    Quelle wurde fälschlich 'existiert nicht' angenommen. Das führte dazu,
    dass grosse Teile der Seite nicht regelmässig aktualisiert wurden.

    Der robuste, verallgemeinerbare Grundsatz: Nur eine ECHTE DNS-
    Auflösungs-Fehlermeldung (die Domain selbst ist nicht registriert oder
    falsch geschrieben) ist ein verlässlicher Beleg gegen die Existenz
    einer Quelle. JEDER andere Fehler (Timeout, Connection Refused/Reset,
    Bot-Schutz-Statuscodes) bedeutet: der Server existiert und hat auf
    DNS-Ebene aufgelöst, blockiert aber nur die automatisierte Anfrage —
    das ist KEIN Beleg gegen die Existenz der Quelle. Nur ein echtes
    404/410 zu einem KONKRETEN Pfad bleibt ein Ablehnungsgrund, da das
    ein Beleg gegen genau diese URL ist (nicht gegen die Domain an sich)."""
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
            # Manche Server lehnen HEAD ab -> mit GET nachprüfen, bevor wir aufgeben
            r = requests.get(url, timeout=timeout, allow_redirects=True, headers=headers, stream=True)
        if r.status_code < 400:
            return True
        if r.status_code in BOT_BLOCK_CODES:
            log(f"  Quelle antwortet mit HTTP {r.status_code} (Bot-Schutz/"
                f"Server-Fehler, keine echte Nicht-Existenz) — wird trotzdem "
                f"akzeptiert: {url}")
            return True
        # Echtes 404/410 -> Quelle existiert nachweislich nicht (Beleg
        # gegen genau diesen Pfad, nicht gegen die Domain generell).
        log(f"  Quelle antwortet mit HTTP {r.status_code} (echte Ablehnung, "
            f"z.B. Seite nicht mehr vorhanden) — verworfen: {url}")
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


# ── Recherche: 3 frische Meldungen (kein Pool, keine Rotation) ─────────────
N_ITEMS = 6


def _fetch_fresh_items(date_label: str, avoid_entities: list, count: int, verbotene_urls: list = None, zusatzhinweis: str = ""):
    """Recherchiert 3 frische 'Schlusslicht'-Meldungen aus BELIEBIGEN
    Bereichen in einem Aufruf — inkl. der kompletten Anzeige-Daten (Icon,
    Kategorie-Label, kleine Rangliste), die früher aus 8 fest zugeteilten
    Rubriken kamen. Kein fester Themen-Pool, keine Rotation: die KI wählt
    jeden Tag frei, welche 3 Bereiche heute die stärksten Fälle liefern."""
    system = (
        f"Du bist Chefredakteur von schlusslicht.de, einem deutschen "
        f"linkssatirischen Magazin. Heute ist {date_label}.\n\n"
        f"Finde {count} ECHTE, tagesaktuelle oder höchstens 14 Tage alte "
        "'Schlusslicht'-Meldungen via Websuche — jeweils aus einem ANDEREN "
        "Bereich. GRUNDSATZ: Jede Meldung braucht einen echten VERGLEICHS-/"
        "RANKING-BEZUG (Liga-Tabelle, Index, Statistik, Preis- oder "
        "Pünktlichkeitsvergleich) — jemand/etwas ist nachweislich "
        "Schlusslicht in einem Vergleich mit anderen, nicht nur ein "
        "einzelnes, unverbundenes Ereignis. Die Rangliste (rows) muss "
        "mehrere vergleichbare Fälle/Orte/Teams/Länder zeigen, keine bloße "
        "Faktenliste zu einem Einzelfall.\n\n"
        "Themenmischung — mindestens die Hälfte der Meldungen aus (a): "
        "massentaugliche, leicht zugängliche Themen MIT Vergleichswert "
        "(Fußball/Bundesliga/Champions League, andere Ballsportarten, "
        "Fluggesellschaften-Pünktlichkeit, Handynetz-/Internet-Qualität, "
        "Streaming, Mietpreise, Lebensmittelpreise, Verkehrsstaus, "
        "Krankenkassen, Uni-Rankings, Reiseziele, Videospiele, Social "
        "Media); Rest aus (b): soziale/politisch-linke Strukturthemen "
        "(Niedriglohn, Wohnungsnot, Gewerkschafts-/Arbeitnehmerrechte, "
        "Pressefreiheit, Korruption, Konzernmacht/Lobbyismus, Klimaschutz, "
        "Steuergerechtigkeit, soziale Ungleichheit, Kinderarmut, "
        "Bildungsungleichheit, Diskriminierung, Gender Pay Gap, "
        "Sozialabbau, Rüstungsexporte, oder jeder andere Bereich mit "
        "nachweislichem strukturellem Schlusslicht). Keine Weltregion aus "
        "Vorsicht meiden — auch Nahost/Gaza, Ukraine/Russland zählen ganz "
        f"normal, solange echt, belegt und ein Vergleichsfall. Die {count} "
        "Meldungen müssen sich thematisch klar unterscheiden.\n\n"
        "NIEMALS Einzelfall-Kriminalität oder -Unglücke als Thema (Gewalttat, "
        "Streit, Betrug, Diebstahl, Unfall, Flugzeugabsturz, Brand, "
        "Naturkatastrophe) — auch mit echter, verifizierbarer Quelle nicht. "
        "Das war für schlusslicht.de nie vorgesehen: ein Vergleichs-/Ranking-"
        "Magazin, keine Polizei- oder Unfallmeldungs-Seite. Ausnahme nur bei "
        "echtem, belegtem Vergleichswert (z.B. 'Land X hat EU-weit die "
        "niedrigste Aufklärungsquote bei häuslicher Gewalt') — NIEMALS ein "
        "Bericht über einen einzelnen Vorfall mit Privatpersonen ohne "
        "Vergleichsdaten, auch nicht in abgewandelter Form. Im Zweifel: "
        "anderes Thema wählen. Vergib außerdem NIEMALS ein kicker-Label wie "
        "'Alltag', 'Lokales', 'Region · X', 'Vermischtes', 'Boulevard' oder "
        "'Panorama' (Warnsignal für Einzelfall/Boulevard ohne Themenbezug) — "
        "jedes Label braucht ein konkretes Thema (z.B. 'EU · Geldpolitik').\n\n"
        "ABSOLUTES VERBOT VON PLATZHALTERN: Jede Schlagzeile und jeder "
        "Kommentar muss eine ECHTE, konkrete, recherchierte Meldung mit "
        "echten Eigennamen, Orten und Zahlen sein. Schreibe NIEMALS "
        "generische Platzhaltersätze wie 'Land mit niedrigstem Etat: "
        "2026-Bericht' oder 'Team X: 2026-Ergebnis' — das ist kein "
        "Stilmittel, sondern ein Fehler.\n\n"
        "KEINE WIEDERKEHRENDEN STANDARDSÄTZE: Verwende niemals denselben "
        "Schlusssatz (z. B. 'Stabilität fehlt, um die Saison zu retten') in "
        "mehreren Meldungen — jeder Kommentar muss individuell zum "
        "jeweiligen Fall passen.\n\n"
        "ABSOLUTES VERBOT VON ERFUNDENEN QUELLEN — HÖCHSTE PRIORITÄT: "
        "Erfinde NIEMALS Firmennamen, Ereignisse, Zahlen oder Studien. Jede "
        "Meldung MUSS von einer echten, mit Websuche auffindbaren Quelle "
        "stammen, UND du musst die tatsächliche, funktionierende URL dieser "
        "Quelle angeben (die Seite, die du bei der Websuche gefunden hast — "
        "keine geratene oder aus dem Gedächtnis rekonstruierte URL). Auch "
        "die kleine Rangliste (rows) muss aus derselben echten Quelle "
        "stammen, nicht erfunden sein. Findest du keine echte Meldung mit "
        "einer echten, existierenden URL, dann liefere GAR KEINEN Eintrag "
        "für diesen Platz (lass ihn im JSON weg), statt etwas zu erfinden.\n\n"
        "ABSOLUTES VERBOT VON PROZESS-KOMMENTAREN — GLEICHRANGIG WICHTIG: "
        "Wenn du für einen Platz keine echte Meldung findest, ist die "
        "EINZIGE zulässige Reaktion, den Eintrag komplett wegzulassen "
        "(siehe oben). Es ist STRENG VERBOTEN, stattdessen einen Satz über "
        "die Suche selbst als Schlagzeile oder Kommentar zu schreiben — "
        "z.B. NIEMALS Formulierungen wie 'Keine verwertbare Meldung "
        "gefunden', 'Die Websuche liefert dafür heute zu wenig', 'Keine "
        "sechs eigenständigen Meldungen aus den Suchergebnissen belegbar' "
        "oder Ähnliches. Ein Satz über deinen eigenen Rechercheprozess ist "
        "KEINE Schlusslicht-Meldung, egal wie grammatikalisch korrekt er "
        "klingt — er wird automatisch erkannt und die gesamte Ausgabe "
        "verworfen. Schreibe entweder eine echte Meldung mit echten Namen "
        "und Zahlen, oder lass den Platz frei.\n\n"
        "JEDE MELDUNG BRAUCHT IHRE EIGENE QUELLE: Verwende NIEMALS dieselbe "
        f"URL für mehrere der {count} Meldungen — auch nicht eine "
        "generische Nachrichten-Übersichtsseite (z.B. eine Newsindex- oder "
        "Startseite) als austauschbares Feigenblatt für mehrere Fälle "
        "gleichzeitig. Jede Quelle muss spezifisch zu genau dem einen Fall "
        "gehören, über den sie berichtet.\n\n"
        "NUR KONKRETE ARTIKEL, KEINE ÜBERSICHTSSEITEN: Die Quellen-URL muss "
        "auf einen spezifischen Artikel mit eigenem Titel/eigener "
        "Überschrift zu GENAU diesem Fall zeigen — NIEMALS auf eine "
        "allgemeine Nachrichten-Startseite wie '.../nachrichten-100.html' "
        "oder '.../nachrichten/nachrichten-xyz-104.html' oder die "
        "Domain-Startseite ohne Pfad. Verwende außerdem NIEMALS "
        "Platzhalter-Domains wie 'example.com' — das sind keine echten "
        "Quellen, auch wenn die Domain technisch existiert.\n\n"
        + (
            f"Diese Fälle/Entitäten wurden in den letzten Tagen bereits "
            f"verwendet — wähle KEINEN davon erneut: "
            f"{', '.join(avoid_entities)}.\n\n"
            if avoid_entities
            else ""
        )
        + (
            "DIESE KONKRETEN URLs SIND DAUERHAFT GESPERRT — HÖCHSTE PRIORITÄT: "
            "Die folgenden URLs wurden bereits mehrfach fälschlich als "
            "Sammel-Quelle für mehrere verschiedene Meldungen missbraucht "
            "und dürfen UNTER KEINEN UMSTÄNDEN erneut verwendet werden, auch "
            f"nicht für eine einzelne Meldung: {', '.join(verbotene_urls)}.\n\n"
            if verbotene_urls
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

    prompt = (
        f"Recherchiere {count} eigenständige, thematisch unterschiedliche "
        "Schlusslicht-Meldungen für die heutige Ausgabe. Nutze die Websuche "
        "mehrfach, auf Deutsch und Englisch."
        + zusatzhinweis + "\n\n"
        "Antworte AUSSCHLIESSLICH mit gültigem JSON, ohne Markdown:\n"
        "{\n"
        '  "items": [\n'
        "    {\n"
        '      "entity": "Kurzname des Falls/der Haupt-Entität zur eindeutigen '
        'Wiedererkennung, z.B. \'Philadelphia Union\' oder \'Eritrea Pressefreiheit\' — PFLICHTFELD",\n'
        '      "thema": "1-2 Wörter Themen-Schlagwort, z.B. \'Fußball\' oder \'Steuerpolitik\'",\n'
        '      "kicker": "Kategorie-Label, z.B. \'Sport · MLS\' oder \'Pressefreiheit\'",\n'
        '      "icon": "ein passendes Emoji",\n'
        '      "headline": "kurze, konkrete Schlagzeile mit echten Namen/Zahlen",\n'
        '      "kommentar": "individueller Kommentar, max 130 Zeichen",\n'
        '      "table_title": "Kurztitel der Rangliste, z.B. \'MLS — Tabellenende\'",\n'
        '      "table_tag": "Zeitraum, z.B. \'Saison 2026\'",\n'
        '      "rows": [{"rank": "28", "name": "Fall/Ort A", "value": "Zahl"}, {"rank": "29", "name": "Fall/Ort B", "value": "Zahl"}, {"rank": "30", "name": "das eigentliche Schlusslicht", "value": "Zahl"}],\n'
        '      "foot": "1 Satz Einordnung/Vergleichswert für die Fußzeile",\n'
        '      "quelle": "Quellenname und Datum, z.B. Reuters 22.06.2026 — KEINE Zitationsnummern wie [1]",\n'
        '      "quelle_url": "die ECHTE, vollständige URL der Quelle (https://...) — PFLICHTFELD"\n'
        "    }\n"
        f"    // genau {count} Einträge in dieser Liste, thematisch unterschiedlich\n"
        "  ]\n"
        "}"
    )

    result = call_api_json(system, prompt, max_tokens=3000)
    if not result:
        return None
    items_list = result.get("items")
    return items_list if isinstance(items_list, list) else None


def _fetch_fallback_sport_item(date_label: str, avoid_entities: list, verbotene_urls: list = None):
    """Garantierter Fallback für einen Platz, der nach den normalen
    Versuchen in get_daily_items immer noch nicht gefüllt werden konnte.

    WICHTIG (Bugfix, gefunden nach Live-Meldung "viele Kategorien bleiben
    beim ersten Lauf leer"): Die freie Themenwahl in _fetch_fresh_items
    muss GLEICHZEITIG mehrere harte Kriterien erfüllen (echter Vergleichs-/
    Rankingbezug, keine Kriminalität/Unglücke, kein Duplikat, verifizierbare
    Einzelquelle) — das senkt die Trefferquote pro Versuch spürbar
    gegenüber der freieren Themenwahl vor der schärferen Ausrichtung. Diese
    Funktion stellt eine DEUTLICH engere, fast immer erfüllbare Alternative:
    eine Sport-Liga-Tabellenletzten-Meldung. Sportligen haben so gut wie
    immer einen aktuellen, real recherchierbaren Tabellenletzten mit
    Vergleichsdaten (Tabelle mit mehreren Teams) — das erfüllt die
    inhaltliche Ausrichtung (echter Vergleichsfall, keine Kriminalität)
    ohne Kompromisse, ist für das Modell aber eine viel engere, leichter zu
    erfüllende Aufgabe als 'irgendein echtes, unverbrauchtes, politisches
    oder massentaugliches Strukturthema'. Wird NUR aufgerufen, wenn ein
    Platz nach den regulären Versuchen noch frei ist (siehe get_daily_items) —
    kein Ersatz für die eigentliche Themenvielfalt, nur eine letzte
    Absicherung gegen leere Plätze."""
    system = (
        "Du bist Redakteur von schlusslicht.de, einem deutschen "
        f"linkssatirischen Magazin. Heute ist {date_label}.\n\n"
        "Finde GENAU EINE echte, aktuelle Sport-Liga-Tabellenletzten-"
        "Meldung — beliebige Sportart/Liga/Land/Geschlecht (Fußball, "
        "Basketball, Eishockey, Handball, Baseball, Rugby, Volleyball "
        "o.ä.), Hauptsache eine ECHTE, mit Websuche belegbare aktuelle "
        "Tabelle (höchstens 30 Tage alter Stand) mit dem tatsächlichen "
        "Tabellenletzten. Erfinde NIEMALS Teams, Ligen, Werte oder "
        "Quellen — findest du keine echte, mit Websuche belegbare Tabelle, "
        "liefere GAR KEINEN Eintrag (leeres 'items'-Array) statt etwas zu "
        "erfinden. Kein Prozess-Kommentar über die eigene Suche als "
        "Schlagzeile/Kommentar. Die Quellen-URL muss ein konkreter Artikel "
        "sein, keine generische Übersichtsseite.\n\n"
        + (
            f"Diese Fälle/Entitäten wurden zuletzt schon verwendet — wähle "
            f"KEINEN davon erneut: {', '.join(avoid_entities)}.\n\n"
            if avoid_entities else ""
        )
        + (
            f"Diese URLs sind gesperrt, NIEMALS verwenden: "
            f"{', '.join(verbotene_urls)}.\n\n"
            if verbotene_urls else ""
        )
        + "Stil: schwarze Satire mit menschlicher Wärme, klar links-"
        "gesellschaftskritisch, aber sachlich. Kommentar max 130 Zeichen. "
        "Antworte AUSSCHLIESSLICH auf " + ("Englisch (US)" if LANG == "en" else "Deutsch") +
        " — keine nicht-lateinischen Schriftzeichen."
    )
    prompt = (
        "Recherchiere GENAU EINE Sport-Liga-Tabellenletzten-Meldung. Nutze "
        "die Websuche.\n\n"
        "Antworte AUSSCHLIESSLICH mit gültigem JSON, ohne Markdown:\n"
        "{\n"
        '  "items": [\n'
        "    {\n"
        '      "entity": "Kurzname des Teams/Falls — PFLICHTFELD",\n'
        '      "thema": "Sportart, z.B. \'Fußball\'",\n'
        '      "kicker": "z.B. \'Fußball · 2. Liga\'",\n'
        '      "icon": "ein passendes Emoji",\n'
        '      "headline": "kurze, konkrete Schlagzeile mit echten Namen/Zahlen",\n'
        '      "kommentar": "individueller Kommentar, max 130 Zeichen",\n'
        '      "table_title": "Kurztitel der Rangliste",\n'
        '      "table_tag": "Zeitraum, z.B. \'Saison 2026\'",\n'
        '      "rows": [{"rank": "N-2", "name": "Team A", "value": "Zahl"}, {"rank": "N-1", "name": "Team B", "value": "Zahl"}, {"rank": "N", "name": "das Schlusslicht-Team", "value": "Zahl"}],\n'
        '      "foot": "1 Satz Einordnung für die Fußzeile",\n'
        '      "quelle": "Quellenname und Datum, KEINE Zitationsnummern wie [1]",\n'
        '      "quelle_url": "die ECHTE, vollständige URL der Quelle (https://...) — PFLICHTFELD"\n'
        "    }\n"
        "  ]\n"
        "}"
    )
    result = call_api_json(system, prompt, max_tokens=1200)
    if not result:
        return None
    items_list = result.get("items")
    return items_list if isinstance(items_list, list) else None


_GENERISCHE_PLACEHOLDER_DOMAINS = {
    "example.com", "example.org", "example.net", "example.edu",
    "test.com", "sample.com", "localhost", "127.0.0.1", "yourdomain.com",
    "domain.com", "website.com",
}
_GENERISCHER_PFAD_MUSTER = re.compile(
    r"^(nachrichten|news|aktuell|newsblog|homepage|startseite)[-_a-z0-9]*$",
    re.IGNORECASE,
)


def _url_ist_zu_generisch(url: str) -> bool:
    """Erkennt zwei häufige Gaming-Muster, die die technische
    Erreichbarkeits-Prüfung sonst durchwinkt, weil sie beide auf echte,
    erreichbare Domains zeigen:
      1) Bekannte Platzhalter-Domains (example.com etc.) — real
         registriert und erreichbar, aber niemals eine echte Quelle.
      2) Generische Nachrichten-Landingpages (z.B. '/nachrichten-100.html'
         oder '/nachrichten/nachrichten-mdr-104.html') statt eines
         konkreten Artikels mit eigenem Titel/Slug — das sind
         Übersichtsseiten, keine Belege für einen bestimmten Einzelfall.
    Gefunden nach Live-Meldung: die KI hat wiederholt dieselbe generische
    Landingpage für alle Meldungen gleichzeitig als 'Quelle' angegeben."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return True
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host in _GENERISCHE_PLACEHOLDER_DOMAINS:
        return True
    path = parsed.path.rstrip("/")
    if not path:
        return True  # nur Domain-Root, keine konkrete Seite/Artikel
    last = path.rsplit("/", 1)[-1].lower()
    last_ohne_endung = re.sub(r"\.(html?|php|aspx?)$", "", last)
    return bool(_GENERISCHER_PFAD_MUSTER.match(last_ohne_endung))


def _ist_meta_kommentar(text: str) -> bool:
    """Erkennt, ob ein Text in Wirklichkeit eine Erklärung des Rechercheprozesses
    ist ('ich habe nichts gefunden') statt einer echten Schlusslicht-Meldung.

    WICHTIG (Bugfix, gefunden nach Live-Meldung "leere/kaputte Meldungen"):
    Wenn ein Modell partout keine 6 echten Fälle findet, kann es dazu neigen,
    STATT einen Eintrag einfach weg zu lassen (wie im Prompt verlangt), eine
    Art Entschuldigung direkt ins headline/kommentar-Feld zu schreiben, z.B.
    'ZEIT-Newsindex meldet keine verwertbare Schlusslicht-Meldung mit
    Rangliste' oder 'Keine sechs eigenständigen Meldungen aus den
    Suchergebnissen belegbar'. Das sieht oberflächlich wie ein Satz aus,
    besteht aber die Sinnhaftigkeits-Prüfung, weil grammatikalisch korrekt —
    und wird von einer geteilten, aber technisch echten URL (z.B. eine
    generische Newsindex-Seite) fälschlich als 'verifiziert' durchgewunken.
    Diese Funktion fängt das inhaltlich ab, unabhängig von der URL-Prüfung."""
    if not text:
        return False
    marker = (
        "suchergebnis", "websuche", "newsindex", "keine verwertbare",
        "nicht belegbar", "nicht verifizierbar", "recherche liefert",
        "rechercheergebnis", "keine sechs", "keine drei", "keine fünf",
        "keine vier", "keine zwei", "keine echte meldung", "kein sauberer treffer",
        "die suche hat", "die datenlage ist", "zu dünn", "breiter und tiefer",
        # WICHTIG (Bugfix, gefunden nach Live-Meldung "5 von 6 Rubriken leer"):
        # Diese zusätzlichen Formulierungen wurden live auf schlusslicht.de
        # gefunden, ohne von der bisherigen Liste erkannt zu werden — derselbe
        # Fehlertyp (Modell beschreibt den eigenen Rechercheprozess statt
        # einer echten Meldung), nur mit anderem Wortlaut.
        "aus der suche", "in der suche", "kein sauberer", "kein echtfall",
        "echtfall", "keine belastbare meldung", "keine belastbare quelle",
        "ohne belastbare quelle", "kein belastbarer", "keine geprüfte meldung",
        "aus der recherche", "der recherche liefert", "die recherche liefert",
        "keine eigenständigen",
    )
    t = text.lower()
    return any(m in t for m in marker)


def _item_kontamination_text(item: dict) -> str:
    """Sammelt ALLE Text-Felder eines Eintrags, in denen sich ein
    Prozess-Kommentar verstecken kann — nicht nur headline/kommentar.

    WICHTIG (Bugfix, gefunden nach Live-Meldung "5 von 6 Rubriken zeigen
    Sätze über die eigene Recherche statt echter Meldungen"): Die bisherige
    Prüfung sah NUR headline und kommentar an. Live gefunden wurde aber ein
    Fall, bei dem headline/kommentar (nach einer Umformulierung durch
    review_and_fix_items) zwar Prozess-Kommentare waren, aber selbst wenn
    sie es nicht gewesen wären: table_title, foot und quelle enthielten
    bereits eindeutige Prozess-Kommentare ('Keine Rangliste gefunden',
    'kein belastbarer Schlusslicht-Fall vor', 'DIE ZEIT Newsindex') — diese
    Felder wurden aber NIE geprüft und liefen deshalb unbemerkt durch."""
    teile = [
        item.get("headline", ""), item.get("kommentar", ""),
        item.get("table_title", ""), item.get("foot", ""),
        item.get("quelle", ""),
    ]
    for row in item.get("rows") or []:
        if isinstance(row, dict):
            teile.append(row.get("name", ""))
    return " ".join(t for t in teile if t)


def _item_ist_kontaminiert(item: dict) -> bool:
    """Prüft ALLE Textfelder eines Eintrags auf Prozess-Kommentare (siehe
    _item_kontamination_text/_ist_meta_kommentar). Wird sowohl direkt nach
    dem Fetch als auch als letzte Instanz nach review_and_fix_items
    aufgerufen, damit eine Kontamination unabhängig davon erkannt wird, an
    welcher Stelle im Ablauf sie entstanden ist."""
    return _ist_meta_kommentar(_item_kontamination_text(item))


# WICHTIG (Bugfix, gefunden nach Live-Meldung "Kriminalität statt sozialer/
# linker Themen, Redundanz durch Einzelfall-Meldungen wie 'Oberkassel'"):
# Trotz Prompt-Anweisung wählte das Modell wiederholt einzelne Kriminal-
# oder Unglücksmeldungen (Straftat, Unfall, Flugzeugabsturz) ohne echten
# Vergleichs-/Rankingbezug — nie als 'Schlusslicht' vorgesehen.
#
# ERWEITERUNG (Bugfix, gefunden nach WEITERER Live-Meldung: 'Alltag ·
# Betrug' [Gutschein-Betrug], 'Region · Bayern' [mutmaßlich getötete Kuh],
# 'Lokales · Düsseldorf' [Oberkassel, drittes Mal]): Ein reiner
# Kicker-Marker ('Kriminalität', 'Blaulicht') reicht NICHT — das Modell
# versteckt dieselbe Art Einzelfall-Meldung auch unter unauffälligen,
# generischen Lokal-/Boulevard-Kickern ('Alltag', 'Lokales', 'Region ·
# X', 'Vermischtes'), die für sich genommen kein Warnsignal sind. Zwei
# zusätzliche, robustere Signale:
#  (1) Generische Lokal-/Boulevard-Rubriken-Label — echte strukturkritische
#      oder massentaugliche Themen dieser Seite haben immer ein KONKRETES
#      Themen-Label (z.B. 'EU · Geldpolitik', 'Fußball · Bundesliga',
#      'Pressefreiheit'), nie ein bloßes Regional-/Boulevard-Schlagwort
#      ohne Themenbezug.
#  (2) Konkrete Tatbestands-/Ermittlungssprache in Schlagzeile/Kommentar
#      (Betrug, Tötung, Festnahme, Ermittlungen gegen eine Privatperson),
#      die auf einen Einzelfall statt einen Strukturvergleich hindeutet.
_VERBOTENE_KICKER_MARKER = (
    "kriminal", "blaulicht", "unfall", "vermisst", "unglück", "unglueck",
    "katastrophe", "mordfall", "gewaltverbrechen",
    # generische Lokal-/Boulevard-Label ohne eigenen Themenbezug (Signal 1)
    "alltag", "lokales", "vermischtes", "boulevard", "region ·", "region -",
    "panorama",
    # dieselben Signale auf Englisch (LANG=en-Ausgabe, siehe index.en.html)
    "crime", "accident", "disaster", "missing person", "breaking news",
    "local news", "human interest",
)

_VERBOTENE_TATBESTAND_MARKER = (
    "ergaunert", "erschlichen", "betrug", "betrüger", "diebstahl", "einbruch",
    "überfall", "raubüberfall", "tatverdächtig", "festgenommen", "verhaftet",
    "unbekannter täter", "unbekannte täter", "angeklagt", "prozess gegen",
    "geht von einer straftat aus", "geht von einer gezielten tötung aus",
    "mutmaßlich getötet", "ermittlungen laufen seit", "die polizei ermittelt",
    "polizei geht von", "abgestürzt", "flugzeugabsturz", "brand ausgebrochen",
    # dieselben Signale auf Englisch
    "scammed", "swindled", "conned", "defrauded", "fraudster", "burglary",
    "robbery", "arrested", "charged with", "suspect", "unknown assailant",
    "shot dead", "stabbed to death", "found dead", "crash killed",
    "were killed when", "dead after a fire", "killed in a fire",
    "died after a fire", "died in a fire", "dead in a fire", "dead in fire",
    "plane crash", "car crash",
)


def _ist_einzelfall_kriminalitaet_oder_unglueck(item: dict) -> bool:
    """Erkennt Einzelfall-Kriminal-/Unglücksmeldungen: über das Kicker-/
    Thema-Label (inkl. generischer Lokal-/Boulevard-Label ohne eigenen
    Themenbezug) UND über konkrete Tatbestands-/Ermittlungssprache in
    Schlagzeile/Kommentar (siehe Kommentar oben für Details/Beispiele)."""
    kicker_text = f"{item.get('kicker', '')} {item.get('thema', '')}".lower()
    if any(m in kicker_text for m in _VERBOTENE_KICKER_MARKER):
        return True
    inhalt_text = f"{item.get('headline', '')} {item.get('kommentar', '')}".lower()
    return any(m in inhalt_text for m in _VERBOTENE_TATBESTAND_MARKER)


BAD_URL_HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bad_url_history.json")
BAD_URL_KEEP_DAYS = 180  # deutlich länger als die Story-Historie: einmal als
                         # Feigenblatt erkannte URLs bleiben lange gesperrt


def load_bad_urls() -> list:
    """Liest dauerhaft gesperrte URLs (einmal als Feigenblatt/Duplikat/
    generische Landingpage erkannt). WICHTIG (Bugfix, gefunden nach
    wiederholter Live-Meldung): Dieselbe generische URL (z.B.
    deutschlandfunk.de/nachrichten-100.html) tauchte über mehrere,
    voneinander unabhängige Tage/Läufe IMMER WIEDER als Feigenblatt-Quelle
    auf — reine Prompt-Anweisungen ('verwende sowas nicht') reichten nicht,
    weil das Modell in jedem neuen Aufruf wieder bei null anfängt. Diese
    Liste merkt sich beobachtete Wiederholungstäter dauerhaft und verbietet
    sie explizit und NAMENTLICH im nächsten Prompt."""
    if not os.path.exists(BAD_URL_HISTORY_PATH):
        return []
    try:
        with open(BAD_URL_HISTORY_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_bad_urls(urls: list) -> None:
    cutoff = datetime.date.today() - datetime.timedelta(days=BAD_URL_KEEP_DAYS)
    pruned = []
    for entry in urls:
        try:
            d = datetime.date.fromisoformat(entry.get("date", ""))
        except (ValueError, TypeError, AttributeError):
            continue
        if d >= cutoff:
            pruned.append(entry)
    try:
        with open(BAD_URL_HISTORY_PATH, "w", encoding="utf-8") as fh:
            json.dump(pruned, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        log(f"  Bad-URL-Historie konnte nicht gespeichert werden: {exc}")


DAILY_ITEMS_HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "daily_items_history.json")
DAILY_ITEMS_HISTORY_KEEP_DAYS = 30  # siehe load_daily_items_history/save_daily_items_history

# WICHTIG (Bugfix, gefunden nach Live-Meldung "Redundanz: 'Oberkassel: Mann
# und Frau bei Streit verletzt' UND 'In Oberkassel werden bei Streit zwei
# Menschen schwer verletzt' gleichzeitig auf der Startseite"): avoid_entities
# wurde bisher NUR aus story_history.json gespeist — das erfasst nur Fälle,
# zu denen je eine Hintergrundstory erfolgreich veröffentlicht wurde. Ein
# Tagesmeldungs-Slot, der (wie hier) über mehrere Tage einfach nie neu
# recherchiert wurde, blieb tagelang sichtbar stehen, ohne je in dieser
# Liste aufzutauchen — eine spätere, unabhängige Recherche konnte denselben
# realen Fall daher erneut auswählen und parallel zur alten, noch stehenden
# Meldung zeigen. Diese Historie merkt sich JEDE tatsächlich veröffentlichte
# Tagesmeldung (nicht nur die mit Hintergrundstory) und verhindert damit
# dauerhaft, dass derselbe reale Fall doppelt bzw. erneut auftaucht, solange
# er (oder eine noch nicht aktualisierte alte Fassung davon) noch sichtbar
# sein könnte.


def load_daily_items_history() -> list:
    if not os.path.exists(DAILY_ITEMS_HISTORY_PATH):
        return []
    try:
        with open(DAILY_ITEMS_HISTORY_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_daily_items_history(history: list) -> None:
    cutoff = datetime.date.today() - datetime.timedelta(days=DAILY_ITEMS_HISTORY_KEEP_DAYS)
    pruned = []
    for entry in history:
        try:
            d = datetime.date.fromisoformat(entry.get("date", ""))
        except (ValueError, TypeError, AttributeError):
            continue
        if d >= cutoff:
            pruned.append(entry)
    try:
        with open(DAILY_ITEMS_HISTORY_PATH, "w", encoding="utf-8") as fh:
            json.dump(pruned, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        log(f"  Tagesmeldungs-Historie konnte nicht gespeichert werden: {exc}")


BATCH_SIZE = 3  # siehe get_daily_items: kleinere Aufrufe recherchieren nachweislich zuverlässiger


def get_daily_items(date_label: str, avoid_entities: list, vorhandene_ueberschriften: list = None):
    """Holt die frei recherchierten Tagesmeldungen (kein Themen-Pool, keine
    Rotation — siehe _fetch_fresh_items), aufgeteilt in Gruppen von je
    BATCH_SIZE.

    WICHTIG (Bugfix, gefunden nach Live-Meldung "nur 1 von 6 Rubriken
    aktualisiert"): Ein einzelner Aufruf für alle N_ITEMS Meldungen auf
    einmal führte messbar häufiger dazu, dass das Modell für mehrere
    Plätze dieselbe generische Feigenblatt-Quelle wiederverwendete, statt
    für jeden Fall einzeln zu recherchieren — vermutlich, weil die
    kombinierte Aufgabe (N_ITEMS unterschiedliche, verifizierbare Fälle
    UND Themenmischung UND Vermeidungsliste gleichzeitig) zu komplex für
    einen Durchgang ist. Kleinere Gruppen (3 auf einmal, wie bei Insights/
    Brightside/dem ursprünglichen 3er-Design) lieferten in der Praxis
    durchgehend eine deutlich höhere Trefferquote. Die zweite Gruppe
    bekommt zusätzlich die in der ersten Gruppe bereits gewählten
    Entitäten als Vermeidungsliste, damit sich beide Gruppen nicht
    überschneiden."""
    all_items = {}
    schon_gewaehlte_entitaeten = list(avoid_entities)
    bad_url_history = load_bad_urls()
    verbotene_urls = sorted({
        (entry.get("url") or "").strip()
        for entry in bad_url_history
        if (entry.get("url") or "").strip()
    })
    if verbotene_urls:
        log(f"  {len(verbotene_urls)} dauerhaft gesperrte Feigenblatt-URL(s) "
            f"werden im Prompt explizit verboten.")
    neue_bad_urls = []

    for batch_start in range(0, N_ITEMS, BATCH_SIZE):
        groesse = min(BATCH_SIZE, N_ITEMS - batch_start)

        # WICHTIG (Bugfix, gefunden nach Live-Meldung "5 von 6 Rubriken
        # bleiben leer"): Die alte Logik brach den Wiederholungsversuch
        # bereits ab, sobald AUCH NUR EINE einzige der `groesse` Meldungen
        # verwertbar war ("Erfolg, auch teilweise -> kein zweiter Versuch
        # nötig") — schaffte eine Gruppe von 3 also nur 1 echten Treffer,
        # blieben die übrigen 2 Plätze für den ganzen Tag leer (bzw. beim
        # bestehenden, evtl. bereits kontaminierten Stand), OHNE dass je ein
        # zweiter Versuch für genau diese fehlenden Plätze unternommen
        # wurde. Jetzt: gruppen_treffer sammelt über ALLE Versuche hinweg,
        # jeder weitere Versuch fragt nur noch die tatsächlich NOCH
        # fehlende Anzahl an, und die Gruppe gilt erst als abgeschlossen,
        # wenn entweder alle `groesse` Plätze gefüllt sind oder alle 3
        # Versuche aufgebraucht wurden.
        gruppen_treffer = {}
        letzter_fehlgrund = ""
        # WICHTIG: 4 statt 3 Versuche — seit der strengeren Themenpolitik
        # (keine Einzelfall-Kriminalität/-Unglücke mehr, siehe
        # _ist_einzelfall_kriminalitaet_oder_unglueck) sind vorher leicht
        # zu findende, aber unzulässige Kandidaten häufiger zu ersetzen;
        # ein zusätzlicher Versuch federt das ab.
        for versuch in range(4):
            fehlend = groesse - len(gruppen_treffer)
            if fehlend <= 0:
                break
            log(f"Recherchiere {fehlend} frische Schlusslicht-Meldungen "
                f"(Gruppe {batch_start // BATCH_SIZE + 1}"
                f"{f', {versuch + 1}. Versuch, {fehlend} von {groesse} fehlen noch' if versuch else ''}) …")

            extra_hinweis = ""
            if versuch > 0 and letzter_fehlgrund:
                extra_hinweis = (
                    f"\n\nWICHTIG: Dein letzter Versuch hat nicht genug echte "
                    f"Meldungen geliefert ({letzter_fehlgrund}). Versuche es "
                    f"diesmal grundlegend anders: wähle andere, dir noch nicht "
                    f"eingefallene Themen und recherchiere für jedes einzeln "
                    f"eine ECHTE, unterschiedliche Quelle. Lieber 1 echte "
                    f"Meldung als 3 erfundene oder wiederverwendete."
                )

            # WICHTIG (Bugfix, gefunden nach Live-Meldung "Startseite
            # aktualisiert sich nicht"): Das JSON-Schema fragte bisher ein
            # Objekt mit REIN NUMERISCHEN String-Schlüsseln ab ("1", "2",
            # "3" als Schlüssel). Manche Modellantworten liefern solche
            # Schlüssel ohne Anführungszeichen (wie ein Python-Dict statt
            # echtem JSON, z.B. {1: {...}}) — das ist ungültiges JSON und
            # ließ sich auch durch die Selbstreparatur nicht zuverlässig
            # retten. Jetzt: Array-Schema ("items": [...]), Zuordnung zum
            # Slot rein über die Position in der Liste — exakt dasselbe
            # robuste Muster wie bei den bereits zuverlässig laufenden
            # Seiten (Insights, Brightside-Good-News, Nonconformist).
            items_list = _fetch_fresh_items(
                date_label, schon_gewaehlte_entitaeten, fehlend,
                verbotene_urls + neue_bad_urls, extra_hinweis,
            ) or []

            # WICHTIG (Bugfix, gefunden nach Live-Meldung "Meldungen sind
            # Textbausteine über die eigene Suche"): Wenn 2 oder mehr
            # Meldungen dieselbe Quellen-URL teilen, ist das ein starkes
            # Signal, dass die KI eine generische, technisch echte URL
            # (z.B. eine Newsindex-Startseite) als Feigenblatt
            # wiederverwendet hat, statt für jede Meldung wirklich zu
            # recherchieren. Betroffene Einträge werden komplett verworfen
            # statt einzeln toleriert — UND die URL wird dauerhaft gesperrt
            # (siehe load_bad_urls/save_bad_urls), damit sie in künftigen
            # Läufen erst gar nicht mehr vorgeschlagen werden kann.
            url_counts = {}
            for it in items_list:
                if isinstance(it, dict):
                    url = (it.get("quelle_url") or "").strip().lower()
                    if url:
                        url_counts[url] = url_counts.get(url, 0) + 1
            doppelte_urls = {u for u, c in url_counts.items() if c > 1}
            if doppelte_urls:
                log(f"  WARNUNG: {len(doppelte_urls)} Quellen-URL(s) werden von "
                    f"mehreren Meldungen gleichzeitig verwendet — starkes "
                    f"Anzeichen für eine Feigenblatt-Quelle statt echter "
                    f"Einzelrecherche. Betroffene Meldungen werden verworfen "
                    f"und dauerhaft gesperrt: {', '.join(doppelte_urls)}")
                for u in doppelte_urls:
                    if u not in neue_bad_urls:
                        neue_bad_urls.append(u)

            neue_treffer = {}
            einzelfall_verworfen = [False]
            # Freie Slot-Nummern dieser Gruppe, die noch keinen Treffer haben —
            # neue Ergebnisse werden der Reihe nach genau diesen zugewiesen.
            offene_keys = [
                str(batch_start + idx + 1) for idx in range(groesse)
                if str(batch_start + idx + 1) not in gruppen_treffer
            ]
            for idx, key in enumerate(offene_keys):
                item = items_list[idx] if idx < len(items_list) else None
                if not isinstance(item, dict):
                    log(f"  Meldung {key}: keine verwertbare Antwort erhalten — übersprungen.")
                    continue
                headline = (item.get("headline") or "").strip()
                kommentar = (item.get("kommentar") or "").strip()
                # WICHTIG (Atomaritäts-Fix, siehe main-Historie): headline
                # UND kommentar müssen BEIDE vorhanden sein, sonst wird der
                # Eintrag komplett verworfen. Ein Teil-Update würde sonst
                # zwei Textteile aus evtl. ganz unterschiedlichen Tagen/
                # Themen kombinieren.
                if not (headline and kommentar):
                    fehlt = "kommentar" if headline else ("headline" if kommentar else "headline+kommentar")
                    log(f"  Meldung {key}: unvollständiger Eintrag ({fehlt} fehlt) "
                        f"— komplett übersprungen, bestehender (in sich konsistenter) "
                        f"Stand bleibt. Kein Teil-Update einzelner Felder.")
                    continue
                if _item_ist_kontaminiert(item):
                    log(f"  Meldung {key}: Text (Schlagzeile, Kommentar, Tabelle, "
                        f"Quelle oder Rangliste) beschreibt den eigenen "
                        f"Rechercheprozess statt einer echten Meldung zu sein "
                        f"({headline!r}) — verworfen, keine Platzhalter-Texte als Inhalt.")
                    continue
                if _ist_einzelfall_kriminalitaet_oder_unglueck(item):
                    log(f"  Meldung {key}: Einzelfall-Kriminal-/Unglücksmeldung ohne "
                        f"Vergleichs-/Rankingbezug ({item.get('kicker', '')!r}, "
                        f"{headline!r}) — verworfen, war für schlusslicht.de nie "
                        f"vorgesehen.")
                    einzelfall_verworfen[0] = True
                    continue
                url = (item.get("quelle_url") or "").strip().lower()
                if url and (url in doppelte_urls or url in verbotene_urls or url in neue_bad_urls):
                    log(f"  Meldung {key}: teilt sich eine Quellen-URL mit anderen "
                        f"Meldungen oder ist bereits dauerhaft gesperrt ({url}) — verworfen.")
                    continue
                if url and _url_ist_zu_generisch(url):
                    log(f"  Meldung {key}: Quellen-URL ist eine generische Landingpage "
                        f"oder Platzhalter-Domain statt eines konkreten Artikels "
                        f"({url}) — verworfen.")
                    continue
                neue_treffer[key] = item

            if neue_treffer:
                gruppen_treffer.update(neue_treffer)
                for item in neue_treffer.values():
                    entity = (item.get("entity") or "").strip()
                    if entity:
                        schon_gewaehlte_entitaeten.append(entity)

            if len(gruppen_treffer) >= groesse:
                break  # Gruppe komplett gefüllt -> kein weiterer Versuch nötig

            if doppelte_urls:
                letzter_fehlgrund = f"alle Meldungen teilten sich eine Quelle ({', '.join(doppelte_urls)})"
            elif einzelfall_verworfen[0]:
                letzter_fehlgrund = (
                    "mindestens eine Meldung war eine einzelne Kriminal-/"
                    "Unglücksmeldung ohne Vergleichs-/Rankingbezug — das ist "
                    "für schlusslicht.de NICHT erlaubt, egal wie echt die "
                    "Quelle ist"
                )
            else:
                letzter_fehlgrund = f"nur {len(neue_treffer)} von {fehlend} angeforderten Meldungen war(en) verwertbar"
            if versuch < 3:
                log(f"  Gruppe noch nicht vollständig ({len(gruppen_treffer)}/{groesse}, "
                    f"{letzter_fehlgrund}) — wiederhole für die fehlenden "
                    f"{groesse - len(gruppen_treffer)} Plätze (Versuch {versuch + 2}/4).")

        # WICHTIG (Bugfix, gefunden nach Live-Meldung "viele Kategorien
        # bleiben beim ersten Lauf leer"): Die freie Themenwahl muss
        # gleichzeitig mehrere harte Kriterien erfüllen (echter Vergleichs-/
        # Rankingbezug, keine Kriminalität, kein Duplikat, verifizierbare
        # Quelle) — auch mit 4 Versuchen bleibt ein Platz manchmal leer.
        # Bevor endgültig aufgegeben wird, versucht diese Stufe für JEDEN
        # noch fehlenden Platz den deutlich engeren, fast immer erfüllbaren
        # Fallback (siehe _fetch_fallback_sport_item): eine Sport-Liga-
        # Tabellenletzten-Meldung. Läuft durch dieselbe Validierung wie ein
        # regulärer Treffer (keine Ausnahme von den Qualitätsprüfungen).
        fehlende_keys = [
            str(batch_start + idx + 1) for idx in range(groesse)
            if str(batch_start + idx + 1) not in gruppen_treffer
        ]
        fallback_verwendete_urls = set()
        for key in fehlende_keys:
            for fallback_versuch in range(2):
                fallback_items = _fetch_fallback_sport_item(
                    date_label, schon_gewaehlte_entitaeten,
                    verbotene_urls + neue_bad_urls + list(fallback_verwendete_urls),
                ) or []
                if not fallback_items or not isinstance(fallback_items[0], dict):
                    log(f"  Meldung {key}: Fallback (Sport-Tabellenletzter) lieferte "
                        f"keine verwertbare Antwort (Versuch {fallback_versuch + 1}/2).")
                    continue
                item = fallback_items[0]
                headline = (item.get("headline") or "").strip()
                kommentar = (item.get("kommentar") or "").strip()
                if not (headline and kommentar):
                    log(f"  Meldung {key}: Fallback-Eintrag unvollständig — übersprungen.")
                    continue
                if _item_ist_kontaminiert(item):
                    log(f"  Meldung {key}: Fallback-Eintrag ist ein Prozess-Kommentar "
                        f"— verworfen.")
                    continue
                if _ist_einzelfall_kriminalitaet_oder_unglueck(item):
                    log(f"  Meldung {key}: Fallback-Eintrag verstößt gegen die "
                        f"Themenpolitik — verworfen.")
                    continue
                url = (item.get("quelle_url") or "").strip().lower()
                if url and (url in verbotene_urls or url in neue_bad_urls
                            or url in fallback_verwendete_urls):
                    log(f"  Meldung {key}: Fallback-Quelle ist gesperrt oder bereits "
                        f"in dieser Ausgabe verwendet — verworfen.")
                    continue
                if url and _url_ist_zu_generisch(url):
                    log(f"  Meldung {key}: Fallback-Quelle ist eine generische "
                        f"Landingpage — verworfen.")
                    continue
                gruppen_treffer[key] = item
                entity = (item.get("entity") or "").strip()
                if entity:
                    schon_gewaehlte_entitaeten.append(entity)
                if url:
                    fallback_verwendete_urls.add(url)
                log(f"  Meldung {key}: über Fallback (Sport-Tabellenletzter) gefüllt.")
                break

        all_items.update(gruppen_treffer)

    if neue_bad_urls:
        save_bad_urls(bad_url_history + [
            {"date": datetime.date.today().isoformat(), "url": u} for u in neue_bad_urls
        ])
        log(f"  {len(neue_bad_urls)} neue Feigenblatt-URL(s) dauerhaft gesperrt: "
            f"{', '.join(neue_bad_urls)}")

    all_items = dedupe_rubrik_topics(all_items, vorhandene_ueberschriften)
    all_items = strip_repeated_boilerplate(all_items)
    all_items = review_and_fix_items(all_items, date_label)

    neue_daily_history_eintraege = [
        {
            "date": datetime.date.today().isoformat(),
            "entity": (it.get("entity") or "").strip(),
            "thema": (it.get("thema") or "").strip(),
            "headline": (it.get("headline") or "").strip(),
        }
        for it in all_items.values()
        if it and (it.get("entity") or "").strip()
    ]
    if neue_daily_history_eintraege:
        save_daily_items_history(load_daily_items_history() + neue_daily_history_eintraege)

    spotlight_ticker = get_spotlight_and_ticker(date_label, all_items)

    if all_items:
        log(f"  {len(all_items)} Meldungen final erhalten.")
    else:
        log("  Keine verwertbaren Meldungsdaten erhalten.")

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
    data = call_api_json(system, prompt, max_tokens=1500) or {}
    return {"spotlight": data.get("spotlight"), "ticker": data.get("ticker")}


def review_and_fix_items(items: dict, date_label: str) -> dict:
    """Letzter Schritt vor der Veröffentlichung: Prüft Sinnhaftigkeit,
    verifiziert technisch jede Quellen-URL — UND formuliert bei Bedarf
    um (Grammatik, Klarheit, Redundanz, Wiederholungen), OHNE dabei neue
    Fakten/Zahlen/Namen zu erfinden. Ein Eintrag, der nur schlecht
    formuliert ist (aber inhaltlich stimmt), wird also nicht mehr
    automatisch verworfen, sondern repariert — nur ein inhaltlich
    kaputter oder fehlzugeordneter Eintrag wird weiterhin verworfen."""
    echte_items = {num: it for num, it in items.items() if it}
    if not echte_items:
        return items

    # Schritt 1: Sinnhaftigkeits-Prüfung MIT Umformulierungs-Option.
    log("  Prüfe alle Rubrik-Texte auf Sinnhaftigkeit vor Veröffentlichung …")
    system = (
        "Du bist Chef vom Dienst bei schlusslicht.de und prüfst Texte vor "
        "der Veröffentlichung. Du erfindest NIEMALS neue Fakten, Zahlen, "
        "Namen oder Ereignisse — du darfst aber vorhandene, korrekte "
        "Inhalte SPRACHLICH verbessern (Grammatik, Klarheit, holprige "
        "Formulierungen, Redundanz, Wiederholung von Standardsätzen), "
        "wenn das inhaltlich exakt dasselbe aussagt wie vorher. Antworte "
        "AUSSCHLIESSLICH auf " + ("Englisch (US)" if LANG == "en" else "Deutsch") + ". Antworte NUR "
        "mit validem JSON, keine Erklärung."
    )
    prompt = (
        "Prüfe jeden der folgenden Einträge auf Sinnhaftigkeit: Ist die "
        "Schlagzeile eine konkrete, in sich sinnvolle Aussage (keine "
        "generische Platzhalterformulierung wie 'X: 2026-Bericht', kein "
        "abgeschnittener oder zusammenhangloser Satz, keine holprige "
        "Grammatik)? Passt der Kommentar inhaltlich zur Schlagzeile? Ist "
        "es KEINE Wiederholung eines Standardsatzes aus einem anderen "
        "Eintrag?\n\n"
        "BESONDERS WICHTIG — PROZESS-KOMMENTARE ERKENNEN: Manche Einträge "
        "beschreiben in Wirklichkeit den Rechercheprozess selbst statt "
        "einer echten Meldung — z.B. 'Keine verwertbare Meldung gefunden', "
        "'Die Websuche liefert dafür heute zu wenig', 'Keine sechs "
        "eigenständigen Meldungen aus den Suchergebnissen belegbar'. Das "
        "klingt grammatikalisch oft einwandfrei, ist aber KEINE echte "
        "Nachricht über ein reales Ereignis, sondern eine verkappte "
        "Fehlermeldung. Erkennst du so ein Muster (der Text handelt vom "
        "Suchen/Finden/Belegen selbst statt von einem konkreten Fall mit "
        "echten Namen), gib zwingend 'ok': false zurück — NIEMALS "
        "versuchen, so einen Eintrag nur sprachlich zu 'verbessern'.\n\n"
        "ZUSÄTZLICH — KATEGORIE-KOHÄRENZ (sehr wichtig, häufigster Fehler): "
        "Jeder Eintrag hat ein Feld 'rubrik_soll' — die Kategorie, der er "
        "zugeordnet ist. Prüfe, ob Schlagzeile UND Kommentar TATSÄCHLICH "
        "inhaltlich zu dieser Kategorie gehören. Beispiel für einen Fehler, "
        "den du erkennen musst: rubrik_soll='Klimaschutz', aber die "
        "Schlagzeile handelt tatsächlich von einem Korruptionsfall — das "
        "ist eine KATEGORIE-FEHLZUORDNUNG und muss mit ok:false markiert "
        "werden — das ist ein inhaltlicher Fehler, keine Formulierungsfrage, "
        "und kann NICHT durch Umformulieren behoben werden.\n\n"
        "WENN DER EINTRAG INHALTLICH KORREKT, ABER SCHLECHT FORMULIERT IST "
        "(holprig, unklar, unnötig wiederholend, generisch klingend): gib "
        "'ok': true UND zusätzlich 'headline_neu'/'kommentar_neu' mit einer "
        "verbesserten Fassung zurück — DIESELBEN Fakten, Zahlen, Namen und "
        "Ereignisse, nur klarer/besser formuliert. Erfinde dabei NICHTS "
        "Neues hinzu und lasse keine Fakten weg. Wenn der Eintrag bereits "
        "gut formuliert ist, lass 'headline_neu'/'kommentar_neu' einfach weg.\n\n"
        "WENN DER EINTRAG INHALTLICH KAPUTT IST (Kategorie-Fehlzuordnung, "
        "Platzhalter, Widerspruch zwischen Schlagzeile und Kommentar, "
        "unrettbar unsinnig): gib 'ok': false mit kurzer 'grund'-Angabe "
        "zurück — das kann NICHT durch Umformulieren behoben werden.\n\n"
        "ABSOLUT VERBOTEN — GILT AUCH FÜR DEINE EIGENE ANTWORT: Schreibe "
        "NIEMALS selbst einen Satz über deinen eigenen Prüf- oder "
        "Rechercheprozess in 'headline_neu' oder 'kommentar_neu' (z.B. "
        "'keine verwertbare Meldung gefunden', 'aus der Suche', 'die "
        "Datenlage ist zu dünn', 'kein sauberer Treffer'). Kannst du einen "
        "Eintrag nicht mit Sicherheit bestätigen oder wirkt er dir "
        "unbelegt, ist die EINZIG zulässige Reaktion 'ok': false mit "
        "'grund' — NIEMALS ein Ersatztext, der den Ausfall selbst beschreibt.\n\n"
        f"Einträge:\n{json.dumps({f'slot{num}': {**it, 'rubrik_soll': it.get('kicker', '')} for num, it in echte_items.items()}, ensure_ascii=False, indent=2)}\n\n"
        "Antworte als JSON, mit genau denselben Schlüsseln wie oben (z.B. 'slot1'):\n"
        '{"slot1": {"ok": true}, '
        '"slot2": {"ok": true, "headline_neu": "verbesserte Schlagzeile", "kommentar_neu": "verbesserter Kommentar"}, '
        '"slot3": {"ok": false, "grund": "Kategorie-Fehlzuordnung: Text handelt von X statt Y"}, ...}'
    )
    urteil = call_api_json(system, prompt, max_tokens=3000) or {}

    def _zahlen(text: str) -> set:
        return set(re.findall(r"\d+[.,]?\d*", text or ""))

    for num in list(echte_items.keys()):
        bewertung = urteil.get(f"slot{num}", {})
        if bewertung.get("ok") is False:
            log(f"  Rubrik {num}: Sinnhaftigkeits-Prüfung fehlgeschlagen "
                f"({bewertung.get('grund', 'kein Grund angegeben')}) — verworfen.")
            items[num] = {}
            continue

        # Umformulierung anwenden, aber NUR wenn dabei keine neuen Zahlen
        # auftauchen, die im Original nicht vorhanden waren (Schutz gegen
        # Fakten-Drift während der sprachlichen Überarbeitung) UND die neue
        # Formulierung selbst KEIN Prozess-Kommentar ist.
        #
        # WICHTIG (Bugfix, gefunden nach Live-Meldung "5 von 6 Rubriken
        # zeigen Sätze über die eigene Recherche"): Der eigentliche Fehler
        # saß genau hier — der Reviewer darf headline/kommentar sprachlich
        # überarbeiten, aber diese Überarbeitung wurde NIE gegen
        # _ist_meta_kommentar geprüft. Konnte der Reviewer einen Fall nicht
        # bestätigen, hat er (statt korrekt 'ok': false zu setzen) manchmal
        # selbst einen Satz über den fehlgeschlagenen Rechercheprozess als
        # 'headline_neu'/'kommentar_neu' geliefert — und dieser lief am
        # Filter komplett vorbei, weil der nur auf die ORIGINAL-Fetch-Daten
        # angewendet wurde, nie auf das Ergebnis der Umformulierung selbst.
        original = items[num]
        for feld, feld_neu in (("headline", "headline_neu"), ("kommentar", "kommentar_neu")):
            neu = (bewertung.get(feld_neu) or "").strip()
            if not neu:
                continue
            if _ist_meta_kommentar(neu):
                log(f"  Rubrik {num}: Umformulierung von '{feld}' ist selbst ein "
                    f"Prozess-Kommentar ({neu!r}) — Umformulierung verworfen, "
                    f"Original bleibt unverändert.")
                continue
            alte_zahlen = _zahlen(original.get(feld, ""))
            neue_zahlen = _zahlen(neu)
            if neue_zahlen - alte_zahlen:
                log(f"  Rubrik {num}: Umformulierung von '{feld}' enthält neue, "
                    f"nicht im Original vorhandene Zahlen — Umformulierung "
                    f"verworfen, Original bleibt.")
                continue
            log(f"  Rubrik {num}: '{feld}' sprachlich überarbeitet "
                f"({original.get(feld, '')!r} -> {neu!r}).")
            items[num][feld] = neu

    # Schritt 1b: Letzte, umfassende Kontaminations-Prüfung über ALLE
    # Textfelder (siehe _item_ist_kontaminiert) — unabhängig davon, ob die
    # Kontamination aus dem ursprünglichen Fetch oder erst aus der
    # Umformulierung oben stammt. Das ist die letzte Instanz vor der
    # technischen URL-Prüfung und fängt jede Kontamination ab, egal an
    # welcher Stelle im Ablauf sie entstanden ist.
    for num, item in list(items.items()):
        if item and _item_ist_kontaminiert(item):
            log(f"  Rubrik {num}: finale Kontaminations-Prüfung schlägt an "
                f"({item.get('headline', '')!r}) — Meldung verworfen.")
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


def dedupe_rubrik_topics(items: dict, vorhandene_ueberschriften: list = None) -> dict:
    """Erkennt, wenn zwei verschiedene Rubriken heute dasselbe Themen-
    Schlagwort tragen (z. B. zweimal 'Fußball' in fachfremden Rubriken —
    auch wenn es um unterschiedliche Vereine/Ligen geht), und verwirft die
    später einsortierte Duplikat-Meldung. Diese Rubrik behält dann ihren
    bestehenden Stand aus der Vorlage statt einer doppelten Meldung.
    Prüft zusätzlich IMMER (nicht nur als Rückfall bei fehlendem 'thema')
    per Wortüberlappung gegen bereits behaltene Texte innerhalb der
    heutigen Ausgabe.

    WICHTIG (Bugfix, gefunden nach Live-Meldung "Oberkassel"-Redundanz:
    'Oberkassel: Mann und Frau bei Streit verletzt' UND 'In Oberkassel
    werden bei Streit zwei Menschen schwer verletzt' gleichzeitig sichtbar):
    Der Wortüberlappungs-Vergleich lief bisher NUR, wenn 'thema' komplett
    fehlte — lieferte das Modell (wie fast immer) ein thema-Feld, das sich
    zufällig nicht exakt mit einem anderen deckte, wurde die inhaltliche
    Überlappung nie geprüft. Jetzt läuft der Wortüberlappungs-Vergleich
    IMMER zusätzlich zum thema-Abgleich.

    Über `vorhandene_ueberschriften` können zudem bereits sichtbare, heute
    NICHT aktualisierte Bestandsmeldungen hier mit hineingegeben werden,
    damit eine frische Meldung nicht denselben realen Fall wie eine noch
    stehende alte Karte doppelt zeigt. WICHTIG dabei: Dieser Vergleich läuft
    NUR über die reine Schlagzeile, nicht über Schlagzeile+Kommentar —
    gemessen am echten Live-Fall überlappten die vollen Kommentar-Texte
    (unterschiedlich formulierte Empörung/Einordnung zum selben Ereignis)
    nur zu ~33%, obwohl es sich nachweislich um denselben Fall handelte;
    die Schlagzeilen allein (kurz, faktenbasiert: Ort, Beteiligte, Tat)
    überlappten dagegen zu 60% — ein deutlich zuverlässigeres Signal für
    'derselbe reale Fall', da Kommentartext viel mehr variable, stilistische
    Füllwörter beisteuert, die die Wortüberlappungs-Quote verwässern."""
    vorhandene_ueberschriften = [h.strip() for h in (vorhandene_ueberschriften or []) if (h or "").strip()]
    seen_themen = {}
    kept_texts = {}

    for num in sorted(items.keys()):
        item = items.get(num) or {}
        headline = (item.get("headline") or "").strip()
        combined = f"{headline} {item.get('kommentar', '')}".strip()
        if not combined:
            continue
        thema = (item.get("thema") or "").strip().lower()
        thema_norm = re.sub(r"[^a-zäöüß ]", "", thema)

        is_dup = bool(thema_norm and thema_norm in seen_themen)
        if not is_dup:
            is_dup = any(_paragraphs_content_overlap(combined, prev, 0.5) for prev in kept_texts.values())
        grund = "anderen, heute bereits vergebenen Rubrik"
        if not is_dup and headline:
            is_dup = any(_paragraphs_content_overlap(headline, alt_hl, 0.5) for alt_hl in vorhandene_ueberschriften)
            if is_dup:
                grund = "bereits (unaktualisiert) angezeigten Bestandskarte"

        if is_dup:
            quelle_info = f"Thema '{thema}'" if thema_norm else combined[:70]
            log(f"  Rubrik {num}: {quelle_info} überschneidet sich mit einer "
                f"{grund} — Meldung verworfen, bestehender Stand bleibt.")
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


def get_embedded_stories(date_label: str, items: dict):
    """Schreibt für JEDE der 3 heutigen Meldungen eine EINGEBETTETE
    Hintergrundstory zum selben Fall (statt 3 unabhängig recherchierter
    Storys zu beliebigen anderen Themen). Nutzt dieselbe Wiederholungssperre
    (story_history.json) wie zuvor, jetzt aber verknüpft mit derselben
    Entität, die auch die Tageskarte zeigt."""
    anchors = {num: it for num, it in (items or {}).items() if it and it.get("headline")}
    if not anchors:
        log("  Keine Meldungen vorhanden — keine Hintergrundstorys möglich.")
        return None

    log(f"Recherchiere {len(anchors)} eingebettete Hintergrundstorys (je Meldung) …")

    history = load_story_history()
    verbotene_themen = sorted({
        (entry.get("entity") or "").strip()
        for entry in history
        if (entry.get("entity") or "").strip()
    })
    if verbotene_themen:
        log(f"  {len(verbotene_themen)} Fälle aus den letzten "
            f"{STORY_HISTORY_KEEP_DAYS} Tagen bereits behandelt — werden ausgeschlossen.")

    system = (
        f"Du bist Hintergrundredakteur von schlusslicht.de. Heute ist {date_label}.\n\n"
        "Schreibe für JEDEN der folgenden Fälle eine tiefe Hintergrundstory ZUM "
        "SELBEN THEMA (nicht zu einem anderen Bereich). Nutze die Websuche, um "
        "über die kurze Tagesmeldung hinaus mehr zu recherchieren. Stil: "
        "investigativ, aber menschlich — zeige erkennbares Mitgefühl mit den "
        "Betroffenen und eine klar erkennbare linke, ökologisch-grüne und "
        "gesellschaftskritische Haltung zum Systemversagen dahinter (wer "
        "profitiert davon, wer trägt die Verantwortung), aber NICHT radikal. "
        "Keine kalte Distanz, aber auch keine Larmoyanz oder Übertreibung — "
        "jede Emotion und jede Wertung muss sich aus den geschilderten "
        "Fakten ergeben, nicht aus Adjektiven allein. Zeige das "
        "Systemversagen hinter dem Einzelfall. 400-700 Wörter je Story. "
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
        "Story MUSS auf dem echten, unten angegebenen Fall beruhen, UND du "
        "musst die tatsächliche, funktionierende URL einer Quelle angeben. "
        "Findest du keine echte, existierende URL, liefere für diesen Fall "
        "GAR KEINE Story (lass den Schlüssel im JSON weg), statt etwas zu "
        "erfinden."
        + (
            "\n\nABSOLUTE WIEDERHOLUNGSSPERRE — HÖCHSTE PRIORITÄT: Diese Fälle "
            "wurden in den letzten " + str(STORY_HISTORY_KEEP_DAYS) + " Tagen "
            "auf schlusslicht.de bereits als Hintergrundstory veröffentlicht — "
            "keiner der unten genannten Fälle darf mit einem dieser Fälle "
            "identisch sein: " + ", ".join(verbotene_themen) + "."
            if verbotene_themen else ""
        )
    )

    zeilen = "\n".join(
        f'- Entität: "{it.get("entity", "")}" · Schlagzeile: "{it.get("headline", "")}" '
        f'· Kommentar: "{it.get("kommentar", "")}" · Quelle: {it.get("quelle", "")}'
        for it in anchors.values()
    )
    prompt = (
        f"Vertiefe JEDEN der folgenden {len(anchors)} Tagesfälle zu einer "
        f"eigenen Hintergrundstory ZUM SELBEN THEMA (nicht zu einem anderen "
        f"Bereich), Ausgabe {date_label}. Nutze die Websuche, um über die "
        f"kurze Meldung hinaus mehr Kontext, Zahlen und Einordnung zu finden.\n\n"
        f"Heutige Fälle:\n{zeilen}\n\n"
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
        '      "for_entity": "MUSS exakt die Entität von oben sein, zu der diese Story gehört",\n'
        '      "cat": "// Kategorie · Zeitraum",\n'
        '      "title": "packender Titel, max 80 Zeichen",\n'
        '      "teaser": "Einleitung, 2-3 Sätze",\n'
        '      "body": ["<p>Absatz 1: nur das Ereignis</p>", "<p>Absatz 2: nur Hintergrund/Ursache</p>", "<p>Absatz 3: nur konkrete Auswirkung</p>", "<p>Absatz 4: nur die Einordnung als Systemversagen</p>"],\n'
        '      "factbox": ["Fakt 1", "Fakt 2", "Fakt 3"],\n'
        '      "conclusion": "Schlusssatz zum Systemversagen",\n'
        '      "source": "Quellenname und Datum, z.B. Spiegel 22.06.2026 — KEINE Zitationsnummern wie [1]",\n'
        '      "source_url": "die ECHTE, vollständige URL der Quelle (https://...) — PFLICHTFELD"\n'
        "    }\n"
        f"    // genau {len(anchors)} Einträge in dieser Liste, eine je Fall\n"
        "  ]\n"
        "}"
    )

    # WICHTIG (Bugfix, siehe get_daily_items für die volle Begründung):
    # Array-Schema statt eines Objekts mit rein numerischen Schlüsseln —
    # Zuordnung zur richtigen Meldung erfolgt über das Feld "for_entity",
    # nicht über eine fragile Positions- oder Schlüssel-Zuordnung.
    data = call_api_json(system, prompt, max_tokens=8000) or {}
    story_list = data.get("stories") if isinstance(data.get("stories"), list) else []
    stories_by_entity = {}
    for st in story_list:
        if isinstance(st, dict) and st.get("for_entity"):
            stories_by_entity[st["for_entity"].strip().lower()] = st

    stories = {}
    today_iso = datetime.date.today().isoformat()
    new_history_entries = []
    for num, anchor in anchors.items():
        entity = (anchor.get("entity") or "").strip()
        story = stories_by_entity.get(entity.lower())
        if not isinstance(story, dict):
            log(f"  Meldung {num} ({entity!r}): keine verwertbare Story-Antwort "
                f"erhalten — übersprungen.")
            continue
        title = (story.get("title") or "").strip()
        if is_recently_used(entity, title, history):
            log(f"  Meldung {num} ({title or entity!r}): bereits in den letzten "
                f"{STORY_HISTORY_KEEP_DAYS} Tagen veröffentlicht — "
                f"WIEDERHOLUNG verworfen (Wiederholungssperre).")
            continue
        url = (story.get("source_url") or "").strip()
        if not verify_url(url):
            log(f"  Meldung {num}: Story-Quellen-URL fehlt oder nicht erreichbar "
                f"({url or 'keine URL angegeben'}) — Story verworfen, keine "
                f"Halluzinationen ohne Beleg.")
            continue
        log(f"  Meldung {num}: Hintergrundstory-Quelle verifiziert ({url})")
        story["body"] = dedupe_paragraphs(story.get("body"))
        stories[num] = story
        new_history_entries.append({"date": today_iso, "entity": entity, "title": title})

    if new_history_entries:
        save_story_history(history + new_history_entries)
        log(f"  Story-Historie aktualisiert (+{len(new_history_entries)} Einträge, "
            f"Wiederholungssperre für {STORY_HISTORY_KEEP_DAYS} Tage).")

    log(f"  {len(stories)} von {len(anchors)} eingebetteten Hintergrundstorys verifiziert.")
    return stories or None


# ── Einbau ins HTML ──────────────────────────────────────────────────────────
def set_text(node, value):
    """Setzt reinen Text in ein BeautifulSoup-Element."""
    if node is not None and value:
        node.clear()
        node.append(str(value))


def inject(html: str, items, stories, date_label: str, build_time: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

def _build_table_html(item: dict) -> str:
    """Baut das .tbl-HTML-Fragment aus den frisch recherchierten Zeilen der
    KI (immer 3-spaltig: Rang/Name/Wert — einheitliches Format, damit die KI
    nicht auch noch unterschiedliche Spaltenzahlen gestalten muss)."""
    title = (item.get("table_title") or "").strip()
    tag = (item.get("table_tag") or "").strip()
    foot = (item.get("foot") or "").strip()
    rows = item.get("rows") or []

    rows_html = []
    n = min(len(rows), 3)
    for i, row in enumerate(rows[:3]):
        is_last = " is-last" if i == n - 1 else ""
        lamp = '<span class="lamp"></span>' if i == n - 1 else ""
        rank = (row.get("rank") or "—")
        name = (row.get("name") or "").strip()
        value = (row.get("value") or "").strip()
        rows_html.append(
            f'<div class="row c3{is_last}"><span class="rk">{rank}</span>'
            f'<span class="nm">{lamp}{name}</span><span class="v">{value}</span></div>'
        )

    col2_label = "Value" if LANG == "en" else "Wert"
    return (
        f'<div class="tbl-head"><span class="tt">{title}</span><span class="tag">{tag}</span></div>'
        f'<div class="cols c3"><span>#</span><span class="l">Name</span><span>{col2_label}</span></div>'
        + "".join(rows_html)
        + f'<div class="tbl-foot">{foot}</div>'
    )


def inject(html: str, items, stories, date_label: str, build_time: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    # ── Die 3 festen Slots im Template bekommen ihren kompletten Inhalt
    #    direkt aus der heutigen, frisch recherchierten KI-Meldung (kein
    #    Pool, keine Rotation mehr — siehe get_daily_items). Fehlt eine
    #    Meldung, bleibt der Slot beim zuletzt veröffentlichten Stand.
    if items and items.get("items"):
        for slot_i in range(1, N_ITEMS + 1):
            key = str(slot_i)
            it = items["items"].get(key)
            card = soup.select_one(f'article.rub[data-slot="{slot_i}"]')
            if card is None or not it:
                continue

            kommentar = (it.get("kommentar") or "").strip()
            headline = (it.get("headline") or "").strip()
            # ATOMARITÄTS-ABSICHERUNG (Defense-in-Depth, zusätzlich zur
            # Prüfung in get_daily_items): headline UND kommentar werden
            # NUR gemeinsam aktualisiert, nie einzeln.
            if not (kommentar and headline and len(headline) > 4):
                log(f"  Slot {slot_i}: unvollständiges Item bei Injektion "
                    f"(headline oder kommentar fehlt) — übersprungen, "
                    f"Karte bleibt vollständig unverändert.")
                continue

            card["data-rubrik"] = key
            rub_no = card.select_one(".rub-no")
            if rub_no is not None:
                rub_no.string = str(slot_i)
            set_text(card.select_one(".rub-ico"), it.get("icon"))
            set_text(card.select_one(".rnum"), it.get("kicker"))
            set_text(card.select_one(".rtit"), headline)
            set_text(card.select_one(".realsatire"), f"„{kommentar}“")
            quelle = (it.get("quelle") or "").strip()
            stand = card.select_one(".rub-stand")
            if stand is not None and quelle:
                set_text(stand, (f"As of: {date_label} · {quelle}" if LANG == "en"
                                  else f"Stand: {date_label} · {quelle}"))
            tag = card.select_one(".ai-tag")
            if tag is not None:
                set_text(tag, f"✦ Tagesaktuell · {quelle}" if quelle else "✦ Tagesaktuell")
            tbl = card.select_one(".tbl")
            if tbl is not None and it.get("rows"):
                tbl.clear()
                tbl.append(BeautifulSoup(_build_table_html(it), "html.parser"))
            story_btn = card.select_one(".story-more")
            if story_btn is not None:
                story_btn["onclick"] = f"openModal('story-slot{slot_i}')"

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

    # ── Eingebettete Hintergrundstorys (je Slot ihre eigene) ────────────────
    if stories:
        for key, st in stories.items():
            modal = soup.select_one(f"#story-slot{key}")
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
        og_desc = txt[:155] if txt else "Das Magazin der Letzten. 6 Rubriken täglich aktuell."

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
def carry_over_dynamic_content(template_html: str, output_html: str) -> str:
    """WICHTIG (Fix für einen bislang unsichtbaren Dauerfehler): Vorher wurde
    bei bestehendem OUTPUT das GESAMTE alte HTML als Basis verwendet und
    index.template.html damit für IMMER nicht mehr gelesen, sobald die
    Struktur einmal passte. Jede künftige Änderung an irgendeinem statischen
    Bereich (Nav-Link, Footer, CSS, Hero-Text, neue Abschnitte) im Template
    kam dadurch NIE auf der echten Seite an, egal wie oft der Workflow lief
    — genau das führte dazu, dass ein bereits im Template korrigierter toter
    Link (#stories) live weiter kaputt blieb.

    Diese Funktion dreht das Prinzip um: das TEMPLATE ist immer die Basis
    (jede statische Änderung wirkt damit garantiert sofort) — nur die
    wenigen eindeutig KI-generierten, dynamischen Felder werden aus dem
    gestrigen OUTPUT in die frische Template-Kopie übertragen, damit ein
    heutiger Generierungs-Fehlschlag nicht auf Tag-0-Platzhalter zurückfällt."""
    try:
        neu = BeautifulSoup(template_html, "html.parser")
        alt = BeautifulSoup(output_html, "html.parser")
    except Exception as exc:
        log(f"  WARNUNG: Übernahme der Altdaten übersprungen (Parse-Fehler: {exc}).")
        return template_html

    def _copy_text(sel):
        src = alt.select_one(sel)
        dst = neu.select_one(sel)
        if src is not None and dst is not None:
            dst.string = src.get_text()

    def _copy_html(sel):
        src = alt.select_one(sel)
        dst = neu.select_one(sel)
        if src is not None and dst is not None:
            dst.clear()
            for child in list(src.children):
                dst.append(child.extract() if hasattr(child, "extract") else str(child))

    def _copy_attr(sel, attr):
        src = alt.select_one(sel)
        dst = neu.select_one(sel)
        if src is not None and dst is not None and src.has_attr(attr):
            dst[attr] = src[attr]

    def _reset_card_to_placeholder(card, slot_i: int, grund: str) -> None:
        """Setzt eine Rubrik-Karte auf einen ehrlichen, strukturell intakten
        Platzhalter zurück (siehe _sanitize_contaminated_slot für den
        Hauptanwendungsfall). Ausgelagert, damit auch die slot-übergreifende
        Redundanz-Prüfung unten dieselbe Reset-Logik wiederverwenden kann."""
        rtit = card.select_one(".rtit")
        realsatire = card.select_one(".realsatire")
        rnum = card.select_one(".rnum")
        tbl = card.select_one(".tbl")
        log(f"  Slot {slot_i}: bestehender Stand ist bereits kontaminiert "
            f"({grund}, vermutlich aus einem früheren fehlerhaften Lauf) — "
            f"wird NICHT weiter übernommen, stattdessen auf ehrlichen "
            f"Platzhalter zurückgesetzt.")
        placeholder_title = "Will be updated on the next run." if LANG == "en" else "Wird beim nächsten Lauf aktualisiert."
        placeholder_quip = "…" if LANG == "en" else "…"
        if rtit is not None:
            rtit.string = placeholder_title
        if realsatire is not None:
            realsatire.string = f"„{placeholder_quip}“"
        # WICHTIG (Bugfix, gefunden bei gründlicher Nachprüfung): Ohne
        # diesen Reset blieb das kicker-Label (.rnum) unverändert stehen —
        # z.B. weiterhin "Alltag · Betrug" neben dem Platzhaltertext, obwohl
        # genau dieses Label der Auslöser für die Bereinigung war.
        if rnum is not None:
            rnum.string = "Pending" if LANG == "en" else "Ausstehend"
        ai_tag = card.select_one(".ai-tag")
        if ai_tag is not None:
            ai_tag.string = "✦ Wird aktualisiert" if LANG != "en" else "✦ Updating"
        stand = card.select_one(".rub-stand")
        if stand is not None:
            stand.string = "Stand: wird nachgereicht" if LANG != "en" else "As of: pending"
        if tbl is not None:
            tbl.clear()
            # Leeres .tbl würde die Kartenlayout-CSS (tbl-head/cols/row)
            # sichtbar kaputt aussehen lassen — stattdessen eine ebenso
            # ehrliche, aber strukturell intakte Platzhalter-Tabelle.
            platzhalter_tbl = (
                '<div class="tbl-head"><span class="tt">No data yet</span>'
                '<span class="tag">pending</span></div>'
                '<div class="cols c3"><span>#</span><span class="l">Name</span><span>Value</span></div>'
                '<div class="row c3 is-last"><span class="rk">—</span>'
                '<span class="nm"><span class="lamp"></span>pending</span>'
                '<span class="v">—</span></div>'
                '<div class="tbl-foot">This category will be updated on the next successful run.</div>'
                if LANG == "en" else
                '<div class="tbl-head"><span class="tt">Noch keine Daten</span>'
                '<span class="tag">wird nachgereicht</span></div>'
                '<div class="cols c3"><span>#</span><span class="l">Name</span><span>Wert</span></div>'
                '<div class="row c3 is-last"><span class="rk">—</span>'
                '<span class="nm"><span class="lamp"></span>wird nachgereicht</span>'
                '<span class="v">—</span></div>'
                '<div class="tbl-foot">Diese Rubrik wird beim nächsten erfolgreichen Lauf aktualisiert.</div>'
            )
            tbl.append(BeautifulSoup(platzhalter_tbl, "html.parser"))

    def _sanitize_contaminated_slot(card_sel: str, slot_i: int) -> None:
        """Selbstheilung gegen dauerhaft weitergetragene Kontamination.

        WICHTIG (Bugfix, gefunden nach Live-Meldung "5 von 6 Rubriken zeigen
        Sätze über die eigene Recherche statt echter Meldungen"): Diese
        Funktion übernahm bisher BLIND jeden bestehenden Stand, egal ob er
        selbst schon ein Prozess-Kommentar war. Da ein fehlgeschlagener
        Fetch-Tag einen Slot einfach unverändert lässt (siehe inject()),
        wurde einmal kontaminierter Inhalt dadurch Tag für Tag identisch
        weiterkopiert — potenziell für immer, unabhängig davon, wie oft der
        Workflow seither lief. Diese Prüfung erkennt genau diesen Zustand
        NACH dem Kopieren und ersetzt ihn durch einen ehrlichen, klar
        erkennbaren Platzhalter statt ihn weiter zu übernehmen. Der nächste
        erfolgreiche Fetch für diesen Slot überschreibt den Platzhalter
        automatisch mit einer echten Meldung.

        WICHTIG (Bugfix, gefunden nach WEITERER Live-Meldung: "Alltag ·
        Betrug", "Region · Bayern" [tote Kuh], "Oberkassel" x3 dauerhaft
        sichtbar): Ein bereits veröffentlichter Einzelfall-Kriminalitäts-/
        Boulevard-Eintrag ist KEIN Prozess-Kommentar (er besteht die
        _ist_meta_kommentar-Prüfung), verstößt aber genauso gegen die
        inhaltliche Ausrichtung der Seite und würde sonst über Tage/Wochen
        unverändert weiterkopiert, bis zufällig ein neuer Fetch für genau
        diesen Slot gelingt. Dieselbe Selbstheilung greift daher jetzt auch
        hier — unabhängig davon, wie lange der Eintrag schon steht."""
        card = neu.select_one(card_sel)
        if card is None:
            return
        rtit = card.select_one(".rtit")
        realsatire = card.select_one(".realsatire")
        rnum = card.select_one(".rnum")
        tbl = card.select_one(".tbl")
        kombiniert = " ".join(
            el.get_text() for el in (rtit, realsatire, tbl) if el is not None
        )
        pseudo_item = {
            "kicker": rnum.get_text() if rnum is not None else "",
            "headline": rtit.get_text() if rtit is not None else "",
            "kommentar": realsatire.get_text() if realsatire is not None else "",
        }
        ist_kontaminiert = _ist_meta_kommentar(kombiniert)
        ist_einzelfall = _ist_einzelfall_kriminalitaet_oder_unglueck(pseudo_item)
        if not (ist_kontaminiert or ist_einzelfall):
            return
        grund = (
            "Prozess-Kommentar statt echter Meldung" if ist_kontaminiert else
            "Einzelfall-Kriminalitäts-/Boulevardmeldung ohne Vergleichs-/Rankingbezug"
        )
        _reset_card_to_placeholder(card, slot_i, grund)

    # Die 3 Rubrik-Karten (per data-slot, positionsstabil) + ihre Modals
    for slot_i in range(1, N_ITEMS + 1):
        card_sel = f'article.rub[data-slot="{slot_i}"]'
        _copy_attr(card_sel, "data-rubrik")
        for cls in (".rub-ico", ".rnum", ".rtit", ".realsatire", ".rub-stand", ".ai-tag"):
            _copy_text(f"{card_sel} {cls}")
        _copy_html(f"{card_sel} .tbl")
        _sanitize_contaminated_slot(card_sel, slot_i)

        modal_sel = f"#story-slot{slot_i}"
        _copy_text(f"{modal_sel} .story-modal-cat")
        _copy_text(f"{modal_sel} .story-modal-hl")
        _copy_text(f"{modal_sel} .story-modal-lead")
        _copy_text(f"{modal_sel} .story-source")
        _copy_html(f"{modal_sel} .story-body")

    # WICHTIG (Bugfix, gefunden nach Live-Meldung "Oberkassel"-Redundanz,
    # bestätigt auch auf der EN-Startseite mit zwei verschieden formulierten
    # Meldungen zu demselben Zugchaos): Ein bereits veröffentlichter, für
    # sich genommen unauffälliger Bestandseintrag kann trotzdem denselben
    # realen Fall wie eine ANDERE, ebenfalls schon veröffentlichte Karte
    # zeigen — z.B. weil beide an unterschiedlichen Tagen unabhängig
    # voneinander recherchiert wurden, bevor die Cross-Tage-Prüfung in
    # get_daily_items (vorhandene_ueberschriften) eingeführt wurde. Diese
    # abschließende Prüfung vergleicht ALLE Slot-Schlagzeilen paarweise;
    # bei Überlappung bleibt die niedrigere Slot-Nummer stehen, die höhere
    # wird auf den ehrlichen Platzhalter zurückgesetzt (der nächste
    # erfolgreiche Fetch für diesen Slot ersetzt ihn wieder).
    # WICHTIG (Bugfix, gefunden bei gründlicher Nachprüfung): Bereits auf
    # den Platzhalter zurückgesetzte Slots (siehe _reset_card_to_placeholder)
    # zeigen alle WORTGLEICH denselben Platzhaltertext — ohne diesen
    # Ausschluss hätte die Redundanz-Prüfung mehrere frisch bereinigte
    # Platzhalter fälschlich gegenseitig als 'Duplikat' erkannt und noch
    # mehr Slots unnötig zurückgesetzt.
    _platzhalter_texte = {
        "Will be updated on the next run.", "Wird beim nächsten Lauf aktualisiert.",
    }
    _slot_headlines = []
    for slot_i in range(1, N_ITEMS + 1):
        card = neu.select_one(f'article.rub[data-slot="{slot_i}"]')
        rtit = card.select_one(".rtit") if card is not None else None
        headline = rtit.get_text().strip() if rtit is not None else ""
        if headline and headline not in _platzhalter_texte and not _ist_meta_kommentar(headline):
            _slot_headlines.append((slot_i, card, headline))

    for i, (slot_i, card, headline) in enumerate(_slot_headlines):
        for other_slot_i, _, other_headline in _slot_headlines[:i]:
            if _paragraphs_content_overlap(headline, other_headline, 0.5):
                _reset_card_to_placeholder(
                    card, slot_i,
                    f"überschneidet sich mit der bereits angezeigten Rubrik {other_slot_i}",
                )
                break

    # Spotlight ("Tagesausgabe")
    for sel in ("#ta-cat", "#ta-hl", "#ta-text", "#ta-source"):
        _copy_text(sel)

    # Ticker (inklusive data-dup-Attribut, damit die Client-JS-Verdopplung
    # nicht versehentlich erneut anspringt)
    _copy_html(".ticker-inner")
    _copy_attr(".ticker-inner", "data-dup")

    # Datum/Zeitstempel
    _copy_text("#nav-issue-label")
    _copy_text("#update-time")

    # SEO: Title + Meta-Description/OG/Twitter (täglich mit Spotlight befüllt)
    title_alt = alt.find("title")
    title_neu = neu.find("title")
    if title_alt is not None and title_neu is not None:
        title_neu.string = title_alt.get_text()
    for sel in ("#meta-description", "#og-title", "#og-description",
                "#twitter-title", "#twitter-description"):
        _copy_attr(sel, "content")

    return str(neu)


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
    # WICHTIG (Architektur-Fix, gefunden nach Live-Meldung "Template-
    # Änderungen kommen nie an"): Die vorherige Logik wählte GANZ entweder
    # OUTPUT oder TEMPLATE als alleinige Basis. Sobald OUTPUT einmal die
    # richtige Struktur hatte, wurde TEMPLATE dauerhaft NIE MEHR gelesen —
    # jede spätere Korrektur an rein statischen Bereichen (z.B. ein
    # kaputter Nav-Link) kam dadurch nie auf der echten Seite an, egal wie
    # oft der Workflow lief. Jetzt: TEMPLATE ist IMMER die Basis (jede
    # statische Änderung wirkt damit garantiert sofort); nur die
    # KI-generierten dynamischen Felder werden bei Bedarf aus dem gestrigen
    # OUTPUT in die frische Template-Kopie übernommen (siehe
    # carry_over_dynamic_content), damit ein heutiger Generierungs-
    # Fehlschlag nicht auf Tag-0-Platzhalter zurückfällt.
    if not os.path.exists(TEMPLATE):
        log(f"FEHLER: {TEMPLATE} nicht gefunden.")
        return 1
    with open(TEMPLATE, encoding="utf-8") as fh:
        html = fh.read()

    def _hat_neue_struktur(html_text: str) -> bool:
        try:
            probe = BeautifulSoup(html_text, "html.parser")
        except Exception:
            return False
        return len(probe.select('article.rub[data-slot]')) == N_ITEMS

    if os.path.exists(OUTPUT):
        with open(OUTPUT, encoding="utf-8") as fh:
            bestehendes_html = fh.read()
        if _hat_neue_struktur(bestehendes_html):
            log(f"Verwende Template als Basis, übernehme dynamische Inhalte aus {OUTPUT}.")
            html = carry_over_dynamic_content(html, bestehendes_html)
        else:
            log(f"  {OUTPUT} hat noch die alte Struktur (vor dem Redesign) — "
                f"keine Altdaten übernommen, reines Template als Basis "
                f"(einmaliger Migrationsschritt).")
    else:
        log("Verwende Template als Basis (kein vorheriges OUTPUT vorhanden).")

    history = load_story_history()
    # WICHTIG (Bugfix, gefunden nach Live-Meldung "Oberkassel"-Redundanz):
    # Neben der Hintergrundstory-Historie fließt jetzt zusätzlich JEDE
    # tatsächlich veröffentlichte Tagesmeldung ein (daily_items_history,
    # siehe get_daily_items) — nicht nur Fälle, zu denen je eine
    # Hintergrundstory geschrieben wurde. Damit wird auch ein Slot, der über
    # mehrere Tage unverändert stehen bleibt, zuverlässig ausgeschlossen,
    # statt dass eine spätere Recherche denselben realen Fall ein zweites
    # Mal parallel zur alten, noch sichtbaren Meldung auswählt.
    daily_items_history = load_daily_items_history()
    # WICHTIG: Nur die neuesten ~35 Entitäten werden dem Prompt gezeigt (nicht
    # alle, die je nach Alter der Historie schnell auf 60-100+ anwachsen
    # können). Ein zu langer Ausschluss-Block überlastet den Prompt und kann
    # dazu führen, dass die KI aufgibt und stattdessen Platzhalter-Inhalte
    # oder eine wiederverwendete Feigenblatt-Quelle produziert. Die eigentliche
    # Wiederholungssperre (is_recently_used) prüft weiterhin gegen die
    # VOLLSTÄNDIGE Historie, nur die Prompt-Anzeige ist gekappt.
    combined_history = list(history) + list(daily_items_history)
    recent_first = sorted(combined_history, key=lambda e: e.get("date") or "", reverse=True)
    seen, avoid_entities = set(), []
    for entry in recent_first:
        ent = (entry.get("entity") or "").strip()
        if ent and ent not in seen:
            seen.add(ent)
            avoid_entities.append(ent)
        if len(avoid_entities) >= 35:
            break
    if avoid_entities:
        log(f"  {len(avoid_entities)} der neuesten Fälle/Entitäten (von "
            f"{len(seen)}+ aus Hintergrundstory- und Tagesmeldungs-Historie) "
            f"werden im Prompt vermieden.")

    # WICHTIG (Bugfix, gefunden nach Live-Meldung "Oberkassel"-Redundanz):
    # Die aktuell sichtbaren Schlagzeilen ALLER Slots (inkl. der heute evtl.
    # gar nicht aktualisierten) werden hier ausgelesen und an
    # dedupe_rubrik_topics durchgereicht, damit eine frisch recherchierte
    # Meldung nicht denselben realen Fall wie eine noch stehende, alte
    # Karte zeigt — unabhängig davon, ob das Modell dieselbe Entität als
    # 'schon verwendet' erkennt (siehe dedupe_rubrik_topics/avoid_entities).
    # Bewusst NUR die Schlagzeile (nicht zusätzlich der Kommentar) — siehe
    # Begründung im Docstring von dedupe_rubrik_topics.
    try:
        _bestand_soup = BeautifulSoup(html, "html.parser")
        vorhandene_ueberschriften = [
            c.select_one(".rtit").get_text()
            for c in _bestand_soup.select("article.rub[data-slot]")
            if c.select_one(".rtit")
        ]
    except Exception as exc:  # noqa: BLE001
        log(f"  WARNUNG: Bestehende Rubrik-Schlagzeilen konnten nicht gelesen werden ({exc}).")
        vorhandene_ueberschriften = []

    items = get_daily_items(date_label, avoid_entities, vorhandene_ueberschriften)
    stories = get_embedded_stories(date_label, (items or {}).get("items", {}))

    # WICHTIG: Bewusst NUR bei echtem neuen Inhalt schreiben (nicht immer).
    # Ein unbedingtes Schreiben würde zwar statische Korrekturen an noch
    # mehr Tagen durchsetzen, aber gleichzeitig #update-time IMMER ändern
    # — und genau das würde die Gesundheitsprüfung (health_check.py, siehe
    # dort) aushebeln, die anhand von "hat sich die Datei geändert" erkennt,
    # ob heute ECHT generiert wurde. Ein Totalausfall soll weiterhin
    # sichtbar bleiben (siehe GitHub-Issue-Automatik im Workflow), nicht
    # hinter einem scheinbaren Datums-Update verschwinden.
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
