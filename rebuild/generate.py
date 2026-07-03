#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate.py — Tagesaktualisierung für schlusslicht.de
================================================================================
Wird vom GitHub-Actions-Workflow .github/workflows/daily-update.yml gestartet.

Ablauf:
  1. Liest die Vorlage  index.template.html  (Fallback: index.html).
  2. Recherchiert per OpenRouter-API mit Web-Search-Server-Tool
       a) tagesaktuelle Meldungen für alle 24 Rubriken,
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
from bs4 import BeautifulSoup

# ── Konfiguration ────────────────────────────────────────────────────────────
API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "perplexity/sonar"  # beliebiges OpenRouter-Modell hier eintragen
TEMPLATE = "index.template.html"
OUTPUT = "index.html"
TIMEOUT = 240

WOCHENTAGE = [
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
]
MONATE = [
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
]

# Die 24 Rubriken der Seite
RUBRIKEN = {
    "01": "Sport / MLS — schlechtestes Team im Tabellenende",
    "02": "Sport / Basketball — WNBA o. a. Liga, schlechtestes Team",
    "03": "Raumfahrt & Astronomie — Missionsscheitern, Budgetkürzung, Panne",
    "04": "Forschungsbudget — Land mit niedrigstem F&E-Etat, aktueller Bericht",
    "05": "Artensterben — bedrohte/ausgestorbene Art, neues Gutachten",
    "06": "Kleinstparteien — Wahlergebnis Deutschland, Land/Kommune",
    "07": "Niedriglohn — Branche, Tarifabschluss, Studie",
    "08": "Bahn & ÖPNV — Pünktlichkeit, Ausfall, Streik, Investitionsstau",
    "09": "Pressefreiheit — RSF-Bericht, inhaftierter Journalist, Einschränkung",
    "10": "Korruption — CPI, verhafteter Politiker, Korruptionsskandal",
    "11": "Klimaschutz — verfehltes Ziel, Emissionsrekord, CCPI",
    "12": "Steuervermeidung — Konzern, Steuerlücke, EU-Strafe",
    "13": "Glück & Zufriedenheit — unglücklichstes Land, Studie, Report",
    "14": "Bildung — PISA, Bildungslücke, Vergleichsstudie, Ranking",
    "15": "Lebenserwartung — aktuellster WHO-Bericht, Ländervergleich",
    "16": "Armut & BIP — ärmstes Land, Weltbank-Bericht",
    "17": "Internet — langsamstes Land, gesperrtes Netz, Zensur",
    "18": "Medien — Zeitungseinstellung, Auflagenrückgang, Entlassungen",
    "19": "App-Bewertungen — negative Reviews nach Skandal/Entscheidung",
    "20": "Kino-Flop — Box-Office-Katastrophe",
    "21": "Eurovision — Vorbereitung, Teilnehmer, Quotenflop",
    "22": "Film/Serie — aktuell schlechteste Rotten-Tomatoes-Bewertung",
    "23": "Börse & Insolvenz — Kurseinbruch, Insolvenz, Managementversagen",
    "24": "Sprachen & Kultur — Sprachtod, bedrohte Sprache, Statement",
}


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
    return sanitize(data)


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


# ── Recherche: 24 Rubriken ───────────────────────────────────────────────────
RUBRIK_BATCHES = [
    dict(list(RUBRIKEN.items())[i : i + 6]) for i in range(0, len(RUBRIKEN), 6)
]


def _fetch_items_batch(batch: dict, date_label: str, bereits_vergebene_themen: list):
    """Holt Meldungen für EINE kleine Gruppe von Rubriken (statt für alle 24
    auf einmal). Kleinere Aufgaben pro Aufruf verhindern, dass das Modell in
    eine Wiederholungsschleife rutscht und generische Platzhaltersätze statt
    echter Recherche liefert."""
    system = (
        f"Du bist Chefredakteur von schlusslicht.de, einem deutschen "
        f"linkssatirischen Magazin. Heute ist {date_label}.\n\n"
        "Finde zu JEDER der folgenden Rubriken eine ECHTE, tagesaktuelle oder "
        "höchstens 14 Tage alte Meldung via Websuche. Hat eine Rubrik heute "
        "keine eigene Meldung, wähle die überraschendste Schlusslicht-Meldung "
        "aus einem ANDEREN passenden Bereich — Aktualität geht vor Rubriktreue.\n\n"
        "ABSOLUTES VERBOT VON PLATZHALTERN: Jede Schlagzeile und jeder "
        "Kommentar muss eine ECHTE, konkrete, recherchierte Meldung mit "
        "echten Eigennamen, Orten und Zahlen sein. Schreibe NIEMALS "
        "generische Platzhaltersätze wie 'Land mit niedrigstem Etat: "
        "2026-Bericht' oder 'Team X: 2026-Ergebnis' — das ist kein "
        "Stilmittel, sondern ein Fehler. Wenn du keine echte Meldung findest, "
        "recherchiere weiter oder wähle ein anderes konkretes Thema, aber "
        "erfinde keine Schema-Lückentext-Sätze.\n\n"
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
        "leichte, erkennbare Haltung darf mitschwingen (Mitgefühl, "
        "Kopfschütteln, Fassungslosigkeit), aber immer auf Basis der Fakten "
        "— nie ins Unsachliche oder Übertriebene abgleiten. Fakten plus ein "
        "pointierter, menschlicher Satz, höchstens 130 Zeichen pro Kommentar. "
        "Antworte AUSSCHLIESSLICH auf Deutsch — keine chinesischen, "
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
        f'  "{list(batch.keys())[0]}": {{"rubrik_name": "Name falls Rubrik gewechselt wurde, sonst leer", '
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
    log("Recherchiere Tagesmeldungen für 24 Rubriken (in 4 Gruppen) …")

    all_items = {}
    vergebene_themen = []
    for i, batch in enumerate(RUBRIK_BATCHES, start=1):
        log(f"  Gruppe {i}/{len(RUBRIK_BATCHES)}: Rubriken {', '.join(batch.keys())}")
        batch_result = _fetch_items_batch(batch, date_label, vergebene_themen)
        if not batch_result:
            log(f"  Gruppe {i}: keine verwertbare Antwort erhalten, wird übersprungen.")
            continue
        for num, item in batch_result.items():
            if num in batch:
                all_items[num] = item
                thema = (item.get("thema") or "").strip()
                if thema:
                    vergebene_themen.append(thema)

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
    Teil des großen 24-Rubriken-Aufrufs), damit auch diese nicht unter einer
    überladenen Gesamtaufgabe leiden."""
    log("  Hole Spotlight und Ticker …")
    kontext = "; ".join(
        f"{num}: {it.get('headline', '')}" for num, it in items.items() if it.get("headline")
    )
    system = (
        f"Du bist Chefredakteur von schlusslicht.de. Heute ist {date_label}. "
        "Antworte AUSSCHLIESSLICH auf Deutsch, keine nicht-lateinischen "
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
        "sinnvoll sind. Antworte AUSSCHLIESSLICH auf Deutsch. Antworte NUR "
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
def get_daily_stories(date_label: str):
    log("Recherchiere 3 Hintergrundstorys …")

    system = (
        f"Du bist Hintergrundredakteur von schlusslicht.de. Heute ist {date_label}.\n\n"
        "Schreibe 3 tiefe Hintergrundstorys über aktuelle (max. 30 Tage alte) "
        "Schlusslichter aus verschiedenen Bereichen. Nutze die Websuche für echte "
        "Fälle. Stil: investigativ, aber menschlich — zeige erkennbares "
        "Mitgefühl mit den Betroffenen und eine klare, aber sachlich "
        "begründete Haltung zum Systemversagen. Keine kalte Distanz, aber "
        "auch keine Larmoyanz oder Übertreibung — jede Emotion muss sich aus "
        "den geschilderten Fakten ergeben, nicht aus Adjektiven allein. "
        "Zeige das Systemversagen hinter dem Einzelfall. 400-700 Wörter je Story. "
        "Antworte AUSSCHLIESSLICH auf Deutsch — keine chinesischen, "
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

    data = extract_json(call_api(system, prompt, max_tokens=6000))
    if data and data.get("stories"):
        log("  Verifiziere Quellen-URLs der Hintergrundstorys …")
        verifizierte_storys = []
        for story in data["stories"]:
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

    # ── 24 Rubrik-Meldungen ───────────────────────────────────────────────
    if items and items.get("items"):
        for num, it in items["items"].items():
            card = soup.select_one(f'[data-rubrik="{num}"]')
            if not card:
                continue
            kommentar = (it.get("kommentar") or "").strip()
            if kommentar:
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
            # Schlagzeile (Text nach „ — ")
            headline = (it.get("headline") or "").strip()
            rtit = card.select_one(".rtit")
            if rtit and headline and len(headline) > 4:
                cur = rtit.get_text()
                dash = cur.find(" — ")
                set_text(rtit, (cur[: dash + 3] + headline) if dash > 0 else headline)
            # Rubrikname nur wenn Rubrik gewechselt wurde
            rname = (it.get("rubrik_name") or "").strip()
            rnum = card.select_one(".rnum")
            if rnum and rname:
                cur = rnum.get_text()
                sep = cur.find(" ⸺ ")
                set_text(rnum, (cur[: sep + 3] + rname) if sep > 0 else cur)

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
            inner.clear()
            doppelt = list(items["ticker"]) + list(items["ticker"])
            for txt in doppelt:
                item = soup.new_tag("span", attrs={"class": "tk-item"})
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
        f"Stand: {build_time} — automatisch erstellt am {date_label}",
    )

    # ── SEO: Title, Description, Open Graph, Twitter Card ─────────────────
    # Täglich mit Spotlight-Inhalt befüllt, damit jede Ausgabe eine
    # eigene Link-Vorschau beim Teilen bekommt.
    if items and items.get("spotlight"):
        sp = items["spotlight"]
        hl = (sp.get("hl") or "").strip()
        txt = (sp.get("text") or "").strip()
        og_title = f"SCHLUSSLICHT — {hl}" if hl else "SCHLUSSLICHT — Das Magazin der Letzten"
        og_desc = txt[:155] if txt else "Das Magazin der Letzten. 24 Rubriken täglich aktuell."

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
        f"{WOCHENTAGE[today.weekday()]}, {today.day}. "
        f"{MONATE[today.month - 1]} {today.year}"
    )
    build_time = datetime.datetime.utcnow().strftime("%d.%m.%Y %H:%M UTC")
    log(f"Tagesausgabe: {date_label}")

    template_path = TEMPLATE if os.path.exists(TEMPLATE) else OUTPUT
    if not os.path.exists(template_path):
        log("FEHLER: Weder index.template.html noch index.html gefunden.")
        return 1
    log(f"Verwende Vorlage: {template_path}")
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
