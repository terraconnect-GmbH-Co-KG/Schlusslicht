#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_mfb.py — Tagesaktualisierung für insights.html
================================================================================
Wird vom GitHub-Actions-Workflow .github/workflows/daily-update.yml gestartet,
nach generate.py und generate_visionen.py.

WICHTIGES DESIGNPRINZIP (Sorgfaltspflicht bei Meinungsinhalten):
Die Zahlen in dieser Meinungsstrecke ("204 zu 1", "Sieben Prozent" usw.)
stammen NICHT aus einer neuen KI-Recherche, sondern werden deterministisch aus
den bereits verifizierten, festen Tabellen in index.template.html ausgelesen
(dieselben Daten, die auch auf der Startseite stehen). Die KI bekommt diese
Fakten als Vorgabe und schreibt NUR den Kommentartext dazu — sie recherchiert
und erfindet keine neuen Zahlen. Das minimiert Halluzinationsrisiko bei einer
Seite, die explizit als "pointiert und parteiisch, aber überprüfbar" beworben
wird.

Ablauf:
  1. Liest die 24 Rubrik-Tabellen aus index.template.html (feste Fakten).
  2. Liest die heutigen Schlagzeilen/Kommentare aus dem frisch gebauten
     index.html (aktueller Anlass des Tages je Rubrik).
  3. Wählt per Datum rotierend 5 von 24 Rubriken aus (volle Abdeckung alle
     ~5 Tage, deterministisch, kein Zufall).
  4. Lässt die KI für jede der 5 Rubriken einen Meinungskommentar auf Basis
     der vorgegebenen Fakten schreiben.
  5. Baut Text in insights.template.html ein, schreibt
     insights.html.
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
FACTS_SOURCE = "index.template.html"
TODAY_SOURCE = "index.html"
OUTPUT = "insights.en.html" if LANG == "en" else "insights.html"
TIMEOUT = 240
N_COLS = 5
N_RUBRIKEN = 24

MONATE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
          "August", "September", "Oktober", "November", "Dezember"]


def log(msg: str) -> None:
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


def call_api(system: str, prompt: str, max_tokens: int, retries: int = 3):
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
    return sanitize(data)


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


# ── Deterministische Fakten-Extraktion aus den Rubrik-Tabellen ───────────────
def extract_rubrik_facts(path: str) -> dict:
    """Liest alle 24 Rubrik-Tabellen aus und liefert strukturierte Fakten
    (Name, Tabellentitel, Zeilen, Fußzeile) — ohne jede KI-Beteiligung."""
    with open(path, encoding="utf-8") as fh:
        soup = BeautifulSoup(fh, "html.parser")

    facts = {}
    for art in soup.select("article.rub"):
        num = art.get("data-rubrik")
        if not num:
            continue
        rnum_el = art.select_one(".rnum")
        name = rnum_el.get_text(strip=True) if rnum_el else ""
        tbl = art.select_one(".tbl")
        rows, tbl_title, tbl_tag, foot = [], "", "", ""
        if tbl:
            head = tbl.select_one(".tbl-head")
            if head:
                tt = head.select_one(".tt")
                tg = head.select_one(".tag")
                tbl_title = tt.get_text(strip=True) if tt else ""
                tbl_tag = tg.get_text(strip=True) if tg else ""
            for row in tbl.select(".row"):
                nm = row.select_one(".nm")
                v = row.select_one(".v")
                if nm and v:
                    rows.append({
                        "name": nm.get_text(" ", strip=True),
                        "value": v.get_text(strip=True),
                    })
            foot_el = tbl.select_one(".tbl-foot")
            if foot_el:
                foot = foot_el.get_text(" ", strip=True)
        facts[num] = {
            "name": name,
            "table_title": tbl_title,
            "table_period": tbl_tag,
            "rows": rows,
            "foot": foot,
        }
    return facts


def extract_today_headlines(path: str) -> dict:
    """Liest die heutigen (bereits generierten) Schlagzeilen/Kommentare aus
    index.html — gibt dem Kommentar einen aktuellen Anlass."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        soup = BeautifulSoup(fh, "html.parser")
    out = {}
    for art in soup.select("article.rub"):
        num = art.get("data-rubrik")
        if not num:
            continue
        title = art.select_one(".rtit")
        quip = art.select_one(".realsatire")
        out[num] = {
            "headline": title.get_text(strip=True) if title else "",
            "quip": quip.get_text(strip=True) if quip else "",
        }
    return out


# Nur Rubriken mit klarem Politik-/Weltgeschehen-Bezug für die Meinungsstrecke.
# Ausgeschlossen: Sport (01,02), Raumfahrt (03), App-Bewertungen (19),
# Kino/Eurovision/Film (20,21,22) — passen thematisch nicht zu einer
# politischen Kolumne für ein Gen-X-/Boomer-Publikum.
POLITISCHE_RUBRIKEN = ["04", "05", "06", "07", "08", "09", "10", "11",
                       "12", "13", "14", "15", "16", "17", "18", "23", "24"]


def pick_rubriken(today: datetime.date) -> list:
    """Deterministische, rotierende Auswahl von 5 aus den politisch
    relevanten Rubriken — kein Zufall, volle Abdeckung alle paar Tage."""
    pool = POLITISCHE_RUBRIKEN
    n = len(pool)
    start = (today.toordinal() * N_COLS) % n
    return [pool[(start + i) % n] for i in range(N_COLS)]


# ── KI-Aufruf: nur Formulierung, keine neuen Zahlen ──────────────────────────
def get_commentary(facts_package: list, date_label: str):
    log("Erstelle Meinungskommentare zu vorgegebenen, festen Fakten …")

    system = (
        "Du bist Kolumnist der Meinungsstrecke 'more from behind' auf "
        "schlusslicht.de, einem deutschen linkssatirischen Magazin. "
        "Zielpublikum: belesene Erwachsene zwischen Mitte 40 und 70 (Generation "
        "X bis Babyboomer) — kein Jugend- oder Social-Media-Slang, keine "
        "Meme-Sprache, keine Anglizismen-Mischwörter (z. B. NIEMALS "
        "Konstruktionen wie 'irgendwas-treue' oder deutsch-englische "
        "Bastelwörter). Schreibe in klarem, druckreifem Deutsch, wie es in "
        "einem gedruckten Satiremagazin (Stil: Titanic, Eulenspiegel, "
        "klassische Feuilleton-Polemik) stehen könnte — nicht wie eine "
        "Boulevard-Schlagzeile oder ein Tweet.\n\n"
        "Du bekommst zu jeder Rubrik FESTE, bereits verifizierte Fakten "
        "(Zahlen, Quellen) vorgegeben. ABSOLUTE REGEL (nicht verhandelbar): "
        "Verwende AUSSCHLIESSLICH die dir gegebenen Zahlen und Fakten, "
        "wortwörtlich übernommen. Erfinde KEINE neuen Statistiken, Studien, "
        "Prozentsätze oder Vergleichszahlen — auch keine berechneten "
        "Verhältnisse, die nicht explizit vorgegeben sind. Wahrheitsgehalt "
        "geht immer vor Zuspitzung.\n\n"
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

    prompt = f"""Ausgabe vom {date_label}. Schreibe zu JEDER der folgenden 5 Rubriken
einen Meinungskommentar, basierend NUR auf den gegebenen Fakten:

{json.dumps(facts_package, ensure_ascii=False, indent=2)}

Liefere GENAU dieses JSON-Schema:
{{
  "columns": [
    {{
      "rubrik_num": "die Nummer aus der Vorgabe",
      "tag": "Standpunkt · Kurzthema",
      "title": "kreativer, prägnanter Titel (wie eine Schlagzeile, max 40 Zeichen)",
      "paragraphs": [
        {{"text": "Absatz 1 — NUR: Einstieg mit einer der vorgegebenen Zahlen, nüchtern dargestellt. Keine Bewertung.", "punch": false}},
        {{"text": "Absatz 2 — NUR: der zugespitzte Kernsatz/die Wertung dazu. Die Zahl aus Absatz 1 nicht wiederholen.", "punch": true}},
        {{"text": "Absatz 3 — NUR: zusätzlicher Kontext oder Gegenargument, das in Absatz 1+2 noch nicht vorkam.", "punch": false}},
        {{"text": "Absatz 4 — NUR: eine konkrete Schlussfolgerung/Forderung, die nirgends vorher stand.", "punch": false}}
      ],
      "bignum_text": "eine der vorgegebenen Zahlen, wortwörtlich, z.B. '204×' oder '~7 %'",
      "bignum_caption": "1 kurzer Satz, was die Zahl bedeutet",
      "stat_bullets": [
        {{"label": "Bezeichnung", "value": "Wert, wortwörtlich aus den Fakten"}}
        // 2-3 Einträge, alle wortwörtlich aus den vorgegebenen Fakten
      ]
    }}
    // für jede der 5 Rubriken ein Eintrag, in derselben Reihenfolge
  ]
}}"""

    raw = call_api(system, prompt, max_tokens=6000)
    data = extract_json(raw)
    if not data or "columns" not in data:
        log("  Keine verwertbaren Kommentar-Daten erhalten.")
        return None
    return data


# ── Validierung: Zahlen müssen wirklich aus den Fakten stammen ───────────────
def _numbers_in(text: str) -> set:
    return set(re.findall(r"\d+[.,]?\d*", text or ""))


def validate_column(col: dict, fact: dict) -> bool:
    """Grobe Sicherheitsnetz-Prüfung: alle Zahlen im bignum/Bullets müssen
    auch irgendwo in den vorgegebenen Fakten auftauchen."""
    allowed = _numbers_in(json.dumps(fact, ensure_ascii=False))
    for field in [col.get("bignum_text", "")] + [b.get("value", "") for b in col.get("stat_bullets", [])]:
        nums = _numbers_in(field)
        if nums and not nums.issubset(allowed):
            return False
    return True


# ── HTML-Injektion ────────────────────────────────────────────────────────────
def set_text(node, value):
    if node is not None and value is not None:
        node.clear()
        node.append(str(value))


def inject(html: str, columns: list, facts: dict) -> str:
    soup = BeautifulSoup(html, "html.parser")

    for i, col in enumerate(columns, start=1):
        rubrik_num = col.get("rubrik_num")
        fact = facts.get(rubrik_num, {})

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

        # Quelle: aus den deterministisch extrahierten Fakten, nicht von der KI
        src_text = f"Quelle: {fact.get('table_title', '')} · {fact.get('table_period', '')} (Rubrik {rubrik_num})"
        set_text(soup.select_one(f"#col{i}-src"), src_text)

    return str(soup)


# ── Hauptprogramm ─────────────────────────────────────────────────────────────
def main() -> int:
    if not API_KEY:
        log("FEHLER: Umgebungsvariable OPENROUTER_API_KEY fehlt.")
        return 1

    today = datetime.date.today()
    date_label = f"{today.day}. {MONATE[today.month - 1]} {today.year}"
    log(f"more_from_behind-Ausgabe: {date_label}")

    if not os.path.exists(FACTS_SOURCE):
        log(f"FEHLER: {FACTS_SOURCE} nicht gefunden.")
        return 1
    if not os.path.exists(TEMPLATE):
        log(f"FEHLER: {TEMPLATE} nicht gefunden.")
        return 1

    facts = extract_rubrik_facts(FACTS_SOURCE)
    today_headlines = extract_today_headlines(TODAY_SOURCE)
    selected = pick_rubriken(today)
    log(f"  Ausgewählte Rubriken heute: {', '.join(selected)}")

    facts_package = []
    for num in selected:
        f = dict(facts.get(num, {}))
        f["rubrik_num"] = num
        f["heutiger_anlass"] = today_headlines.get(num, {})
        facts_package.append(f)

    data = get_commentary(facts_package, date_label)
    if not data:
        log("Keine Inhalte erzeugt — insights.html bleibt unverändert.")
        return 0

    columns = [c for c in data.get("columns", [])[:N_COLS] if isinstance(c, dict)]
    for col in columns:
        col["paragraphs"] = dedupe_column_paragraphs(col.get("paragraphs"))

    # Sicherheitsnetz: Spalten mit nicht belegbaren Zahlen aussortieren
    # (Platz bleibt dann bei den alten Inhalten stehen, statt falsche Zahlen zu zeigen)
    checked = []
    for col in columns:
        fact = facts.get(col.get("rubrik_num"), {})
        if validate_column(col, fact):
            checked.append(col)
        else:
            log(f"  WARNUNG: Rubrik {col.get('rubrik_num')} enthält nicht belegbare Zahlen — übersprungen.")

    if not checked:
        log("Keine Spalte hat die Faktenprüfung bestanden — Datei bleibt unverändert.")
        return 0

    with open(TEMPLATE, encoding="utf-8") as fh:
        html = fh.read()

    html = inject(html, checked, facts)

    with open(OUTPUT, "w", encoding="utf-8") as fh:
        fh.write(html)
    log(f"{OUTPUT} geschrieben ({len(html):,} Zeichen), {len(checked)}/{N_COLS} Spalten aktualisiert.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
