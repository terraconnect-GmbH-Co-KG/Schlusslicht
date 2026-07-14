#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_ncf.py — Tägliche Aktualisierung der Nonconformist-Seite.

Erzeugt 3 philosophisch-linke Meinungsessays (DE oder EN via SL_LANG=en).
Die KI wählt die 3 Themen jeden Tag selbst frei (kein fester Themen-Pool,
keine Rotation) — die bestehende Blickwinkel-Historie (essay_history.json)
sorgt weiterhin dafür, dass ein wiederkehrendes Thema einen neuen Aspekt
bekommt statt denselben Gedanken nur umzuformulieren.
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

N_ESSAYS = 3

# Keine feste Themenliste mehr — die KI wählt jeden Tag frei 3 unterschied-
# liche philosophische/strukturelle Themen (kein Pool, keine Rotation).
# Beispielhafte Denkrichtungen für den Prompt (keine abschließende Liste):
BEISPIEL_THEMEN = (
    "Macht, Eigentum, Zeit, Arbeit, Freiheit, Technik, Demokratie, Wachstum, "
    "Solidarität, Angst als Herrschaftsinstrument, Normalität als Konstruktion, "
    "Konsum, Care-Arbeit, Schulden als Machtverhältnis, Meritokratie"
)


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


# ── Blickwinkel-Historie ─────────────────────────────────────────────────────
# WICHTIG: Verhindert, dass ein wiederkehrendes Thema (im Schnitt alle ~8
# Tage, siehe Rotationsformel in main()) einfach denselben Kerngedanken in
# anderen Worten wiederholt. Analog zum story_history.json-Mechanismus bei
# den Hintergrundstorys auf der Startseite.
ESSAY_HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   f"essay_history{'_en' if LANG == 'en' else ''}.json")
ESSAY_HISTORY_KEEP_DAYS = 120
ESSAY_HISTORY_MAX_PER_THEME = 4


def load_essay_history() -> list:
    if not os.path.exists(ESSAY_HISTORY_PATH):
        return []
    try:
        with open(ESSAY_HISTORY_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except Exception as exc:
        log(f"  Blickwinkel-Historie konnte nicht gelesen werden: {exc}")
        return []


def save_essay_history(history: list) -> None:
    cutoff = datetime.date.today() - datetime.timedelta(days=ESSAY_HISTORY_KEEP_DAYS)
    pruned = []
    for entry in history:
        try:
            d = datetime.date.fromisoformat(entry.get("date", ""))
        except (ValueError, TypeError, AttributeError):
            continue
        if d >= cutoff:
            pruned.append(entry)
    try:
        with open(ESSAY_HISTORY_PATH, "w", encoding="utf-8") as fh:
            json.dump(pruned, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        log(f"  Blickwinkel-Historie konnte nicht gespeichert werden: {exc}")


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


def get_essays(date_label: str, history: list):
    log(f"Erzeuge {N_ESSAYS} Nonconformist-Essays ({LANG}) …")

    # Statt Kernthesen NUR für vorab feststehende Themen nachzuschlagen
    # (ging vorher, weil die Themen schon feststanden), geben wir der KI
    # jetzt eine Zusammenfassung ALLER kürzlich behandelten Themen+Kernthesen
    # mit — sie wählt ihre 3 Themen selbst und muss dabei selbst prüfen, ob
    # eines davon kürzlich (mit welcher These) behandelt wurde.
    recent = [e for e in history if e.get("theme") and e.get("kernthese")]

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
        + (
            "\n\nABSOLUTE WIEDERHOLUNGSSPERRE — HÖCHSTE PRIORITÄT: Die unten "
            "aufgeführten Themen+Kernthesen wurden in den letzten " +
            str(ESSAY_HISTORY_KEEP_DAYS) + " Tagen bereits behandelt. Wählst "
            "du eines dieser Themen erneut, MUSST du einen GRUNDLEGEND "
            "ANDEREN Aspekt, ein anderes Argument oder eine andere "
            "Perspektive wählen — NICHT denselben Gedanken nur in anderen "
            "Worten wiederholen. Bevorzuge aber ohnehin ein Thema, das noch "
            "gar nicht in der Liste steht, wenn eines gut passt."
            if recent else ""
        )
    )
    blickwinkel_block = ""
    if recent:
        blickwinkel_block = "\n\nKÜRZLICH BEHANDELTE THEMEN+KERNTHESEN (nicht wiederholen!):\n"
        for e in recent[-30:]:
            blickwinkel_block += f"- {e['theme']}: {e['kernthese']}\n"

    prompt = (
        f"Wähle selbst {N_ESSAYS} eigenständige, thematisch klar unterschiedliche "
        f"philosophische/strukturkritische Themen (Beispiele: {BEISPIEL_THEMEN} — "
        f"oder ein anderes Thema aus derselben Denkrichtung) und schreibe zu "
        f"jedem einen eigenständigen philosophischen Kurzessay für die Ausgabe "
        f"vom {date_label}."
        + blickwinkel_block +
        "\n\nJeder Essay: 4 Absätze à 2-4 Sätze. Genau EINER der Absätze "
        "(Position 2 oder 3) ist der Zuspitzungs-Absatz: maximal 2 Sätze, "
        "aphoristisch, merkbar.\n\n"
        "Liefere GENAU dieses JSON-Schema:\n"
        "{\n"
        '  "essays": [\n'
        "    {\n"
        '      "theme": "1-3 Wörter Themen-Schlagwort, für Wiederholungsschutz",\n'
        '      "title": "prägnanter Essay-Titel, kein Doppelpunkt-Klischee",\n'
        '      "kernthese": "1 knapper Satz: welches Argument/welcher Blickwinkel wird '
        'HEUTE vertreten? (dient nur der internen Wiederholungs-Erkennung, wird nicht angezeigt)",\n'
        '      "paragraphs": [\n'
        '        {"text": "Absatz 1", "punch": false},\n'
        '        {"text": "Zuspitzung, max 2 Sätze", "punch": true},\n'
        '        {"text": "Absatz 3", "punch": false},\n'
        '        {"text": "Absatz 4", "punch": false}\n'
        "      ],\n"
        '      "aside": "' + ("Lines of thought: " if LANG == "en" else "Denkrichtung: ")
        + '2-3 Denker/Konzepte, kommagetrennt"\n'
        "    }\n"
        f"    // genau {N_ESSAYS} Essays, thematisch unterschiedlich\n"
        "  ]\n"
        "}"
    )
    data = call_api_json(system, prompt, max_tokens=7000)
    if not data or not isinstance(data.get("essays"), list):
        log("  Keine verwertbaren Essays erhalten.")
        return None

    essays = [e for e in data["essays"] if isinstance(e, dict) and e.get("title")
              and isinstance(e.get("paragraphs"), list) and len(e["paragraphs"]) >= 3]

    # Thema pro Essay kommt jetzt direkt aus der KI-Antwort selbst (kein
    # Pool/keine Rotation mehr, also keine positionsbasierte Zuordnung nötig).
    for e in data["essays"]:
        if isinstance(e, dict) and e.get("theme"):
            e["_theme"] = e["theme"]

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

    geprueft = review_and_rewrite_essays(geprueft, date_label)

    if geprueft:
        today_iso = datetime.date.today().isoformat()
        neue_eintraege = []
        for e in geprueft:
            thema = e.get("_theme")
            kernthese = (e.get("kernthese") or "").strip()
            if thema and kernthese:
                neue_eintraege.append({"date": today_iso, "theme": thema, "kernthese": kernthese})
        if neue_eintraege:
            history.extend(neue_eintraege)
            save_essay_history(history)
            log(f"  Blickwinkel-Historie aktualisiert (+{len(neue_eintraege)} Einträge, "
                f"{ESSAY_HISTORY_KEEP_DAYS} Tage Wiederholungssperre je Thema).")

    return geprueft or None


def review_and_rewrite_essays(essays: list, date_label: str) -> list:
    """NEUER Zwischenschritt vor der Veröffentlichung: Prüft Sinnhaftigkeit
    der Essays (Grammatik, Klarheit, Kohärenz zwischen den Absätzen) und
    formuliert bei Bedarf sprachlich um — OHNE dabei neue Behauptungen,
    Namen oder Ereignisse hinzuzufügen (juristische Leitplanken bleiben
    unberührt, die 'verboten'-Prüfung lief bereits vorher)."""
    if not essays:
        return essays

    pruefbar = {
        str(i): {"title": e["title"], "paragraphs": [p.get("text", "") for p in e["paragraphs"]
                                                       if isinstance(p, dict)]}
        for i, e in enumerate(essays)
    }

    log("  Prüfe Nonconformist-Essays auf Sinnhaftigkeit vor Veröffentlichung …")
    system = (
        "Du bist Chef vom Dienst bei schlusslicht.de (Rubrik 'Nonconformist') "
        "und prüfst Essays vor der Veröffentlichung. Du fügst NIEMALS neue "
        "Behauptungen, Namen oder Ereignisse hinzu — du darfst aber "
        "vorhandene, korrekte Formulierungen sprachlich verbessern "
        "(Grammatik, Klarheit, holprige Sätze, Redundanz), wenn das "
        "inhaltlich exakt dieselbe Aussage trifft wie vorher. Antworte "
        "AUSSCHLIESSLICH auf " + ("Englisch (US)" if LANG == "en" else "Deutsch") +
        ". Antworte NUR mit validem JSON, keine Erklärung."
    )
    prompt = (
        "Prüfe jeden Essay: Ist der Titel prägnant und vollständig (kein "
        "abgebrochenes Kunstwort)? Sind die Absätze klar formuliert, "
        "logisch aufeinander aufbauend, ohne Wiederholung?\n\n"
        "WENN INHALTLICH KORRUPT, ABER SCHLECHT FORMULIERT: gib 'ok': true "
        "UND 'title_neu'/'paragraphs_neu' (Liste, gleiche Reihenfolge/Länge) "
        "mit verbesserter Fassung zurück — DIESELBE Aussage, nur klarer. "
        "Lass die '_neu'-Felder weg, wenn der Text bereits gut ist.\n\n"
        "WENN UNRETTBAR UNSINNIG: gib 'ok': false mit kurzer 'grund'-Angabe zurück.\n\n"
        f"Essays:\n{json.dumps(pruefbar, ensure_ascii=False, indent=2)}\n\n"
        "Antworte als JSON, z.B.:\n"
        '{"0": {"ok": true}, "1": {"ok": true, "title_neu": "...", '
        '"paragraphs_neu": ["...", "...", "...", "..."]}, "2": {"ok": false, "grund": "..."}}'
    )
    urteil = call_api_json(system, prompt, max_tokens=4000) or {}

    ergebnis = []
    for i, e in enumerate(essays):
        bewertung = urteil.get(str(i), {})
        if bewertung.get("ok") is False:
            log(f"  Essay {e.get('title', '')!r}: Sinnhaftigkeits-Prüfung "
                f"fehlgeschlagen ({bewertung.get('grund', 'kein Grund')}) "
                f"— verworfen, bestehender Stand für diesen Essay-Slot bleibt.")
            continue

        title_neu = (bewertung.get("title_neu") or "").strip()
        if title_neu:
            log(f"  Essay {i}: Titel sprachlich überarbeitet.")
            e["title"] = title_neu

        paras_neu = bewertung.get("paragraphs_neu")
        if isinstance(paras_neu, list) and len(paras_neu) == len(e["paragraphs"]):
            for p_obj, neuer_text in zip(e["paragraphs"], paras_neu):
                if isinstance(p_obj, dict) and str(neuer_text).strip():
                    p_obj["text"] = str(neuer_text).strip()
            log(f"  Essay {i}: Absätze sprachlich überarbeitet.")
        ergebnis.append(e)

    return ergebnis


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

    history = load_essay_history()
    log(f"  {len({e.get('theme') for e in history if e.get('theme')})} unterschiedliche "
        f"Themen in der Blickwinkel-Historie der letzten {ESSAY_HISTORY_KEEP_DAYS} Tage.")
    essays = get_essays(date_label, history)
    if not essays:
        log("Keine Essays erzeugt — Seite bleibt unverändert (bestehender Stand).")
        return 0

    # Redesign-Migrations-Fix (siehe generate.py für die volle Begründung):
    # OUTPUT wird nur bevorzugt, wenn es bereits die neue 3-Essay-Struktur
    # hat — sonst bliebe ein altes nonconformist.html (5 Essays) für immer
    # die Basis und die neue Struktur würde nie übernommen.
    def _hat_neue_struktur(html_text: str) -> bool:
        try:
            probe = BeautifulSoup(html_text, "html.parser")
        except Exception:
            return False
        return len(probe.select("section.essay")) == N_ESSAYS

    base_path = TEMPLATE
    if os.path.exists(OUTPUT):
        bestehendes_html = open(OUTPUT, encoding="utf-8").read()
        if _hat_neue_struktur(bestehendes_html):
            base_path = OUTPUT
        else:
            log(f"  {OUTPUT} hat noch die alte Struktur (vor dem Redesign) — "
                f"verwende stattdessen {TEMPLATE} als Basis (einmaliger "
                f"Migrationsschritt).")
    log(f"Verwende als Basis: {base_path}")
    html = open(base_path, encoding="utf-8").read()
    html = inject(html, essays, date_label)
    open(OUTPUT, "w", encoding="utf-8").write(html)
    log(f"{OUTPUT} geschrieben ({len(essays)} Essays).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
