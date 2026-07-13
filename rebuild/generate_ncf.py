#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_ncf.py — Tägliche Aktualisierung der Nonconformist-Seite.

Erzeugt 5 philosophisch-linke Meinungsessays (DE oder EN via SL_LANG=en).
Enthält dieselben Schutzebenen wie die anderen Generatoren:
  - Vierstufige Sprach-Durchsetzung inkl. Sprach-Schranke im EN-Modus
  - Juristische Leitplanken im Prompt (keine Personen/Firmen, keine Aufrufe)
  - sanitize gegen Fremdschrift, Duplikat-Schutz, isinstance-Absicherung
  - Bei fehlgeschlagener Generierung bleibt der bestehende Stand erhalten
"""

import datetime
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
TEMPLATE = "nonconformist.en.template.html" if LANG == "en" else "nonconformist.template.html"
OUTPUT = "nonconformist.en.html" if LANG == "en" else "nonconformist.html"

MONATE = (
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"]
    if LANG == "en" else
    ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
     "August", "September", "Oktober", "November", "Dezember"]
)

N_ESSAYS = 5

# Themenpool: rein philosophisch/strukturell, rotiert nach Kalendertag
THEMENPOOL = [
    "Gehorsam und Autorität", "Eigentum und Macht", "Normalität als Konstruktion",
    "Wachstumskritik", "Utopie und Hoffnung", "Arbeit und Entfremdung",
    "Zeit und Beschleunigung", "Konsum und Bedürfnis", "Solidarität statt Konkurrenz",
    "Öffentliches Gut und Gemeineigentum", "Leistungsbegriff und Erbe",
    "Technik und Herrschaft", "Bildung als Anpassung oder Befreiung",
    "Angst als Herrschaftsinstrument", "Demokratie jenseits der Wahl",
    "Care-Arbeit und Unsichtbarkeit", "Fortschritt, der keiner ist",
    "Freiheit: von etwas oder zu etwas", "Der Wert des Nutzlosen",
    "Schweigen und Komplizenschaft",
]


def log(msg: str) -> None:
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


# ── Sprach-Schranke (identisch zu den anderen Generatoren) ───────────────────
_DE_STOPWORTE_GATE = {"der", "die", "das", "und", "nicht", "eine", "einen", "mit",
                      "für", "von", "wird", "sind", "auch", "sich", "wurde", "beim",
                      "über", "gegen", "wegen", "seit", "noch", "nur", "dass"}


def _wirkt_deutsch(obj) -> bool:
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


def sanitize(obj):
    """Entfernt nicht-lateinische Schriftzeichen aus allen Strings."""
    if isinstance(obj, str):
        return re.sub(r"[\u0400-\u04FF\u0590-\u05FF\u0600-\u06FF\u4E00-\u9FFF"
                      r"\u3040-\u30FF\uAC00-\uD7AF]+", "", obj)
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    return obj


def call_api(system: str, prompt: str, max_tokens: int, retries: int = 3):
    if LANG == "en":
        system = (
            "CRITICAL LANGUAGE RULE — HIGHEST PRIORITY: Write EVERY single output "
            "value (titles, paragraphs, tags, labels, asides) in ENGLISH (US) ONLY. "
            "The instructions below are written in German, but your output must be "
            "entirely in English. NEVER output German words or sentences.\n\n" + system
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
    for versuch in range(1, retries + 1):
        try:
            r = requests.post(API_URL, headers=headers, json=body, timeout=180)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            log(f"  API-Fehler (Versuch {versuch}/{retries}): {exc}")
            if versuch < retries:
                time.sleep(8 * versuch)
    return None


def extract_json(raw):
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    txt = m.group(0)
    try:
        data = json.loads(txt)
    except json.JSONDecodeError:
        try:
            data = json.loads(re.sub(r",\s*([}\]])", r"\1", txt))
        except json.JSONDecodeError:
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
    weitere Versuche. Identisches Muster wie in generate.py/generate_mfb.py/
    generate_visionen.py — behebt dieselbe Fehlerklasse, die bei Insights
    zu tagelangen, stillschweigenden Totalausfällen führte. Hier besonders
    relevant, da get_essays() ALLE 5 Essays in einer einzigen, langen
    JSON-Antwort anfordert."""
    raw = call_api(system, prompt, max_tokens=max_tokens)
    data = extract_json(raw)
    attempt = 0
    while data is None and raw and attempt < repair_retries:
        attempt += 1
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        parse_error = "unbekannt"
        if m:
            try:
                json.loads(m.group(0))
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


def get_essays(date_label: str, themen: list):
    log(f"Erzeuge {N_ESSAYS} Nonconformist-Essays ({LANG}) …")
    system = (
        "Du bist Essayist der Seite 'Nonconformist' auf schlusslicht.de — einer "
        "ausdrücklich als Meinung gekennzeichneten, philosophischen Strecke. "
        "Haltung: radikal links, kapitalismuskritisch, herrschaftskritisch — "
        "aber intellektuell redlich, gewaltfrei und juristisch einwandfrei.\n\n"
        "JURISTISCHE LEITPLANKEN — ABSOLUT VERPFLICHTEND:\n"
        "- NIEMALS real existierende Personen, Unternehmen, Parteien oder "
        "Organisationen namentlich nennen oder erkennbar beschreiben.\n"
        "- NIEMALS Tatsachenbehauptungen über konkrete Akteure aufstellen — "
        "nur Struktur- und Systemkritik auf abstrakter Ebene.\n"
        "- NIEMALS zu Straftaten, Gewalt, Sachbeschädigung, Steuerverweigerung "
        "oder sonstigen rechtswidrigen Handlungen aufrufen, auch nicht indirekt. "
        "Der einzige zulässige Aufruf ist der zum Selberdenken und zu legalem, "
        "demokratischem Engagement.\n"
        "- KEINE Verschwörungserzählungen, keine Herabwürdigung von Gruppen.\n\n"
        "Stil: druckreif, pointiert, philosophisch fundiert (Bezüge auf Denker "
        "wie Arendt, Gramsci, Bloch, Fisher, Raworth sind erwünscht — als "
        "Denkrichtung, nicht als Zitat). Keine Phrasen, keine Wiederholungen. "
        "Antworte AUSSCHLIESSLICH auf " + ("Englisch (US)" if LANG == "en" else "Deutsch") +
        " — keine nicht-lateinischen Schriftzeichen. Antworte NUR mit einem "
        "einzigen validen JSON-Objekt, keine Erklärungen."
    )
    prompt = (
        f"Schreibe {N_ESSAYS} eigenständige philosophische Kurzessays für die "
        f"Ausgabe vom {date_label}.\n\n"
        f"Die Themen für heute (je Essay eines, in dieser Reihenfolge):\n"
        + "\n".join(f"{i+1}. {t}" for i, t in enumerate(themen)) +
        "\n\nJeder Essay: 4 Absätze à 2-4 Sätze. Genau EINER der Absätze "
        "(Position 2 oder 3) ist der Zuspitzungs-Absatz: maximal 2 Sätze, "
        "aphoristisch, merkbar.\n\n"
        "Liefere GENAU dieses JSON-Schema:\n"
        "{\n"
        '  "essays": [\n'
        "    {\n"
        '      "title": "prägnanter Essay-Titel, kein Doppelpunkt-Klischee",\n'
        '      "paragraphs": [\n'
        '        {"text": "Absatz 1", "punch": false},\n'
        '        {"text": "Zuspitzung, max 2 Sätze", "punch": true},\n'
        '        {"text": "Absatz 3", "punch": false},\n'
        '        {"text": "Absatz 4", "punch": false}\n'
        "      ],\n"
        '      "aside": "' + ("Lines of thought: " if LANG == "en" else "Denkrichtung: ")
        + '2-3 Denker/Konzepte, kommagetrennt"\n'
        "    }\n"
        f"    // genau {N_ESSAYS} Essays\n"
        "  ]\n"
        "}"
    )
    data = call_api_json(system, prompt, max_tokens=7000)
    if not data or not isinstance(data.get("essays"), list):
        log("  Keine verwertbaren Essays erhalten.")
        return None

    essays = [e for e in data["essays"] if isinstance(e, dict) and e.get("title")
              and isinstance(e.get("paragraphs"), list) and len(e["paragraphs"]) >= 3]

    # Juristische Nachkontrolle: verdächtige Aufruf-Formulierungen aussortieren
    verboten = re.compile(
        r"\b(boykott\w*|sabot\w*|blockier\w*|besetz\w*|verweigert die Steuer|"
        r"zerstör\w*|gewalt gegen|greift .{0,20} an|refuse to pay|"
        r"occupy the|smash|burn down)", re.IGNORECASE)
    geprueft = []
    for e in essays:
        gesamt = " ".join(p.get("text", "") for p in e["paragraphs"] if isinstance(p, dict))
        if verboten.search(gesamt):
            log(f"  Essay {e.get('title', '')!r}: verdächtige Aufruf-Formulierung "
                f"— verworfen (juristische Leitplanke).")
            continue
        geprueft.append(e)

    if len(geprueft) < N_ESSAYS:
        log(f"  Nur {len(geprueft)}/{N_ESSAYS} Essays bestanden — "
            f"vorhandene werden verwendet, Rest behält Alt-Stand.")
    return geprueft or None


def inject(html: str, essays: list, date_label: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for i, essay in enumerate(essays[:N_ESSAYS], start=1):
        t = soup.select_one(f"#e{i}-title")
        if t is not None:
            t.string = essay["title"]
        body = soup.select_one(f"#e{i}-body")
        if body is not None:
            body.clear()
            for p in essay["paragraphs"][:5]:
                if not isinstance(p, dict) or not p.get("text"):
                    continue
                tag = soup.new_tag("p")
                if p.get("punch"):
                    tag["class"] = "punch"
                tag.string = p["text"].strip()
                body.append(tag)
        aside = soup.select_one(f"#e{i}-aside")
        if aside is not None and essay.get("aside"):
            aside.string = essay["aside"].strip()
    stand = soup.select_one("#ncf-stand")
    if stand is not None:
        prefix = "As of: " if LANG == "en" else "Stand: "
        suffix = " · automatically generated" if LANG == "en" else " · automatisch erstellt"
        stand.string = prefix + date_label + suffix
    return str(soup)


def main() -> int:
    # Fehlt der API-Key, wird bewusst NICHTS geschrieben. Der Workflow
    # erkennt über 'git diff', dass diese Datei unverändert blieb, und
    # ruft danach das externe rebuild/fallback_update.py auf, um
    # wenigstens das Datum zu aktualisieren (siehe generate.py für die
    # ausführliche Begründung). Rückgabe 0 statt 1: ein fehlender Key ist
    # ein erwarteter, sauber behandelter Zustand, kein Fehlerfall.
    if not API_KEY:
        log("⚠️  OPENROUTER_API_KEY fehlt — überspringe echte Generierung. "
            "Der Workflow ruft im Anschluss automatisch das externe "
            "Fallback-Skript für die Datumsaktualisierung auf.")
        return 0
    if not os.path.exists(TEMPLATE):
        log(f"FEHLER: {TEMPLATE} nicht gefunden.")
        return 1

    today = datetime.date.today()
    date_label = (f"{MONATE[today.month - 1]} {today.day}, {today.year}"
                  if LANG == "en" else
                  f"{today.day}. {MONATE[today.month - 1]} {today.year}")
    log(f"Nonconformist-Ausgabe ({LANG}): {date_label}")

    # Themenrotation: 5 Themen je Tag, deterministisch
    start = today.toordinal() * 5
    themen = [THEMENPOOL[(start + i) % len(THEMENPOOL)] for i in range(N_ESSAYS)]
    log("Themen heute: " + " · ".join(themen))

    essays = get_essays(date_label, themen)
    if not essays:
        log("Keine Essays erzeugt — Seite bleibt unverändert (bestehender Stand).")
        return 0

    base_path = OUTPUT if os.path.exists(OUTPUT) else TEMPLATE
    log(f"Verwende als Basis: {base_path}")
    html = open(base_path, encoding="utf-8").read()
    html = inject(html, essays, date_label)
    open(OUTPUT, "w", encoding="utf-8").write(html)
    log(f"{OUTPUT} geschrieben ({len(essays)} Essays).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
