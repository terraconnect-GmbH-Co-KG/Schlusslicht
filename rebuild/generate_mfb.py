#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_mfb.py — Tagesaktualisierung für insights.html
================================================================================
Wird vom GitHub-Actions-Workflow .github/workflows/daily-update.yml gestartet,
nach generate.py und generate_visionen.py.

WICHTIGES DESIGNPRINZIP (Sorgfaltspflicht bei Meinungsinhalten):
Diese Meinungsstrecke recherchiert ihre 3 Themen und Zahlen jeden Tag FRISCH
per Websuche (kein fester Themen-Pool, keine Rotation mehr) — genau wie
Brightside/Nonconformist auch. Die Absicherung gegen Halluzination läuft
deshalb nicht mehr über eine vorgegebene, fest verifizierte Zahlentabelle,
sondern über dieselbe technische Quellen-URL-Verifikation (HTTP-Check), die
auch die anderen drei Seiten verwenden: eine Zahl ohne echte, erreichbare
Quelle wird verworfen, nicht veröffentlicht.

Ablauf:
  1. Recherchiert 3 eigenständige politische/gesellschaftliche Themen samt
     Zahlen und Quelle (kein Bezug zur Startseite oder zu anderen Seiten).
  2. Verifiziert jede angegebene Quellen-URL technisch.
  3. Baut Text in insights.template.html ein, schreibt insights.html.
  4. Merkt sich die heutigen Themen in einer kleinen Historie-Datei, damit
     sich Themen nicht postwendend wiederholen (kein Pool/Rotation, nur ein
     Wiederholungs-Schutz).
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

API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "perplexity/sonar"
LANG = os.environ.get("SL_LANG", "de").strip().lower()
TEMPLATE = "insights.en.template.html" if LANG == "en" else "insights.template.html"
OUTPUT = "insights.en.html" if LANG == "en" else "insights.html"
HISTORY_PATH = os.path.join("data", "insights_theme_history.en.json" if LANG == "en" else "insights_theme_history.json")
TIMEOUT = 240
N_COLS = 3

MONATE = (
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"]
    if LANG == "en" else
    ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
     "August", "September", "Oktober", "November", "Dezember"]
)


# ── Themen-Historie (Wiederholungsschutz, KEIN Pool/Rotation) ───────────────
def load_recent_themes(path: str, max_items: int = 20) -> list:
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


def verify_url(url: str, timeout: int = 8) -> bool:
    """Prüft, ob eine Quellen-URL tatsächlich existiert und erreichbar ist —
    identische Absicherung wie in generate.py/generate_visionen.py."""
    if not url or not isinstance(url, str) or not url.strip().lower().startswith("http"):
        return False
    headers = {"User-Agent": "Mozilla/5.0 (compatible; SchlusslichtBot/1.0)"}
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True, headers=headers)
        if r.status_code >= 400:
            r = requests.get(url, timeout=timeout, allow_redirects=True, headers=headers, stream=True)
        return r.status_code < 400
    except requests.RequestException as exc:
        log(f"  Quellen-URL nicht erreichbar: {url} ({exc.__class__.__name__})")
        return False


def log(msg: str) -> None:
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


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


def _close_brackets(text: str) -> str:
    repaired = re.sub(r",\s*$", "", text.rstrip())
    if repaired.count('"') % 2 == 1:
        repaired += '"'
    open_brackets = repaired.count("[") - repaired.count("]")
    open_braces = repaired.count("{") - repaired.count("}")
    repaired += "]" * max(open_brackets, 0)
    repaired += "}" * max(open_braces, 0)
    return repaired


def _repair_truncated_json(text: str):
    """Versucht, eine abgeschnittene JSON-Antwort (Modellantwort wurde mitten
    im Objekt abgeschnitten) zu retten. Zwei Stufen:
      1) Nur offene Klammern/Anführungszeichen schließen (fängt Truncation
         direkt nach einem vollständigen Element ab).
      2) Falls das nicht reicht (z.B. mitten in einem Schlüssel/Wert
         abgeschnitten): schrittweise am letzten vollständigen Element-Ende
         (',' nach '}' oder ']') zurückschneiden und erneut schließen.
    Kein Allheilmittel, fängt aber den häufigsten Fall (Truncation durch
    max_tokens) ab, der sonst zu einem stillen Totalausfall führt."""
    candidate = _close_brackets(text)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    cut = text
    for _ in range(6):
        last_obj = cut.rfind("},")
        last_arr = cut.rfind("],")
        pos = max(last_obj, last_arr)
        if pos <= 0:
            break
        cut = cut[: pos + 1]  # bis inkl. schließender Klammer, Komma weg
        candidate = _close_brackets(cut)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise json.JSONDecodeError("Selbstreparatur ausgeschöpft", text, 0)


def extract_json(text):
    if not text:
        return None
    text = text.replace("```json", "").replace("```", "").strip()
    start, end = text.find("{"), text.rfind("}") + 1
    if start < 0 or end <= start:
        return None
    raw = text[start:end]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log(f"  JSON-Parsefehler: {exc} — versuche Selbstreparatur (Truncation?) …")
        try:
            data = _repair_truncated_json(raw)
            log("  Selbstreparatur erfolgreich — abgeschnittene Antwort gerettet.")
        except json.JSONDecodeError as exc2:
            log(f"  Selbstreparatur fehlgeschlagen: {exc2} — Antwort verworfen.")
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
def get_fresh_columns(date_label: str, recent_themes: list):
    log(f"Recherchiere {N_COLS} frische politische/gesellschaftliche Themen "
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
        "STIL (hier darfst und sollst du zuspitzen): pointiert, bissig, "
        "mit trockenem schwarzem Humor und klarer politischer Haltung für "
        "die Benachteiligten — Satire durch Sprachwitz, Ironie und "
        "überraschende Bilder, nicht durch Ausrufezeichen oder reißerische "
        "Effekthascherei. Kurze, klare Sätze wechseln mit einem gelegentlich "
        "längeren, kunstvoll gebauten Satz. Der 'punch'-Absatz soll die "
        "pointierteste, bissigste Formulierung der Kolumne enthalten. Der "
        "Titel darf originell und wortspielerisch sein, aber nicht "
        "reißerisch wie eine Boulevardzeile klingen. Antworte "
        "AUSSCHLIESSLICH auf " + ("Englisch (US)" if LANG == "en" else "Deutsch") + " — keine chinesischen, kyrillischen, "
        "arabischen oder anderen nicht-lateinischen Schriftzeichen, auch "
        "nicht einzelne Wörter oder Zeichen davon.\n\n"
        "SPRACHLICHE KLARHEIT: Jeder der 4 Absätze hat eine feste, eigene "
        "Aufgabe (siehe Schema unten) und darf NICHTS aus einem anderen "
        "Absatz wiederholen — auch nicht sinngemäß oder mit anderen Worten. "
        "Prüfe vor der Ausgabe jeden Absatz gegen die vorherigen: Steht der "
        "Gedanke schon da? Falls ja, streiche ihn. Antworte NUR mit einem "
        "validen JSON-Objekt, keine Erklärung davor oder danach."
    )

    themen_hinweis = (
        f"\n\nDiese Themen wurden in den letzten Tagen bereits verwendet — "
        f"wähle KEINES davon erneut: {', '.join(recent_themes)}."
        if recent_themes else ""
    )
    prompt = f"""Ausgabe vom {date_label}. Recherchiere und schreibe {N_COLS} eigenständige,
thematisch unterschiedliche politische/gesellschaftliche Meinungskolumnen.{themen_hinweis}

Liefere GENAU dieses JSON-Schema:
{{
  "columns": [
    {{
      "thema": "1-2 Wörter Themen-Schlagwort, für Wiederholungsschutz",
      "tag": "" + ("Standpoint · short topic" if LANG == "en" else "Standpunkt · Kurzthema") + "",
      "title": "kreativer, prägnanter Titel (wie eine Schlagzeile, max 40 Zeichen)",
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
    // genau {N_COLS} Einträge, thematisch unterschiedlich
  ]
}}"""

    raw = call_api(system, prompt, max_tokens=6000)
    data = extract_json(raw)
    if not data or "columns" not in data or not isinstance(data["columns"], list):
        log("  Keine verwertbaren Kommentar-Daten erhalten.")
        return None

    log("  Verifiziere Quellen-URLs technisch (HTTP-Check) …")
    verified = []
    for col in data["columns"][:N_COLS]:
        if not isinstance(col, dict):
            continue
        url = (col.get("source_url") or "").strip()
        if not verify_url(url):
            log(f"  Kolumne {col.get('title', '(ohne Titel)')!r}: Quellen-URL "
                f"fehlt oder nicht erreichbar ({url or 'keine URL angegeben'}) "
                f"— komplett verworfen, keine Halluzinationen ohne Beleg.")
            continue
        log(f"  Kolumne {col.get('title', '')!r}: Quelle verifiziert ({url})")
        verified.append(col)

    if not verified:
        log("  Keine Kolumne hat die Quellen-Verifikation bestanden.")
        return None
    return {"columns": verified}


def set_text(node, value):
    if node is not None and value is not None:
        node.clear()
        node.append(str(value))


# ── HTML-Injektion ────────────────────────────────────────────────────────────
def inject(html: str, columns: list) -> str:
    soup = BeautifulSoup(html, "html.parser")

    for i, col in enumerate(columns, start=1):
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


# ── Hauptprogramm ─────────────────────────────────────────────────────────────
def main() -> int:
    if not API_KEY:
        log("FEHLER: Umgebungsvariable OPENROUTER_API_KEY fehlt.")
        return 1

    today = datetime.date.today()
    date_label = (f"{MONATE[today.month - 1]} {today.day}, {today.year}"
                  if LANG == "en" else
                  f"{today.day}. {MONATE[today.month - 1]} {today.year}")
    log(f"more_from_behind-Ausgabe: {date_label}")

    if not os.path.exists(TEMPLATE):
        log(f"FEHLER: {TEMPLATE} nicht gefunden.")
        return 1

    recent_themes = load_recent_themes(HISTORY_PATH)
    log(f"Bereits kürzlich verwendete Themen ({len(recent_themes)}): "
        + (", ".join(recent_themes) if recent_themes else "keine"))

    data = get_fresh_columns(date_label, recent_themes)
    if not data:
        log("Keine Inhalte erzeugt — insights.html bleibt unverändert.")
        return 0

    columns = [c for c in data.get("columns", [])[:N_COLS] if isinstance(c, dict)]
    for col in columns:
        col["paragraphs"] = dedupe_column_paragraphs(col.get("paragraphs"))

    if not columns:
        log("Keine Spalte übrig nach Bereinigung — Datei bleibt unverändert.")
        return 0

    with open(TEMPLATE, encoding="utf-8") as fh:
        html = fh.read()

    html = inject(html, columns)

    new_themes = [c.get("thema", "").strip() for c in columns if c.get("thema")]
    if new_themes:
        save_recent_themes(HISTORY_PATH, recent_themes, new_themes)
        log(f"Themen-Historie aktualisiert: {', '.join(new_themes)}")

    with open(OUTPUT, "w", encoding="utf-8") as fh:
        fh.write(html)
    log(f"{OUTPUT} geschrieben ({len(html):,} Zeichen), {len(columns)}/{N_COLS} Spalten aktualisiert.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
