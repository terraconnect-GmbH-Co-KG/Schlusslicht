#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate.py — Tagesaktualisierung für schlusslicht.de
================================================================================
Wird vom GitHub-Actions-Workflow .github/workflows/daily-update.yml gestartet.

Ablauf:
  1. Liest die Vorlage  index.template.html  (Fallback: index.html).
  2. Recherchiert per OpenRouter-API mit Web-Search-Server-Tool FRISCH, ohne
     festen Themen-Pool und ohne Rotation:
       a) 3 tagesaktuelle "Schlusslicht"-Meldungen aus beliebigen Bereichen,
          jede mit echter, verifizierter Quelle,
       b) je Meldung eine EINGEBETTETE Hintergrundstory zum selben Thema.
  3. Baut die Inhalte fest in das HTML ein (3 feste Slots im Template) und
     schreibt  index.html.
  4. Merkt sich die heutigen Themen in einer kleinen Historie-Datei, damit die
     KI an den Folgetagen nicht dieselben Themen wiederholt — das ist KEIN
     Pool/Rotationssystem, sondern nur ein Wiederholungs-Schutz.

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
from bs4 import BeautifulSoup

# ── Konfiguration ────────────────────────────────────────────────────────────
API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "perplexity/sonar"  # beliebiges OpenRouter-Modell hier eintragen
LANG = os.environ.get("SL_LANG", "de").strip().lower()
TEMPLATE = "index.en.template.html" if LANG == "en" else "index.template.html"
OUTPUT = "index.en.html" if LANG == "en" else "index.html"
HISTORY_PATH = os.path.join("data", "home_theme_history.en.json" if LANG == "en" else "home_theme_history.json")
N_ITEMS = 3
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


# ── Themen-Historie (Wiederholungsschutz, KEIN Pool/Rotation) ───────────────
def load_recent_themes(path: str, max_items: int = 20) -> list:
    """Liest die zuletzt verwendeten Themen-Schlagworte aus einer kleinen
    JSON-Datei. Dient nur dazu, der KI zu sagen 'das hattest du kürzlich
    schon' — es ist kein festes Themen-Set, die KI kann jederzeit ein
    komplett neues Thema wählen."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data[-max_items:] if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_recent_themes(path: str, existing: list, new_themes: list, max_items: int = 40) -> None:
    combined = [t for t in (existing + new_themes) if t][-max_items:]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(combined, fh, ensure_ascii=False, indent=2)


# ── Hilfsfunktionen ──────────────────────────────────────────────────────────
def log(msg: str) -> None:
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


def verify_url(url: str, timeout: int = 8) -> bool:
    """Prüft, ob eine Quellen-URL tatsächlich existiert und erreichbar ist
    (kein 404, keine DNS-Auflösung fehlgeschlagen, keine Zeitüberschreitung).
    Technische Absicherung gegen halluzinierte Quellen: Eine Meldung ohne
    nachweislich funktionierende URL wird NICHT veröffentlicht."""
    if not url or not isinstance(url, str) or not url.strip().lower().startswith("http"):
        return False
    headers = {"User-Agent": "Mozilla/5.0 (compatible; SchlusslichtBot/1.0)"}
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True, headers=headers)
        if r.status_code >= 400:
            # Manche Server lehnen HEAD ab -> mit GET nachprüfen, bevor wir aufgeben
            r = requests.get(url, timeout=timeout, allow_redirects=True, headers=headers, stream=True)
        return r.status_code < 400
    except requests.RequestException as exc:
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


# ── Recherche: 3 rotierende Rubriken ────────────────────────────────────────
def _fetch_fresh_items(date_label: str, recent_themes: list):
    """Recherchiert 3 frische 'Schlusslicht'-Meldungen aus BELIEBIGEN
    Bereichen (kein fester Themen-Pool, keine Rotation) — inkl. der
    kompletten Anzeige-Daten (Icon, Kategorie-Label, kleine Rangliste), die
    früher aus einer festen, von Hand gepflegten Tabelle kamen. Die KI muss
    jede Zahl mit einer echten, verifizierten Quelle belegen."""
    system = (
        f"Du bist Chefredakteur von schlusslicht.de, einem deutschen "
        f"linkssatirischen Magazin. Heute ist {date_label}.\n\n"
        f"Finde {N_ITEMS} ECHTE, tagesaktuelle oder höchstens 14 Tage alte "
        "'Schlusslicht'-Meldungen via Websuche — jeweils aus einem ANDEREN "
        "Bereich (z.B. Sport, Niedriglohn, Verkehr, Pressefreiheit, "
        "Korruption, Klimaschutz, Steuervermeidung, Medien, oder jeder "
        "andere Bereich, in dem jemand/etwas nachweislich 'Schlusslicht' "
        "bzw. Tabellenletzter ist). Die 3 Meldungen müssen sich thematisch "
        "klar unterscheiden.\n\n"
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
        "stammen, nicht erfunden sein.\n\n"
        + (
            f"Diese Themen wurden in den letzten Tagen bereits verwendet — "
            f"wähle KEINES davon erneut: {', '.join(recent_themes)}.\n\n"
            if recent_themes
            else ""
        )
        + "Stil: schwarze Satire mit menschlicher Wärme — nicht kalt-nüchtern, "
        "sondern erkennbar mit Empathie für die Betroffenen geschrieben. Eine "
        "leichte, erkennbare Haltung darf mitschwingen (Mitgefühl, "
        "Kopfschütteln, Fassungslosigkeit), aber immer auf Basis der Fakten "
        "— nie ins Unsachliche oder Übertriebene abgleiten. Fakten plus ein "
        "pointierter, menschlicher Satz, höchstens 130 Zeichen pro Kommentar. "
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
        '  "1": {\n'
        '    "thema": "1-2 Wörter Themen-Schlagwort, z.B. \'Fußball\' oder \'Steuerpolitik\'",\n'
        '    "kicker": "Kategorie-Label, z.B. \'Sport · MLS\' oder \'Pressefreiheit\'",\n'
        '    "icon": "ein passendes Emoji",\n'
        '    "headline": "kurze, konkrete Schlagzeile mit echten Namen/Zahlen",\n'
        '    "kommentar": "individueller Kommentar, max 130 Zeichen",\n'
        '    "table_title": "Kurztitel der Rangliste, z.B. \'MLS — Tabellenende\'",\n'
        '    "table_tag": "Zeitraum, z.B. \'Saison 2026\'",\n'
        '    "rows": [{"rank": "28", "name": "Team/Ort/Fall A", "value": "Zahl"}, {"rank": "29", "name": "Team/Ort/Fall B", "value": "Zahl"}, {"rank": "30", "name": "das eigentliche Schlusslicht", "value": "Zahl"}],\n'
        '    "foot": "1 Satz Einordnung/Vergleichswert für die Fußzeile",\n'
        '    "quelle": "Quellenname und Datum, z.B. Reuters 22.06.2026 — KEINE Zitationsnummern wie [1]",\n'
        '    "quelle_url": "die ECHTE, vollständige URL der Quelle (https://...) — PFLICHTFELD"\n'
        "  },\n"
        f'  ... insgesamt {N_ITEMS} Einträge mit den Schlüsseln "1".."{N_ITEMS}" ...\n'
        "}"
    )

    return extract_json(call_api(system, prompt, max_tokens=3000))


def get_daily_items(date_label: str, recent_themes: list):
    """Holt die 3 frei recherchierten Tagesmeldungen (kein Themen-Pool, keine
    Rotation — siehe _fetch_fresh_items)."""
    log(f"Recherchiere {N_ITEMS} frische Schlusslicht-Meldungen …")

    batch_result = _fetch_fresh_items(date_label, recent_themes) or {}
    all_items = {}
    for i in range(1, N_ITEMS + 1):
        key = str(i)
        item = batch_result.get(key)
        if isinstance(item, dict) and item.get("headline"):
            all_items[key] = item
        else:
            log(f"  Meldung {key}: leerer oder ungültiger Eintrag von der "
                f"KI geliefert — übersprungen, bestehender Stand bleibt.")

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
    Teil des großen 24-Rubriken-Aufrufs), damit auch diese nicht unter einer
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
        f"weiteren aktuellen Schlusslicht-Themen (unabhängig von den 24 "
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
        "WICHTIG: Du bewertest nur, du erfindest oder änderst keine Inhalte. "
        "Für jeden Eintrag gib zurück, ob er sinnvoll ist "
        '(\"ok\": true) oder nicht (\"ok\": false, mit kurzer "grund"-Angabe).\n\n'
        f"Einträge:\n{json.dumps(echte_items, ensure_ascii=False, indent=2)}\n\n"
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
def get_embedded_stories(date_label: str, items: dict):
    """Schreibt für JEDE heute ausgewählte Rubrik mit gültiger Meldung eine
    EINGEBETTETE Hintergrundstory zum selben Fall (statt einer unabhängig
    recherchierten Story zu einem beliebigen anderen Thema). Rubriken ohne
    gültige Tagesmeldung bekommen keine neue Story — die Vorlage behält für
    diesen Slot ihren bisherigen Platzhalterinhalt."""
    anchors = {num: it for num, it in (items or {}).items() if it and it.get("headline")}
    if not anchors:
        log("  Keine Rubrik-Meldungen vorhanden — keine Hintergrundstorys möglich.")
        return None

    log(f"Recherchiere {len(anchors)} eingebettete Hintergrundstorys (je Rubrik) …")

    system = (
        f"Du bist Hintergrundredakteur von schlusslicht.de. Heute ist {date_label}.\n\n"
        "Schreibe für JEDEN der folgenden Fälle eine tiefe Hintergrundstory ZUM "
        "SELBEN THEMA (nicht zu einem anderen Bereich). Nutze die Websuche, um "
        "über die kurze Tagesmeldung hinaus mehr zu recherchieren. Stil: "
        "investigativ, aber menschlich — zeige erkennbares Mitgefühl mit den "
        "Betroffenen und eine klare, aber sachlich begründete Haltung zum "
        "Systemversagen. Keine kalte Distanz, aber auch keine Larmoyanz oder "
        "Übertreibung — jede Emotion muss sich aus den geschilderten Fakten "
        "ergeben, nicht aus Adjektiven allein. Zeige das Systemversagen hinter "
        "dem Einzelfall. 400-700 Wörter je Story. "
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
        "Story MUSS auf dem echten, unten angegebenen Fall beruhen, "
        "UND du musst die tatsächliche, funktionierende URL einer Quelle "
        "angeben. Findest du keine echte, existierende URL, liefere für "
        "diese eine Rubrik GAR KEINE Story (lass den Schlüssel im JSON weg), "
        "statt etwas zu erfinden."
    )

    zeilen = "\n".join(
        f'{num}: Schlagzeile: "{it.get("headline", "")}" · Kommentar: '
        f'"{it.get("kommentar", "")}" · Quelle: {it.get("quelle", "")}'
        for num, it in anchors.items()
    )
    prompt = (
        f"Vertiefe JEDEN der folgenden {len(anchors)} Tagesfälle zu einer "
        f"eigenen Hintergrundstory. Nutze die Websuche mehrfach, um über die "
        f"kurze Schlagzeile hinaus Fakten, Vorgeschichte und Folgen zu finden.\n\n"
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
        "Antworte AUSSCHLIESSLICH mit gültigem JSON, ohne Markdown, mit genau "
        "den Rubrik-Nummern oben als Schlüssel:\n"
        "{\n"
        f'  "{list(anchors.keys())[0]}": {{\n'
        '    "cat": "// Kategorie · Zeitraum",\n'
        '    "title": "packender Titel, max 80 Zeichen",\n'
        '    "teaser": "Einleitung, 2-3 Sätze",\n'
        '    "body": ["<p>Absatz 1: nur das Ereignis</p>", "<p>Absatz 2: nur Hintergrund/Ursache</p>", "<p>Absatz 3: nur konkrete Auswirkung</p>", "<p>Absatz 4: nur die Einordnung als Systemversagen</p>"],\n'
        '    "factbox": ["Fakt 1", "Fakt 2", "Fakt 3"],\n'
        '    "conclusion": "Schlusssatz zum Systemversagen",\n'
        '    "source": "Quellenname und Datum, z.B. Spiegel 22.06.2026 — KEINE Zitationsnummern wie [1]",\n'
        '    "source_url": "die ECHTE, vollständige URL der Quelle (https://...) — PFLICHTFELD, ohne echte funktionierende URL keine Veröffentlichung"\n'
        "  },\n"
        f"  ... für jede der {len(anchors)} Rubriken ein Eintrag ...\n"
        "}"
    )

    data = extract_json(call_api(system, prompt, max_tokens=6000)) or {}
    stories = {}
    for num in anchors:
        story = data.get(num)
        if not isinstance(story, dict):
            log(f"  Rubrik {num}: keine verwertbare Story-Antwort erhalten — übersprungen.")
            continue
        url = (story.get("source_url") or "").strip()
        if not verify_url(url):
            log(f"  Rubrik {num}: Story-Quellen-URL fehlt oder nicht erreichbar "
                f"({url or 'keine URL angegeben'}) — Story verworfen, keine "
                f"Halluzinationen ohne Beleg.")
            continue
        log(f"  Rubrik {num}: Hintergrundstory-Quelle verifiziert ({url})")
        story["body"] = dedupe_paragraphs(story.get("body"))
        stories[num] = story

    log(f"  {len(stories)} von {len(anchors)} eingebetteten Hintergrundstorys verifiziert.")
    return stories or None


# ── Einbau ins HTML ──────────────────────────────────────────────────────────
def set_text(node, value):
    """Setzt reinen Text in ein BeautifulSoup-Element."""
    if node is not None and value:
        node.clear()
        node.append(str(value))


def set_html(node, html_value):
    """Setzt HTML-Inhalt in ein BeautifulSoup-Element."""
    if node is not None and html_value:
        node.clear()
        node.append(BeautifulSoup(str(html_value), "html.parser"))


def _build_table_html(item: dict) -> str:
    """Baut das .tbl-HTML-Fragment aus den frisch recherchierten Zeilen der
    KI (immer 3-spaltig: Rang/Name/Wert — einheitliches Format, damit die KI
    nicht auch noch unterschiedliche Spaltenzahlen gestalten muss)."""
    title = (item.get("table_title") or "").strip()
    tag = (item.get("table_tag") or "").strip()
    foot = (item.get("foot") or "").strip()
    rows = item.get("rows") or []

    rows_html = []
    for i, row in enumerate(rows[:3]):
        is_last = " is-last" if i == len(rows[:3]) - 1 else ""
        lamp = '<span class="lamp"></span>' if i == len(rows[:3]) - 1 else ""
        rank = (row.get("rank") or "—")
        name = (row.get("name") or "").strip()
        value = (row.get("value") or "").strip()
        rows_html.append(
            f'<div class="row c3{is_last}"><span class="rk">{rank}</span>'
            f'<span class="nm">{lamp}{name}</span><span class="v">{value}</span></div>'
        )

    return (
        f'<div class="tbl-head"><span class="tt">{title}</span><span class="tag">{tag}</span></div>'
        f'<div class="cols c3"><span>#</span><span class="l">{"Name" if LANG != "en" else "Name"}</span><span>{"Wert" if LANG != "en" else "Value"}</span></div>'
        + "".join(rows_html)
        + f'<div class="tbl-foot">{foot}</div>'
    )


def inject(html: str, items, stories, date_label: str, build_time: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    # ── Die 3 festen Slots im Template bekommen ihren kompletten Inhalt
    #    direkt aus der heutigen, frisch recherchierten KI-Meldung (kein
    #    Pool, keine Rotation, kein zweistufiges Fallback-Overwrite-Muster
    #    mehr). Fehlt eine Meldung (Recherche fehlgeschlagen), bleibt der
    #    Slot einfach beim zuletzt veröffentlichten Stand.
    for slot_i in range(1, N_ITEMS + 1):
        key = str(slot_i)
        it = (items or {}).get("items", {}).get(key)
        card = soup.select_one(f'article.rub[data-slot="{slot_i}"]')
        if card is None or not it:
            continue
        card["data-rubrik"] = key
        rub_no = card.select_one(".rub-no")
        if rub_no is not None:
            rub_no.string = str(slot_i)
        set_text(card.select_one(".rub-ico"), it.get("icon"))
        set_text(card.select_one(".rnum"), it.get("kicker"))
        headline = (it.get("headline") or "").strip()
        if headline:
            set_text(card.select_one(".rtit"), headline)
        kommentar = (it.get("kommentar") or "").strip()
        if kommentar:
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
        for num, st in stories.items():
            modal = soup.select_one(f"#story-slot{num}")
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
    if not API_KEY:
        log("FEHLER: Umgebungsvariable OPENROUTER_API_KEY fehlt.")
        return 1

    today = datetime.date.today()
    date_label = (
        (f"{WOCHENTAGE[today.weekday()]}, {MONATE[today.month - 1]} {today.day}, {today.year}"
         if LANG == "en" else
         f"{WOCHENTAGE[today.weekday()]}, {today.day}. {MONATE[today.month - 1]} {today.year}")
    )
    build_time = datetime.datetime.now(datetime.timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    log(f"Tagesausgabe: {date_label}")

    template_path = TEMPLATE if os.path.exists(TEMPLATE) else OUTPUT
    if not os.path.exists(template_path):
        log("FEHLER: Weder index.template.html noch index.html gefunden.")
        return 1
    log(f"Verwende Vorlage: {template_path}")
    with open(template_path, encoding="utf-8") as fh:
        html = fh.read()

    recent_themes = load_recent_themes(HISTORY_PATH)
    log(f"Bereits kürzlich verwendete Themen ({len(recent_themes)}): "
        + (", ".join(recent_themes) if recent_themes else "keine"))

    items = get_daily_items(date_label, recent_themes)
    stories = get_embedded_stories(date_label, (items or {}).get("items", {}))

    if not items and not stories:
        log("Keine Inhalte erzeugt — index.html bleibt unverändert.")
        return 0

    html = inject(html, items, stories, date_label, build_time)

    new_themes = [
        (it.get("thema") or "").strip()
        for it in (items or {}).get("items", {}).values()
        if it.get("thema")
    ]
    if new_themes:
        save_recent_themes(HISTORY_PATH, recent_themes, new_themes)
        log(f"Themen-Historie aktualisiert: {', '.join(new_themes)}")

    with open(OUTPUT, "w", encoding="utf-8") as fh:
        fh.write(html)
    log(f"{OUTPUT} geschrieben ({len(html):,} Zeichen). Fertig.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
