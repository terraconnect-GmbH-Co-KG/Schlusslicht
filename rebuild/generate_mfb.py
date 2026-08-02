#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_mfb.py — Tagesaktualisierung für insights.html
================================================================================
Wird vom GitHub-Actions-Workflow .github/workflows/daily-update.yml gestartet,
nach generate.py und generate_visionen.py.

WICHTIGES DESIGNPRINZIP (Sorgfaltspflicht bei Meinungsinhalten):
Diese Meinungsstrecke recherchiert ihre 3 Themen und Zahlen jeden Tag FRISCH
per Websuche — kein fester Themen-Pool, keine Rotation, komplett unabhängig
von der Startseite. Die Absicherung gegen Halluzination läuft über dieselbe
technische Quellen-URL-Verifikation (HTTP-Check mit DNS-Fehler-Unterscheidung),
die auch generate.py verwendet: eine Zahl ohne echte, erreichbare Quelle wird
verworfen, nicht veröffentlicht.

Ablauf:
  1. Recherchiert 3 eigenständige politische/gesellschaftliche Themen samt
     Zahlen und Quelle (kein Bezug zur Startseite oder zu anderen Seiten).
  2. Verifiziert jede angegebene Quellen-URL technisch.
  3. Baut Text in insights.template.html ein, schreibt insights.html.
  4. Merkt sich die heutigen Themen in einer kleinen Historie-Datei
     (insights_history.json), damit sich Themen nicht postwendend
     wiederholen (kein Pool/Rotation, nur ein Wiederholungs-Schutz).
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

API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "perplexity/sonar"
LANG = os.environ.get("SL_LANG", "de").strip().lower()
TEMPLATE = "insights.en.template.html" if LANG == "en" else "insights.template.html"
OUTPUT = "insights.en.html" if LANG == "en" else "insights.html"
HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "insights_history_en.json" if LANG == "en" else "insights_history.json")
HISTORY_KEEP_DAYS = 60
TIMEOUT = 240
N_COLS = 3

MONATE = (
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"]
    if LANG == "en" else
    ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
     "August", "September", "Oktober", "November", "Dezember"]
)


def log(msg: str) -> None:
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


# Dieselbe kuratierte Vertrauensliste + DNS-bewusste Verifikationslogik wie
# in generate.py — siehe dortige Kommentare für die volle Begründung.
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
    """Identische Verifikationslogik wie in generate.py: nur ein echter
    DNS-Fehler (Domain existiert nicht) gilt als Beleg gegen die Existenz
    einer Quelle. Bot-Schutz-Statuscodes und Verbindungsfehler zu real
    existierenden Domains werden akzeptiert."""
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
        log(f"  Quelle antwortet mit HTTP {r.status_code} (echte Ablehnung, "
            f"z.B. Seite nicht mehr vorhanden) — verworfen: {url}")
        return False
    except requests.RequestException as exc:
        msg = str(exc).lower()
        ist_dns_fehler = any(marker in msg for marker in DNS_FAILURE_MARKERS)
        if not ist_dns_fehler:
            log(f"  Quelle technisch nicht erreichbar ({exc.__class__.__name__}, "
                f"kein DNS-Fehler) — wird akzeptiert: {url}")
            return True
        if _domain_is_trusted(url):
            log(f"  Quelle mit DNS-Fehler, aber etablierte Institution — "
                f"wird trotzdem akzeptiert: {url}")
            return True
        log(f"  Quellen-URL nicht erreichbar: {url} ({exc.__class__.__name__})")
        return False


# ── Themen-Historie (Wiederholungsschutz, KEIN Pool/Rotation) ───────────────
def load_history() -> list:
    if not os.path.exists(HISTORY_PATH):
        return []
    try:
        with open(HISTORY_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except Exception as exc:
        log(f"  Themen-Historie konnte nicht gelesen werden: {exc}")
        return []


def save_history(history: list) -> None:
    cutoff = datetime.date.today() - datetime.timedelta(days=HISTORY_KEEP_DAYS)
    pruned = []
    for entry in history:
        try:
            d = datetime.date.fromisoformat(entry.get("date", ""))
        except (ValueError, TypeError, AttributeError):
            continue
        if d >= cutoff:
            pruned.append(entry)
    try:
        with open(HISTORY_PATH, "w", encoding="utf-8") as fh:
            json.dump(pruned, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        log(f"  Themen-Historie konnte nicht gespeichert werden: {exc}")


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


def call_api_json(system: str, prompt: str, max_tokens: int, repair_retries: int = 2):
    """Wie call_api() + extract_json(), aber mit Selbstkorrektur: Wenn die
    Modellantwort kein gültiges JSON ergibt (z.B. durch Abschneiden bei zu
    knappem max_tokens oder nicht escapte Anführungszeichen im Fließtext),
    wird dem Modell der exakte Parse-Fehler zurückgemeldet und es bekommt
    bis zu `repair_retries` weitere Versuche, gültiges JSON zu liefern.
    (Identisches Muster wie in generate.py — behebt dieselbe Fehlerklasse,
    die dort zum 'Bahnbrech'-Vorfall führte: bei 5 Spalten mit vielen
    Textfeldern ist das Risiko für abgeschnittenes/kaputtes JSON mindestens
    genauso hoch wie bei den Hintergrundstorys.)"""
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


def _is_duplicate_sentence(s_norm: str, seen_norm: list, threshold: float) -> bool:
    """Siehe generate.py: Duplikat bei hoher Ähnlichkeit ODER wenn der
    kürzere Satzkern komplett im längeren enthalten ist."""
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
    "wir", "du", "ich", "damit", "dabei", "dadurch", "wurde",
}


def _significant_words(text: str) -> set:
    words = re.findall(r"[a-zäöüß]{4,}", text.lower())
    return {w for w in words if w not in _STOPWORTE}


def _paragraphs_content_overlap(a: str, b: str, threshold: float = 0.45) -> bool:
    """Erkennt inhaltliche Wiederholung anhand gemeinsamer inhaltstragender
    Wörter — erwischt auch umformulierte Wiederholungen."""
    wa, wb = _significant_words(a), _significant_words(b)
    smaller = min(len(wa), len(wb))
    if smaller < 4:
        return False
    return len(wa & wb) / smaller > threshold


def dedupe_column_paragraphs(paragraphs, threshold=0.75):
    """Zweistufiger Filter: 1) ganze Absätze mit hoher inhaltlicher
    Wortüberlappung verwerfen (auch umformulierte Wiederholungen), 2)
    innerhalb der verbleibenden Absätze zusätzlich doppelte Sätze entfernen."""
    # Stufe 1: inhaltlich wiederholte ganze Absätze verwerfen
    stage1 = []
    for para in paragraphs or []:
        text = str(para.get("text", "")).strip()
        if not text:
            continue
        if any(_paragraphs_content_overlap(text, str(k.get("text", ""))) for k in stage1):
            log(f"  Inhaltlich wiederholter Absatz entfernt: {text[:90]!r}")
            continue
        stage1.append(para)

    # Stufe 2: doppelte Sätze innerhalb der verbliebenen Absätze entfernen
    seen_norm = []
    result = []
    for para in stage1:
        text = str(para.get("text", "")).strip()
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
            new_para = dict(para)
            new_para["text"] = " ".join(kept)
            result.append(new_para)
    return result


# ── KI-Aufruf: eigenständige Recherche + Meinungskommentar ──────────────────
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
    """Erkennt Platzhalter-Domains (z.B. example.com) und generische
    Nachrichten-Landingpages statt konkreter Artikel — siehe generate.py
    für die volle Begründung dieses Bugfixes."""
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
        return True
    last = path.rsplit("/", 1)[-1].lower()
    last_ohne_endung = re.sub(r"\.(html?|php|aspx?)$", "", last)
    return bool(_GENERISCHER_PFAD_MUSTER.match(last_ohne_endung))


def _ist_meta_kommentar(text: str) -> bool:
    """Erkennt, ob ein Text den eigenen Rechercheprozess beschreibt ('ich
    habe nichts gefunden') statt ein echtes Thema zu behandeln — siehe
    generate.py für die volle Begründung dieses Bugfixes."""
    if not text:
        return False
    marker = (
        "suchergebnis", "websuche", "newsindex", "keine verwertbare",
        "nicht belegbar", "nicht verifizierbar", "recherche liefert",
        "rechercheergebnis", "keine sechs", "keine drei", "keine fünf",
        "keine vier", "keine zwei", "keine echte meldung", "kein sauberer treffer",
        "die suche hat", "die datenlage ist", "zu dünn", "breiter und tiefer",
    )
    t = text.lower()
    return any(m in t for m in marker)


def get_fresh_columns(date_label: str, avoid_themes: list, count: int = N_COLS, zusatzhinweis: str = ""):
    log(f"Recherchiere {count} frische politische/gesellschaftliche Themen "
        "samt Zahlen und Quelle …")

    system = (
        "Du bist Kolumnist der Meinungsstrecke 'more from behind' auf "
        "schlusslicht.de, einem deutschen linkssatirischen Magazin. "
        "Zielpublikum: belesene Erwachsene zwischen Mitte 40 und 70 (Generation "
        "X bis Babyboomer) — kein Jugend- oder Social-Media-Slang, keine "
        "Meme-Sprache, keine Anglizismen-Mischwörter (z. B. NIEMALS "
        "Konstruktionen wie 'irgendwas-treue' oder deutsch-englische "
        "Bastelwörter). Schreibe in klarer, druckreifer Sprache, wie es in "
        "einem gedruckten Satiremagazin (Stil: Titanic, Eulenspiegel, "
        "klassische Feuilleton-Polemik) stehen könnte — nicht wie eine "
        "Boulevard-Schlagzeile oder ein Tweet.\n\n"
        "Recherchiere zu JEDEM Thema per Websuche ECHTE, aktuelle Zahlen und "
        "Fakten. ABSOLUTE REGEL (nicht verhandelbar): Erfinde KEINE "
        "Statistiken, Studien, Prozentsätze oder Vergleichszahlen. Jede Zahl "
        "MUSS von einer echten, mit Websuche auffindbaren Quelle stammen, "
        "UND du musst die tatsächliche, funktionierende URL dieser Quelle "
        "angeben. Findest du zu einem Thema keine echte Zahl mit einer "
        "echten, existierenden Quelle, wähle ein anderes Thema, aber "
        "erfinde nichts. Wahrheitsgehalt geht immer vor Zuspitzung.\n\n"
        "ABSOLUTES VERBOT VON PROZESS-KOMMENTAREN: Findest du partout kein "
        "geeignetes Thema, wähle ein anderes Thema — schreibe NIEMALS einen "
        "Satz über die Suche selbst als Titel oder Absatz (z.B. NIEMALS "
        "'Keine verwertbare Meldung gefunden', 'Die Websuche liefert dafür "
        "heute zu wenig'). So ein Satz ist KEINE Kolumne, egal wie "
        "grammatikalisch korrekt er klingt, und wird automatisch erkannt "
        "und verworfen.\n\n"
        "JEDE KOLUMNE BRAUCHT IHRE EIGENE QUELLE: Verwende NIEMALS dieselbe "
        f"URL für mehrere der {N_COLS} Kolumnen, auch nicht eine generische "
        "Nachrichten-Übersichtsseite als austauschbares Feigenblatt. Die "
        "URL muss auf einen konkreten Artikel zu GENAU diesem Thema zeigen, "
        "nicht auf eine allgemeine Landingpage. Verwende NIEMALS "
        "Platzhalter-Domains wie 'example.com' — das ist keine echte "
        "Quelle, auch wenn die Domain technisch existiert.\n\n"
        "STIL (hier darfst und sollst du zuspitzen): pointiert, bissig, "
        "mit trockenem schwarzem Humor und klarer, DEUTLICH benannter "
        "linker, ökologisch-grüner politischer Haltung für die "
        "Benachteiligten — Satire durch Sprachwitz, Ironie und "
        "überraschende Bilder, nicht durch Ausrufezeichen oder reißerische "
        "Effekthascherei. Benenne Ursache und Verantwortung direkt und ohne "
        "übermäßige Zurückhaltung (strukturell: wer profitiert, wer trägt "
        "die politische Verantwortung, welche Verteilungslogik steckt "
        "dahinter) — deutlich direkter als eine vorsichtig-relativierende "
        "Zeitungsmeldung, aber weiterhin NICHT radikal und niemals plump: "
        "jede Zuspitzung bleibt an die recherchierten Fakten gebunden, keine "
        "Übertreibung ins Unbelegbare. Kurze, klare Sätze wechseln mit "
        "einem gelegentlich längeren, kunstvoll gebauten Satz. Der "
        "'punch'-Absatz soll die pointierteste, bissigste Formulierung der "
        "Kolumne enthalten. Der Titel darf originell und wortspielerisch "
        "sein, aber nicht reißerisch wie eine Boulevardzeile klingen. NUR "
        "vollständige, echte Wörter — KEINE erfundenen Kunstwörter oder "
        "abgebrochenen Wortspiele (z.B. NIEMALS 'Bahnbrech' statt "
        "'bahnbrechend'). Im Zweifel lieber sachlich-klar als "
        "kreativ-kaputt. Antworte AUSSCHLIESSLICH auf "
        + ("Englisch (US)" if LANG == "en" else "Deutsch") + " — keine "
        "chinesischen, kyrillischen, arabischen oder anderen "
        "nicht-lateinischen Schriftzeichen, auch nicht einzelne Wörter oder "
        "Zeichen davon.\n\n"
        "SPRACHLICHE KLARHEIT: Jeder der 4 Absätze hat eine feste, eigene "
        "Aufgabe (siehe Schema unten) und darf NICHTS aus einem anderen "
        "Absatz wiederholen — auch nicht sinngemäß oder mit anderen Worten. "
        "Prüfe vor der Ausgabe jeden Absatz gegen die vorherigen: Steht der "
        "Gedanke schon da? Falls ja, streiche ihn. Antworte NUR mit einem "
        "validen JSON-Objekt, keine Erklärung davor oder danach."
    )

    avoid_hinweis = (
        f"\n\nDiese Themen wurden in den letzten Tagen bereits verwendet — "
        f"wähle KEINES davon erneut: {', '.join(avoid_themes)}."
        if avoid_themes else ""
    )
    prompt = f"""Ausgabe vom {date_label}. Recherchiere und schreibe {count} eigenständige,
thematisch unterschiedliche politische/gesellschaftliche Meinungskolumnen.{avoid_hinweis}{zusatzhinweis}

Liefere GENAU dieses JSON-Schema:
{{
  "columns": [
    {{
      "thema": "1-2 Wörter Themen-Schlagwort, für Wiederholungsschutz",
      "tag": "" + ("Standpoint · short topic" if LANG == "en" else "Standpunkt · Kurzthema") + "",
      "title": "prägnanter Titel wie eine Schlagzeile, max 40 Zeichen",
      "paragraphs": [
        {{"text": "Absatz 1 — NUR: Einstieg mit einer recherchierten Zahl, nüchtern dargestellt. Keine Bewertung.", "punch": false}},
        {{"text": "Absatz 2 — NUR: der zugespitzte Kernsatz/die Wertung dazu. Die Zahl aus Absatz 1 nicht wiederholen.", "punch": true}},
        {{"text": "Absatz 3 — NUR: zusätzlicher Kontext oder Gegenargument, das in Absatz 1+2 noch nicht vorkam.", "punch": false}},
        {{"text": "Absatz 4 — NUR: eine konkrete Schlussfolgerung/Forderung, die nirgends vorher stand.", "punch": false}}
      ],
      "bignum_text": "die recherchierte Kernzahl, wortwörtlich, z.B. '204×' oder '~7 %'",
      "bignum_caption": "1 kurzer Satz, was die Zahl bedeutet",
      "stat_bullets": [
        {{"label": "Bezeichnung", "value": "Wert, wortwörtlich recherchiert"}}
        // 2-3 Einträge
      ],
      "source_name": "Name der echten Quelle, z.B. 'Destatis' oder 'OECD'",
      "source_url": "https://echte-existierende-url, die exakt zu source_name passt",
      "source_date": "Datum/Zeitraum der Quelle, z.B. '2025'"
    }}
    // genau {count} Einträge, thematisch unterschiedlich
  ]
}}"""

    data = call_api_json(system, prompt, max_tokens=9000)
    if not data or "columns" not in data or not isinstance(data["columns"], list):
        log("  Keine verwertbaren Kommentar-Daten erhalten.")
        return None
    return data


def get_daily_columns(date_label: str, avoid_themes: list) -> dict:
    """Holt bis zu N_COLS Kolumnen, verteilt auf feste Slot-Nummern (1..N_COLS),
    mit Retry NUR für tatsächlich noch fehlende Slots.

    WICHTIG (Bugfix, gefunden bei einer Live-Prüfung des Fehlers "viele
    Kategorien bleiben nach dem ersten Lauf leer" auf generate.py): Diese
    Funktion ersetzt die alte Logik in main(), die GENAU EINMAL alle N_COLS
    Kolumnen auf einmal anfragte und danach nacheinander mehrere Listen
    filterte (columns -> verified). Zwei unabhängige Probleme dabei:
    (1) Schlug die einmalige Anfrage komplett oder teilweise fehl, gab es
    KEINEN zweiten Versuch — betroffene Slots blieben für den ganzen Tag
    leer bzw. beim alten Stand, obwohl ein erneuter, enger gefasster
    Versuch oft erfolgreich gewesen wäre (siehe generate.py get_daily_items
    für dasselbe Muster und dieselbe Behebung).
    (2) SCHWERWIEGENDER: Wurde EINE von mehreren Kolumnen durch einen der
    Filter (Prozess-Kommentar, geteilte URL, generische Domain, technische
    Quellen-Prüfung) verworfen, RUTSCHTEN die verbleibenden Kolumnen in
    der gefilterten Liste nach vorne — inject() schrieb sie dadurch anhand
    ihrer neuen LISTENPOSITION in den FALSCHEN Slot (#col1/#col2/#col3
    statt ihres ursprünglich recherchierten Slots). Diese Funktion ordnet
    jede Kolumne von Anfang an einer festen Slot-Nummer zu (dict statt
    Liste) — ein verworfener Slot bleibt schlicht LEER (der bestehende
    Stand bleibt unverändert), statt dass sich alles nachrückt."""
    spalten = {}
    schon_gewaehlte_themen = list(avoid_themes)
    letzter_fehlgrund = ""
    for versuch in range(4):
        fehlend = N_COLS - len(spalten)
        if fehlend <= 0:
            break
        log(f"Recherchiere {fehlend} frische Kolumne(n)"
            f"{f' (Versuch {versuch + 1}, {fehlend} von {N_COLS} fehlen noch)' if versuch else ''} …")

        extra_hinweis = ""
        if versuch > 0 and letzter_fehlgrund:
            extra_hinweis = (
                f"\n\nWICHTIG: Dein letzter Versuch hat nicht genug verwertbare "
                f"Kolumnen geliefert ({letzter_fehlgrund}). Wähle diesmal andere, "
                f"dir noch nicht eingefallene Themen mit je einer echten, "
                f"unterschiedlichen Quelle."
            )

        data = get_fresh_columns(date_label, schon_gewaehlte_themen, fehlend, extra_hinweis)
        kandidaten = [c for c in (data or {}).get("columns", [])[:fehlend] if isinstance(c, dict)]
        for col in kandidaten:
            col["paragraphs"] = dedupe_column_paragraphs(col.get("paragraphs"))

        url_counts = {}
        for col in kandidaten:
            u = (col.get("source_url") or "").strip().lower()
            if u:
                url_counts[u] = url_counts.get(u, 0) + 1
        doppelte_urls = {u for u, c in url_counts.items() if c > 1}
        if doppelte_urls:
            log(f"  WARNUNG: {len(doppelte_urls)} Quellen-URL(s) werden von "
                f"mehreren Kolumnen gleichzeitig verwendet — Feigenblatt-"
                f"Verdacht, betroffene Kolumnen werden verworfen.")

        offene_keys = [i for i in range(1, N_COLS + 1) if i not in spalten]
        neue = {}
        for idx, key in enumerate(offene_keys):
            if idx >= len(kandidaten):
                log(f"  Kolumne {key}: keine verwertbare Antwort erhalten — übersprungen.")
                continue
            col = kandidaten[idx]
            title = (col.get("title") or "").strip()
            paras_text = " ".join(p.get("text", "") for p in col.get("paragraphs", []) if isinstance(p, dict))
            if not title or not paras_text:
                log(f"  Kolumne {key}: unvollständiger Eintrag — übersprungen.")
                continue
            if _ist_meta_kommentar(title) or _ist_meta_kommentar(paras_text):
                log(f"  Kolumne {key} ({title!r}): Text beschreibt den eigenen "
                    f"Rechercheprozess statt ein echtes Thema zu behandeln — "
                    f"verworfen, keine Platzhalter-Texte als Inhalt.")
                continue
            url = (col.get("source_url") or "").strip().lower()
            if url and url in doppelte_urls:
                log(f"  Kolumne {key} ({title!r}): teilt sich eine Quellen-URL "
                    f"mit anderen Kolumnen — verworfen.")
                continue
            if url and _url_ist_zu_generisch(url):
                log(f"  Kolumne {key} ({title!r}): Quellen-URL ist eine "
                    f"generische Landingpage oder Platzhalter-Domain — verworfen.")
                continue
            neue[key] = col

        if neue:
            spalten.update(neue)
            for col in neue.values():
                th = (col.get("thema") or "").strip()
                if th:
                    schon_gewaehlte_themen.append(th)

        if len(spalten) >= N_COLS:
            break

        letzter_fehlgrund = (
            f"alle Kolumnen teilten sich eine Quelle ({', '.join(doppelte_urls)})"
            if doppelte_urls else
            f"nur {len(neue)} von {fehlend} angeforderten Kolumnen war(en) verwertbar"
        )
        if versuch < 3:
            log(f"  Noch nicht vollständig ({len(spalten)}/{N_COLS}, {letzter_fehlgrund}) "
                f"— wiederhole für die fehlenden {N_COLS - len(spalten)} Plätze "
                f"(Versuch {versuch + 2}/4).")

    return spalten


def review_and_rewrite_columns(columns: list, date_label: str) -> list:
    """NEUER Zwischenschritt vor der Veröffentlichung: Prüft Sinnhaftigkeit
    von Titel und Fliesstext-Absätzen und formuliert bei Bedarf sprachlich
    um. WICHTIG: Rührt NIEMALS bignum_text, bignum_caption oder
    stat_bullets an — diese werden separat per Quellen-URL-Verifikation gegen
    echte, extrahierte Fakten geprüft und dürfen durch diesen Schritt
    nicht verändert werden. Auch bei Titel/Absätzen wird geprüft, dass
    keine neuen, im Original nicht vorhandenen Zahlen hinzukommen."""
    pruefbar = {}
    for i, col in enumerate(columns):
        if not isinstance(col, dict):
            continue
        paras = [p.get("text", "") for p in col.get("paragraphs", []) if isinstance(p, dict)]
        if col.get("title") and paras:
            pruefbar[f"col{i}"] = {"title": col["title"], "paragraphs": paras}

    if not pruefbar:
        return columns

    log("  Prüfe Insights-Kolumnen auf Sinnhaftigkeit vor Veröffentlichung …")
    system = (
        "Du bist Chef vom Dienst bei schlusslicht.de (Rubrik 'Insights') "
        "und prüfst Texte vor der Veröffentlichung. Du erfindest NIEMALS "
        "neue Fakten oder Zahlen — du darfst aber vorhandene, korrekte "
        "Inhalte SPRACHLICH verbessern (Grammatik, Klarheit, holprige "
        "Formulierungen, Redundanz, kaputte Kunstwörter), wenn das "
        "inhaltlich exakt dasselbe aussagt wie vorher. Antworte "
        "AUSSCHLIESSLICH auf " + ("Englisch (US)" if LANG == "en" else "Deutsch") +
        ". Antworte NUR mit validem JSON, keine Erklärung."
    )
    prompt = (
        "Prüfe jede Kolumne: Ist der Titel ein echtes, vollständiges Wort/"
        "eine echte Phrase (kein abgebrochenes Kunstwort wie 'Bahnbrech' "
        "statt 'bahnbrechend')? Sind die Absätze in sich sinnvoll, klar "
        "formuliert, ohne Wiederholung?\n\n"
        "WENN INHALTLICH KORREKT, ABER SCHLECHT FORMULIERT: gib 'ok': true "
        "UND 'title_neu'/'paragraphs_neu' (Liste, gleiche Reihenfolge/Länge) "
        "mit einer verbesserten Fassung zurück — DIESELBEN Fakten/Zahlen, "
        "nur klarer formuliert. NIEMALS neue Zahlen einführen. Lass die "
        "'_neu'-Felder weg, wenn der Text bereits gut ist.\n\n"
        "WENN INHALTLICH KAPUTT: gib 'ok': false mit kurzer 'grund'-Angabe zurück.\n\n"
        f"Kolumnen:\n{json.dumps(pruefbar, ensure_ascii=False, indent=2)}\n\n"
        "Antworte als JSON, mit genau denselben Schlüsseln wie oben (z.B. 'col0'):\n"
        '{"col0": {"ok": true}, "col1": {"ok": true, "title_neu": "...", '
        '"paragraphs_neu": ["...", "...", "...", "..."]}, "col2": {"ok": false, "grund": "..."}}'
    )
    urteil = call_api_json(system, prompt, max_tokens=4000) or {}

    for i, col in enumerate(columns):
        if not isinstance(col, dict) or f"col{i}" not in urteil:
            continue
        bewertung = urteil[f"col{i}"]
        if bewertung.get("ok") is False:
            log(f"  Kolumne {i} ({col.get('title', '')!r}): Sinnhaftigkeits-Prüfung "
                f"fehlgeschlagen ({bewertung.get('grund', 'kein Grund')}) — "
                f"Titel/Text bleiben unverändert (Zahlen-Validierung greift "
                f"separat weiterhin).")
            continue

        alte_zahlen = _numbers_in(col.get("title", "")) | _numbers_in(
            " ".join(p.get("text", "") for p in col.get("paragraphs", []) if isinstance(p, dict))
        )

        title_neu = (bewertung.get("title_neu") or "").strip()
        if title_neu and not (_numbers_in(title_neu) - alte_zahlen):
            log(f"  Kolumne {i}: Titel sprachlich überarbeitet.")
            col["title"] = title_neu
        elif title_neu:
            log(f"  Kolumne {i}: Titel-Umformulierung enthält neue Zahlen — verworfen.")

        paras_neu = bewertung.get("paragraphs_neu")
        if isinstance(paras_neu, list) and len(paras_neu) == len(col.get("paragraphs", [])):
            neuer_text = " ".join(str(p) for p in paras_neu)
            if not (_numbers_in(neuer_text) - alte_zahlen):
                for p_obj, neuer_p_text in zip(col["paragraphs"], paras_neu):
                    if isinstance(p_obj, dict) and str(neuer_p_text).strip():
                        p_obj["text"] = str(neuer_p_text).strip()
                log(f"  Kolumne {i}: Absätze sprachlich überarbeitet.")
            else:
                log(f"  Kolumne {i}: Absatz-Umformulierung enthält neue Zahlen — verworfen.")

    return columns


# ── Validierung: Zahlen-Vergleich für review_and_rewrite_columns ───────────
def _numbers_in(text: str) -> set:
    return set(re.findall(r"\d+[.,]?\d*", text or ""))


# ── HTML-Injektion ────────────────────────────────────────────────────────────
def set_text(node, value):
    if node is not None and value is not None:
        node.clear()
        node.append(str(value))


def inject(html: str, columns: dict) -> str:
    """WICHTIG (Bugfix, siehe get_daily_columns): columns ist jetzt ein
    dict {slot_nummer: kolumne}, KEINE Liste mehr — jede Kolumne landet
    garantiert im Slot, für den sie recherchiert wurde, statt anhand ihrer
    Position in einer nach Filterung verkürzten Liste."""
    soup = BeautifulSoup(html, "html.parser")

    for i, col in columns.items():
        set_text(soup.select_one(f"#col{i}-tag"), col.get("tag"))
        set_text(soup.select_one(f"#col{i}-h2"), col.get("title"))

        # Alte Absätze entfernen, neue einfügen
        body = soup.select_one(f"#col{i}-body")
        if body is not None:
            for old_p in body.select("p.gen-para"):
                old_p.decompose()
            for para in col.get("paragraphs", []):
                p = soup.new_tag("p", attrs={"class": "gen-para punch" if para.get("punch") else "gen-para"})
                p.string = str(para.get("text", ""))
                body.append(p)

        set_text(soup.select_one(f"#col{i}-bignum-text"), col.get("bignum_text"))
        set_text(soup.select_one(f"#col{i}-bigcap"), col.get("bignum_caption"))

        bullets = col.get("stat_bullets", [])
        for j in range(1, 4):
            li = soup.select_one(f"#col{i}-stat{j}")
            if li is None:
                continue
            if j <= len(bullets):
                b = bullets[j - 1]
                li.clear()
                li.append(f"{b.get('label', '')} ")
                strong = soup.new_tag("b")
                strong.string = str(b.get("value", ""))
                li.append(strong)

        # Quelle: direkt aus der heute recherchierten und verifizierten
        # Angabe der KI (kein fester Datenpool mehr).
        name = (col.get("source_name") or "").strip()
        date = (col.get("source_date") or "").strip()
        prefix = "Source" if LANG == "en" else "Quelle"
        src_text = f"{prefix}: {name}" + (f" · {date}" if date else "") if name else f"{prefix}: —"
        set_text(soup.select_one(f"#col{i}-src"), src_text)

    return str(soup)


def _hat_neue_struktur(html_text: str) -> bool:
    try:
        probe = BeautifulSoup(html_text, "html.parser")
    except Exception:
        return False
    return len(probe.select("article.col")) == N_COLS and probe.select_one("#col4") is None


def _carry_over_columns(template_html: str, output_html: str) -> str:
    """WICHTIG: modulweite Funktion (nicht mehr in main() verschachtelt),
    damit die Selbstheilung unten unabhängig testbar ist — dieselbe
    Überlegung wie bei carry_over_dynamic_content in generate.py."""
    try:
        neu = BeautifulSoup(template_html, "html.parser")
        alt = BeautifulSoup(output_html, "html.parser")
    except Exception as exc:
        log(f"  WARNUNG: Übernahme der Altdaten übersprungen (Parse-Fehler: {exc}).")
        return template_html
    for i in range(1, N_COLS + 1):
        for sel in (f"#col{i}-tag", f"#col{i}-h2", f"#col{i}-bignum-text",
                    f"#col{i}-bigcap", f"#col{i}-src"):
            src, dst = alt.select_one(sel), neu.select_one(sel)
            if src is not None and dst is not None:
                dst.string = src.get_text()
        for j in range(1, 4):
            sel = f"#col{i}-stat{j}"
            src, dst = alt.select_one(sel), neu.select_one(sel)
            if src is not None and dst is not None:
                dst.clear()
                for child in list(src.children):
                    dst.append(child.extract() if hasattr(child, "extract") else str(child))
        body_sel = f"#col{i}-body"
        src, dst = alt.select_one(body_sel), neu.select_one(body_sel)
        if src is not None and dst is not None:
            for old_p in dst.select("p.gen-para"):
                old_p.decompose()
            for p in src.select("p.gen-para"):
                dst.append(p.extract())

        # WICHTIG (Bugfix, gefunden bei gründlicher Nachprüfung nach dem
        # "viele Kategorien bleiben leer"-Fehler in generate.py): Ein
        # fehlgeschlagener Tag lässt eine Kolumne unverändert (siehe
        # inject()/get_daily_columns) — war der ÜBERNOMMENE Bestand
        # selbst schon ein Prozess-Kommentar (z.B. aus einem früheren
        # fehlerhaften Lauf, bevor dieser Filter existierte), würde er
        # sonst für immer identisch weiterkopiert. Nach dem Kopieren
        # wird deshalb geprüft, ob der jetzt in neu stehende Text schon
        # kontaminiert ist, und bei Bedarf auf einen ehrlichen
        # Platzhalter zurückgesetzt.
        h2 = neu.select_one(f"#col{i}-h2")
        body_now = neu.select_one(body_sel)
        titel_text = h2.get_text() if h2 is not None else ""
        body_text = body_now.get_text() if body_now is not None else ""
        if _ist_meta_kommentar(titel_text) or _ist_meta_kommentar(body_text):
            log(f"  Kolumne {i}: bestehender Stand ist bereits ein "
                f"Prozess-Kommentar (vermutlich aus einem früheren "
                f"fehlerhaften Lauf) — wird NICHT weiter übernommen, "
                f"stattdessen auf ehrlichen Platzhalter zurückgesetzt.")
            placeholder = "Will be updated on the next run." if LANG == "en" else "Wird beim nächsten Lauf aktualisiert."
            if h2 is not None:
                h2.string = placeholder
            if body_now is not None:
                for old_p in body_now.select("p.gen-para"):
                    old_p.decompose()
                p = neu.new_tag("p", attrs={"class": "gen-para"})
                p.string = "…"
                body_now.append(p)
            src_el = neu.select_one(f"#col{i}-src")
            if src_el is not None:
                src_el.string = "Source: pending" if LANG == "en" else "Quelle: wird nachgereicht"
    return str(neu)


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
    log(f"more_from_behind-Ausgabe: {date_label}")

    if not os.path.exists(TEMPLATE):
        log(f"FEHLER: {TEMPLATE} nicht gefunden.")
        return 1

    history = load_history()
    # WICHTIG (siehe generate.py für die volle Begründung): nur die
    # neuesten ~35 Themen werden im Prompt gezeigt, um Prompt-Überlastung
    # zu vermeiden — ein zu langer Ausschluss-Block kann dazu führen, dass
    # die KI aufgibt und stattdessen Platzhalter-Inhalte produziert.
    recent_first = list(reversed(history))
    seen, avoid_themes = set(), []
    for entry in recent_first:
        th = (entry.get("thema") or "").strip()
        if th and th not in seen:
            seen.add(th)
            avoid_themes.append(th)
        if len(avoid_themes) >= 35:
            break
    if avoid_themes:
        log(f"  {len(avoid_themes)} der neuesten Themen (von {len(seen)}+ in "
            f"den letzten {HISTORY_KEEP_DAYS} Tagen) werden im Prompt vermieden.")

    spalten = get_daily_columns(date_label, avoid_themes)
    if not spalten:
        log("Keine Inhalte erzeugt — insights.html bleibt unverändert.")
        return 0

    # WICHTIG (Bugfix, siehe get_daily_columns): review_and_rewrite_columns
    # erwartet/liefert eine Liste gleicher Länge/Reihenfolge (mutiert jede
    # Kolumne in place) — wir übergeben sie in fester Slot-Reihenfolge und
    # ordnen das Ergebnis 1:1 denselben Slot-Nummern wieder zu, statt danach
    # erneut positionsbasiert weiterzuverarbeiten.
    geordnete_keys = sorted(spalten.keys())
    kol_liste = review_and_rewrite_columns([spalten[k] for k in geordnete_keys], date_label)
    for k, col in zip(geordnete_keys, kol_liste):
        spalten[k] = col

    # Sicherheitsnetz: Spalten mit nicht verifizierbarer Quelle aussortieren
    # (technischer HTTP-Check, dieselbe Logik wie in generate.py) — bleibt
    # slot-treu (dict), statt in eine neue, ggf. verkürzte Liste zu wandern.
    log("  Verifiziere Quellen-URLs technisch (HTTP-Check) …")
    verified = {}
    for key, col in spalten.items():
        url = (col.get("source_url") or "").strip()
        if not verify_url(url):
            log(f"  Kolumne {key} ({col.get('title', '(ohne Titel)')!r}): "
                f"Quellen-URL fehlt oder nicht erreichbar "
                f"({url or 'keine URL angegeben'}) — komplett verworfen, "
                f"keine Halluzinationen ohne Beleg.")
            continue
        log(f"  Kolumne {key} ({col.get('title', '')!r}): Quelle verifiziert ({url})")
        verified[key] = col

    if not verified:
        log("Keine Kolumne hat die Quellen-Verifikation bestanden — Datei bleibt unverändert.")
        return 0

    # WICHTIG (Architektur-Fix, siehe generate.py für die volle Begründung):
    # Vorher wurde bei passender Struktur OUTPUT als GANZE Basis verwendet,
    # wodurch TEMPLATE dauerhaft nie mehr gelesen wurde — jede spätere
    # Korrektur an rein statischen Bereichen (Nav, Footer, CSS, Disclaimer-
    # Texte) kam dadurch nie auf der echten Seite an. Jetzt: TEMPLATE ist
    # immer die Basis, nur die KI-generierten Spalteninhalte werden bei
    # Bedarf aus dem gestrigen OUTPUT übernommen (siehe _hat_neue_struktur/
    # _carry_over_columns, jetzt modulweite Funktionen oben).
    with open(TEMPLATE, encoding="utf-8") as fh:
        html = fh.read()

    if os.path.exists(OUTPUT):
        with open(OUTPUT, encoding="utf-8") as fh:
            bestehendes_html = fh.read()
        if _hat_neue_struktur(bestehendes_html):
            log(f"Verwende Template als Basis, übernehme dynamische Inhalte aus {OUTPUT}.")
            html = _carry_over_columns(html, bestehendes_html)
        else:
            log(f"  {OUTPUT} hat noch die alte Struktur (vor dem Redesign) — "
                f"keine Altdaten übernommen, reines Template als Basis "
                f"(einmaliger Migrationsschritt).")
    else:
        log("Verwende Template als Basis (kein vorheriges OUTPUT vorhanden).")

    html = inject(html, verified)

    new_themes = [c.get("thema", "").strip() for c in verified.values() if c.get("thema")]
    today_iso = datetime.date.today().isoformat()
    if new_themes:
        save_history(history + [{"date": today_iso, "thema": t} for t in new_themes])
        log(f"Themen-Historie aktualisiert: {', '.join(new_themes)}")

    with open(OUTPUT, "w", encoding="utf-8") as fh:
        fh.write(html)
    log(f"{OUTPUT} geschrieben ({len(html):,} Zeichen), {len(verified)}/{N_COLS} Spalten aktualisiert.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
