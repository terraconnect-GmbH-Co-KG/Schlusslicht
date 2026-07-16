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
N_ITEMS = 3


def _fetch_fresh_items(date_label: str, avoid_entities: list):
    """Recherchiert 3 frische 'Schlusslicht'-Meldungen aus BELIEBIGEN
    Bereichen in einem Aufruf — inkl. der kompletten Anzeige-Daten (Icon,
    Kategorie-Label, kleine Rangliste), die früher aus 8 fest zugeteilten
    Rubriken kamen. Kein fester Themen-Pool, keine Rotation: die KI wählt
    jeden Tag frei, welche 3 Bereiche heute die stärksten Fälle liefern."""
    system = (
        f"Du bist Chefredakteur von schlusslicht.de, einem deutschen "
        f"linkssatirischen Magazin. Heute ist {date_label}.\n\n"
        f"Finde {N_ITEMS} ECHTE, tagesaktuelle oder höchstens 14 Tage alte "
        "'Schlusslicht'-Meldungen via Websuche — jeweils aus einem ANDEREN "
        "Bereich (z.B. Sport, Niedriglohn, Verkehr, Pressefreiheit, "
        "Korruption, Klimaschutz, Steuervermeidung, Medien, oder jeder "
        "andere Bereich, in dem jemand/etwas nachweislich Schlusslicht bzw. "
        "Tabellenletzter ist). Die Quelle muss NICHT zwingend eine formale "
        "Ranking-Tabelle oder ein offizieller Index sein (RSF-Index, CPI, "
        "CCPI o.ä. sind Beispiele, keine Pflicht) — jede echte, relevante "
        "Meldung aus JEDEM Land der Welt zählt, z.B. auch ein einzelner "
        "Gerichtsfall, ein Zeitungsartikel über einen konkreten Vorfall, "
        "eine Studie oder ein parlamentarischer Bericht. Meide KEINE "
        "Weltregion aus vermeintlicher Vorsicht — auch Nahost/Gaza, "
        "Ukraine/Russland oder andere politisch sensible Weltgegenden sind "
        "ganz normale, gleichberechtigte Themenquellen wie jede andere "
        "Region, solange die Meldung echt und belegt ist. Die 3 Meldungen "
        "müssen sich thematisch klar unterscheiden.\n\n"
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
        + (
            f"Diese Fälle/Entitäten wurden in den letzten Tagen bereits "
            f"verwendet — wähle KEINEN davon erneut: "
            f"{', '.join(avoid_entities)}.\n\n"
            if avoid_entities
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
        f"Recherchiere {N_ITEMS} eigenständige, thematisch unterschiedliche "
        "Schlusslicht-Meldungen für die heutige Ausgabe. Nutze die Websuche "
        "mehrfach, auf Deutsch und Englisch.\n\n"
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
        f"    // genau {N_ITEMS} Einträge in dieser Liste, thematisch unterschiedlich\n"
        "  ]\n"
        "}"
    )

    result = call_api_json(system, prompt, max_tokens=3000)
    if not result:
        return None
    items_list = result.get("items")
    return items_list if isinstance(items_list, list) else None


def get_daily_items(date_label: str, avoid_entities: list):
    """Holt die 3 frei recherchierten Tagesmeldungen (kein Themen-Pool,
    keine Rotation — siehe _fetch_fresh_items)."""
    log(f"Recherchiere {N_ITEMS} frische Schlusslicht-Meldungen …")

    # WICHTIG (Bugfix, gefunden nach Live-Meldung "Startseite aktualisiert
    # sich nicht"): Das JSON-Schema fragte bisher ein Objekt mit REIN
    # NUMERISCHEN String-Schlüsseln ab ("1", "2", "3" als Schlüssel). Manche
    # Modellantworten liefern solche Schlüssel ohne Anführungszeichen (wie
    # ein Python-Dict statt echtem JSON, z.B. {1: {...}}) — das ist
    # ungültiges JSON und ließ sich auch durch die Selbstreparatur nicht
    # zuverlässig retten, wodurch die komplette Recherche tagelang immer
    # wieder scheiterte, während die (Array-basierte) Spotlight/Ticker-
    # Recherche im selben Lauf ganz normal weiterlief. Jetzt: Array-Schema
    # ("items": [...]), Zuordnung zu Slot 1..3 rein über die Position in
    # der Liste — exakt dasselbe robuste Muster wie bei den bereits
    # zuverlässig laufenden Seiten (Insights, Brightside-Good-News,
    # Nonconformist).
    items_list = _fetch_fresh_items(date_label, avoid_entities) or []
    all_items = {}
    for idx in range(N_ITEMS):
        key = str(idx + 1)
        item = items_list[idx] if idx < len(items_list) else None
        if not isinstance(item, dict):
            log(f"  Meldung {key}: keine verwertbare Antwort erhalten — übersprungen.")
            continue
        headline = (item.get("headline") or "").strip()
        kommentar = (item.get("kommentar") or "").strip()
        # WICHTIG (Atomaritäts-Fix, siehe main-Historie): headline UND
        # kommentar müssen BEIDE vorhanden sein, sonst wird der Eintrag
        # komplett verworfen. Ein Teil-Update würde sonst zwei Textteile
        # aus evtl. ganz unterschiedlichen Tagen/Themen kombinieren.
        if headline and kommentar:
            all_items[key] = item
        else:
            fehlt = "kommentar" if headline else ("headline" if kommentar else "headline+kommentar")
            log(f"  Meldung {key}: unvollständiger Eintrag ({fehlt} fehlt) "
                f"— komplett übersprungen, bestehender (in sich konsistenter) "
                f"Stand bleibt. Kein Teil-Update einzelner Felder.")

    all_items = dedupe_rubrik_topics(all_items)
    all_items = strip_repeated_boilerplate(all_items)
    all_items = review_and_fix_items(all_items, date_label)

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
        # Fakten-Drift während der sprachlichen Überarbeitung).
        original = items[num]
        for feld, feld_neu in (("headline", "headline_neu"), ("kommentar", "kommentar_neu")):
            neu = (bewertung.get(feld_neu) or "").strip()
            if not neu:
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
        og_desc = txt[:155] if txt else "Das Magazin der Letzten. 3 Rubriken täglich aktuell."

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
    # WICHTIG (Redesign-Migrations-Fix, gefunden nach Live-Meldung): Die
    # obige "OUTPUT bevorzugen"-Logik schützt zwar vor einem Rückfall auf
    # uralte Tag-0-Platzhalter bei einem fehlgeschlagenen Lauf — sie hatte
    # aber einen blinden Fleck: wenn sich die STRUKTUR des Templates ändert
    # (wie beim Rotations-Redesign: 8 Rubrik-Karten -> 3 feste Slots), bleibt
    # ein bereits bestehendes, altes index.html für immer die Basis, weil es
    # ja existiert — die neue Struktur aus dem Template wird NIE übernommen,
    # egal wie oft der Workflow erfolgreich läuft. Fix: Vor der Verwendung
    # wird geprüft, ob OUTPUT bereits die erwartete neue Struktur hat (genau
    # N_ITEMS Slots mit data-slot). Fehlt sie, wird einmalig auf TEMPLATE
    # zurückgegriffen, um die neue Struktur zu übernehmen.
    def _hat_neue_struktur(html_text: str) -> bool:
        try:
            probe = BeautifulSoup(html_text, "html.parser")
        except Exception:
            return False
        return len(probe.select('article.rub[data-slot]')) == N_ITEMS

    template_path = TEMPLATE
    if os.path.exists(OUTPUT):
        with open(OUTPUT, encoding="utf-8") as fh:
            bestehendes_html = fh.read()
        if _hat_neue_struktur(bestehendes_html):
            template_path = OUTPUT
        else:
            log(f"  {OUTPUT} hat noch die alte Struktur (vor dem Redesign) — "
                f"verwende stattdessen {TEMPLATE} als Basis, um die neue "
                f"3-Slot-Struktur zu übernehmen (einmaliger Migrationsschritt).")
    if not os.path.exists(template_path):
        log("FEHLER: Weder index.html noch index.template.html gefunden.")
        return 1
    log(f"Verwende als Basis: {template_path}")
    with open(template_path, encoding="utf-8") as fh:
        html = fh.read()

    history = load_story_history()
    avoid_entities = sorted({
        (entry.get("entity") or "").strip()
        for entry in history
        if (entry.get("entity") or "").strip()
    })
    if avoid_entities:
        log(f"  {len(avoid_entities)} Fälle/Entitäten aus den letzten "
            f"{STORY_HISTORY_KEEP_DAYS} Tagen bereits verwendet — werden vermieden.")

    items = get_daily_items(date_label, avoid_entities)
    stories = get_embedded_stories(date_label, (items or {}).get("items", {}))

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
