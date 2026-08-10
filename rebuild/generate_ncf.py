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

N_ESSAYS = 1  # ein einziger, langer Essay pro Tag

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


def _ist_meta_kommentar(text: str) -> bool:
    """Erkennt, ob ein Text den eigenen Rechercheprozess beschreibt ('ich
    habe kein Thema gefunden') statt einen echten Essay zu liefern — siehe
    generate.py für die volle Begründung dieses Bugfixes.

    WICHTIG (Bugfix, gefunden bei gründlicher Nachprüfung nach dem "viele
    Kategorien bleiben leer"-Fehler in generate.py): Dieser Filter fehlte in
    generate_ncf.py bisher KOMPLETT — anders als bei den anderen drei
    Generatoren (generate.py, generate_mfb.py, generate_visionen.py) gab es
    hier keinerlei Schutz gegen einen Essay, der in Wirklichkeit nur eine
    Erklärung des gescheiterten Rechercheprozesses ist."""
    if not text:
        return False
    marker = (
        "suchergebnis", "websuche", "newsindex", "keine verwertbare",
        "nicht belegbar", "nicht verifizierbar", "recherche liefert",
        "rechercheergebnis", "kein passendes thema", "kein geeignetes thema",
        "die datenlage ist", "zu dünn", "kein sauberer treffer",
        "no usable result", "no suitable topic", "insufficient search results",
    )
    t = text.lower()
    return any(m in t for m in marker)


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


_VERBOTENE_AUFRUF_MUSTER = re.compile(
    r"\b(boykott\w*|sabot\w*|blockier\w*|besetz\w*|verweigert die Steuer|"
    r"zerstör\w*|gewalt gegen|greift .{0,20} an|refuse to pay|"
    r"occupy the|smash|burn down)", re.IGNORECASE)


def _fetch_essays_batch(date_label: str, recent: list, count: int, zusatzhinweis: str = ""):
    """Rohe Fetch-Funktion (ohne Filter/Retry) — liefert bis zu `count`
    frisch verfasste Essay-Kandidaten. `recent` sind die kürzlich
    behandelten Themen+Kernthesen (siehe get_daily_essays)."""
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
        "ABSOLUTES VERBOT VON PROZESS-KOMMENTAREN: Findest du partout kein "
        "geeignetes Thema, wähle ein anderes Thema aus derselben "
        "Denkrichtung — schreibe NIEMALS einen Satz über die Suche/dein "
        "eigenes Zögern selbst als Titel oder Absatz. So ein Satz ist KEIN "
        "Essay, egal wie druckreif er klingt, und wird automatisch erkannt "
        "und verworfen.\n\n"
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
        f"Schreibe für die Ausgabe vom {date_label} GENAU EINEN einzigen, "
        f"LANGEN philosophisch-strukturkritischen Essay (Denkrichtungen z. B.: "
        f"{BEISPIEL_THEMEN} — oder ein anderes Thema derselben Richtung). Kein "
        f"kurzer Kommentar, sondern ein ausgearbeiteter Text von 9-12 Absätzen "
        f"à 2-5 Sätzen."
        + blickwinkel_block + zusatzhinweis +
        "\n\nDIESER EINE ESSAY IST DAS EINZIGE DES TAGES — nimm dir den Raum. "
        "Anforderungen an die Machart:\n"
        "- DOPPELBÖDIG: Der Text muss sich beim ZWEITEN Lesen anders lesen als "
        "beim ersten. Baue etwa in der Mitte eine Wendung ein, nach der die "
        "anfänglichen Sätze eine neue, schärfere Bedeutung bekommen. Der Titel "
        "soll dabei mitkippen (zweite Bedeutung), ohne ein Wortspiel-Klischee zu "
        "sein.\n"
        "- BISSIG: pointiert und unbequem, aber nie plump; Ironie ist erlaubt.\n"
        "- Genau ZWEI Absätze sind Zuspitzungen (\"punch\": true, je max. 2 "
        "Sätze, aphoristisch) — je einer vor und einer nach der Wendung.\n"
        "- Roter Faden statt Aufzählung; jeder Absatz baut auf dem vorigen auf.\n\n"
        "Liefere GENAU dieses JSON-Schema (das Array enthält genau EIN "
        "Essay-Objekt):\n"
        "{\n"
        '  "essays": [\n'
        "    {\n"
        '      "theme": "1-3 Wörter Themen-Schlagwort (nur intern, Wiederholungsschutz)",\n'
        '      "title": "prägnanter, doppelbödiger Titel — kein Doppelpunkt-Klischee",\n'
        '      "kernthese": "1 knapper Satz: welche Wendung/These wird HEUTE '
        'vertreten? (nur intern, wird nicht angezeigt)",\n'
        '      "paragraphs": [\n'
        '        {"text": "Absatz 1", "punch": false},\n'
        '        {"text": "weitere Absätze …", "punch": false},\n'
        '        {"text": "Zuspitzung vor der Wendung, max 2 Sätze", "punch": true},\n'
        '        {"text": "Wendung und weitere Absätze …", "punch": false},\n'
        '        {"text": "Zuspitzung nach der Wendung, max 2 Sätze", "punch": true},\n'
        '        {"text": "Schlussabsatz", "punch": false}\n'
        "      ],\n"
        '      "aside": "' + ("Lines of thought: " if LANG == "en" else "Denkrichtung: ")
        + '2-3 Denker/Konzepte, kommagetrennt"\n'
        "    }\n"
        "    // GENAU EIN Essay-Objekt, 9-12 Absätze, genau 2 davon mit punch:true\n"
        "  ]\n"
        "}"
    )
    data = call_api_json(system, prompt, max_tokens=8000)
    if not data or not isinstance(data.get("essays"), list):
        return None
    return data["essays"]


def get_daily_essays(date_label: str, history: list) -> dict:
    """Holt bis zu N_ESSAYS Essays, verteilt auf feste Slot-Nummern
    (1..N_ESSAYS), mit Retry NUR für tatsächlich noch fehlende Slots.

    WICHTIG (Bugfix, gefunden bei gründlicher Nachprüfung nach dem "viele
    Kategorien bleiben leer"-Fehler in generate.py): Ersetzt die alte
    Logik, die GENAU EINMAL alle N_ESSAYS Essays auf einmal anfragte und
    Ablehnungen (juristische Leitplanke, jetzt auch Prozess-Kommentare)
    einfach in Kauf nahm ('Rest behält Alt-Stand', ohne je einen zweiten
    Versuch zu unternehmen). Verhindert außerdem, dass ein Essay beim
    Verwerfen eines anderen in den falschen Slot rutscht (dieselbe
    Fehlerklasse wie in generate_mfb.py/generate_visionen.py gefunden und
    behoben) — jeder Essay behält seine Slot-Nummer über den gesamten
    Ablauf hinweg."""
    recent = [e for e in history if e.get("theme") and e.get("kernthese")]
    slots = {}
    letzter_fehlgrund = ""
    for versuch in range(4):
        fehlend = N_ESSAYS - len(slots)
        if fehlend <= 0:
            break
        log(f"Erzeuge {fehlend} Nonconformist-Essay(s) ({LANG})"
            f"{f' (Versuch {versuch + 1}, {fehlend} von {N_ESSAYS} fehlen noch)' if versuch else ''} …")

        extra_hinweis = ""
        if versuch > 0 and letzter_fehlgrund:
            extra_hinweis = (
                f"\n\nWICHTIG: Dein letzter Versuch hat nicht genug verwertbare "
                f"Essays geliefert ({letzter_fehlgrund}). Wähle diesmal andere "
                f"Themen aus derselben Denkrichtung."
            )

        kandidaten = _fetch_essays_batch(date_label, recent, fehlend, extra_hinweis) or []
        kandidaten = [e for e in kandidaten[:fehlend] if isinstance(e, dict) and e.get("title")
                      and isinstance(e.get("paragraphs"), list) and len(e["paragraphs"]) >= 3]

        offene_keys = [i for i in range(1, N_ESSAYS + 1) if i not in slots]
        neue = {}
        for idx, key in enumerate(offene_keys):
            if idx >= len(kandidaten):
                log(f"  Essay {key}: keine verwertbare Antwort erhalten — übersprungen.")
                continue
            e = kandidaten[idx]
            if e.get("theme"):
                e["_theme"] = e["theme"]
            titel = (e.get("title") or "").strip()
            gesamt = " ".join(p.get("text", "") for p in e["paragraphs"] if isinstance(p, dict))
            if _ist_meta_kommentar(titel) or _ist_meta_kommentar(gesamt):
                log(f"  Essay {key} ({titel!r}): Text beschreibt den eigenen "
                    f"Rechercheprozess statt ein echtes Thema — verworfen.")
                continue
            if _VERBOTENE_AUFRUF_MUSTER.search(gesamt):
                log(f"  Essay {key} ({titel!r}): verdächtige Aufruf-Formulierung "
                    f"— verworfen (juristische Leitplanke).")
                continue
            neue[key] = e

        if neue:
            slots.update(neue)

        if len(slots) >= N_ESSAYS:
            break

        letzter_fehlgrund = f"nur {len(neue)} von {fehlend} angeforderten Essays war(en) verwertbar"
        if versuch < 3:
            log(f"  Noch nicht vollständig ({len(slots)}/{N_ESSAYS}, {letzter_fehlgrund}) "
                f"— wiederhole für die fehlenden {N_ESSAYS - len(slots)} Plätze "
                f"(Versuch {versuch + 2}/4).")

    return slots


def get_essays(date_label: str, history: list):
    slots = get_daily_essays(date_label, history)
    if not slots:
        log("  Keine verwertbaren Essays erhalten.")
        return None

    slots = review_and_rewrite_essays(slots, date_label)

    if slots:
        today_iso = datetime.date.today().isoformat()
        neue_eintraege = []
        for e in slots.values():
            thema = e.get("_theme")
            kernthese = (e.get("kernthese") or "").strip()
            if thema and kernthese:
                neue_eintraege.append({"date": today_iso, "theme": thema, "kernthese": kernthese})
        if neue_eintraege:
            history.extend(neue_eintraege)
            save_essay_history(history)
            log(f"  Blickwinkel-Historie aktualisiert (+{len(neue_eintraege)} Einträge, "
                f"{ESSAY_HISTORY_KEEP_DAYS} Tage Wiederholungssperre je Thema).")

    return slots or None


def review_and_rewrite_essays(essays: dict, date_label: str) -> dict:
    """NEUER Zwischenschritt vor der Veröffentlichung: Prüft Sinnhaftigkeit
    der Essays (Grammatik, Klarheit, Kohärenz zwischen den Absätzen) und
    formuliert bei Bedarf sprachlich um — OHNE dabei neue Behauptungen,
    Namen oder Ereignisse hinzuzufügen (juristische Leitplanken bleiben
    unberührt, die 'verboten'-Prüfung lief bereits vorher).

    WICHTIG: essays ist ein dict {slot_nummer: essay} (siehe
    get_daily_essays), keine Liste mehr — ein verworfener Essay hinterlässt
    einen leeren Slot, statt dass die übrigen Essays in einer neu
    durchnummerierten Liste in den falschen Slot rutschen (dieselbe
    Fehlerklasse wie in generate_mfb.py/generate_visionen.py gefunden und
    behoben)."""
    if not essays:
        return essays

    pruefbar = {
        f"essay{i}": {"title": e["title"], "paragraphs": [p.get("text", "") for p in e["paragraphs"]
                                                       if isinstance(p, dict)]}
        for i, e in essays.items()
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
        "Antworte als JSON, mit genau denselben Schlüsseln wie oben (z.B. 'essay0'):\n"
        '{"essay0": {"ok": true}, "essay1": {"ok": true, "title_neu": "...", '
        '"paragraphs_neu": ["...", "...", "...", "..."]}, "essay2": {"ok": false, "grund": "..."}}'
    )
    urteil = call_api_json(system, prompt, max_tokens=5000) or {}

    ergebnis = {}
    for i, e in essays.items():
        bewertung = urteil.get(f"essay{i}", {})
        if bewertung.get("ok") is False:
            log(f"  Essay {e.get('title', '')!r} (Slot {i}): Sinnhaftigkeits-"
                f"Prüfung fehlgeschlagen ({bewertung.get('grund', 'kein Grund')}) "
                f"— verworfen, bestehender Stand für diesen Essay-Slot bleibt.")
            continue

        # WICHTIG (Bugfix, siehe generate.py review_and_fix_items für die
        # volle Begründung): die Umformulierung selbst muss ebenfalls gegen
        # _ist_meta_kommentar geprüft werden — sonst könnte der Reviewer bei
        # einem unsicheren Fall einen Prozess-Kommentar statt 'ok: false'
        # in title_neu/paragraphs_neu schreiben, unbemerkt vom Filter oben.
        title_neu = (bewertung.get("title_neu") or "").strip()
        if title_neu and not _ist_meta_kommentar(title_neu):
            log(f"  Essay {i}: Titel sprachlich überarbeitet.")
            e["title"] = title_neu
        elif title_neu:
            log(f"  Essay {i}: Umformulierung des Titels ist selbst ein "
                f"Prozess-Kommentar — verworfen, Original bleibt.")

        paras_neu = bewertung.get("paragraphs_neu")
        if isinstance(paras_neu, list) and len(paras_neu) == len(e["paragraphs"]):
            if _ist_meta_kommentar(" ".join(str(p) for p in paras_neu)):
                log(f"  Essay {i}: Umformulierung der Absätze ist selbst ein "
                    f"Prozess-Kommentar — verworfen, Original bleibt.")
            else:
                for p_obj, neuer_text in zip(e["paragraphs"], paras_neu):
                    if isinstance(p_obj, dict) and str(neuer_text).strip():
                        p_obj["text"] = str(neuer_text).strip()
                log(f"  Essay {i}: Absätze sprachlich überarbeitet.")
        ergebnis[i] = e

    return ergebnis


def inject(html: str, essays: dict, date_label: str) -> str:
    """WICHTIG (Bugfix, siehe get_daily_essays): essays ist ein dict
    {slot_nummer: essay}, keine Liste mehr — jeder Essay landet garantiert
    im Slot, für den er verfasst wurde, statt anhand seiner Position in
    einer nach Filterung verkürzten Liste (Slot-Verschiebung, dieselbe
    Fehlerklasse wie in generate_mfb.py/generate_visionen.py gefunden und
    behoben)."""
    soup = BeautifulSoup(html, "html.parser")
    for i, essay in essays.items():
        t = soup.select_one(f"#e{i}-title")
        if t is not None:
            t.string = essay["title"]
        body = soup.select_one(f"#e{i}-body")
        if body is not None:
            body.clear()
            for p in essay["paragraphs"][:14]:
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


def _hat_neue_struktur(html_text: str) -> bool:
    try:
        probe = BeautifulSoup(html_text, "html.parser")
    except Exception:
        return False
    return len(probe.select("section.essay")) == N_ESSAYS


def _carry_over_essays(template_html: str, output_html: str) -> str:
    """WICHTIG: modulweite Funktion (nicht mehr in main() verschachtelt),
    damit die Selbstheilung unten unabhängig testbar ist — dieselbe
    Überlegung wie bei carry_over_dynamic_content in generate.py."""
    try:
        neu = BeautifulSoup(template_html, "html.parser")
        alt = BeautifulSoup(output_html, "html.parser")
    except Exception as exc:
        log(f"  WARNUNG: Übernahme der Altdaten übersprungen (Parse-Fehler: {exc}).")
        return template_html
    for i in range(1, N_ESSAYS + 1):
        for sel in (f"#e{i}-title", f"#e{i}-aside"):
            src, dst = alt.select_one(sel), neu.select_one(sel)
            if src is not None and dst is not None:
                dst.string = src.get_text()
        body_sel = f"#e{i}-body"
        src, dst = alt.select_one(body_sel), neu.select_one(body_sel)
        if src is not None and dst is not None:
            dst.clear()
            for p in src.select("p"):
                dst.append(p.extract())

        # WICHTIG (Bugfix, gefunden bei gründlicher Nachprüfung nach dem
        # "viele Kategorien bleiben leer"-Fehler in generate.py): Ein
        # fehlgeschlagener Tag lässt einen Essay unverändert (siehe
        # inject()/get_daily_essays) — war der ÜBERNOMMENE Bestand selbst
        # schon ein Prozess-Kommentar (möglich, da dieser Filter hier bis
        # eben komplett fehlte, siehe _ist_meta_kommentar), würde er sonst
        # für immer identisch weiterkopiert. Nach dem Kopieren wird deshalb
        # geprüft, ob der jetzt in neu stehende Text schon kontaminiert
        # ist, und bei Bedarf auf einen ehrlichen Platzhalter zurückgesetzt.
        title_el = neu.select_one(f"#e{i}-title")
        body_el = neu.select_one(body_sel)
        titel_text = title_el.get_text() if title_el is not None else ""
        body_text = body_el.get_text() if body_el is not None else ""
        if _ist_meta_kommentar(titel_text) or _ist_meta_kommentar(body_text):
            log(f"  Essay {i}: bestehender Stand ist bereits ein "
                f"Prozess-Kommentar (vermutlich aus einem früheren "
                f"fehlerhaften Lauf) — wird NICHT weiter übernommen, "
                f"stattdessen auf ehrlichen Platzhalter zurückgesetzt.")
            placeholder = "Will be updated on the next run." if LANG == "en" else "Wird beim nächsten Lauf aktualisiert."
            if title_el is not None:
                title_el.string = placeholder
            if body_el is not None:
                body_el.clear()
                p = neu.new_tag("p")
                p.string = "…"
                body_el.append(p)
    return str(neu)


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

    # WICHTIG (Architektur-Fix, siehe generate.py für die volle Begründung):
    # Vorher wurde bei passender Struktur OUTPUT als GANZE Basis verwendet,
    # wodurch TEMPLATE dauerhaft nie mehr gelesen wurde — jede spätere
    # Korrektur an rein statischen Bereichen (Nav, Footer, CSS) kam dadurch
    # nie auf der echten Seite an. Jetzt: TEMPLATE ist immer die Basis, nur
    # die KI-generierten Essay-Inhalte werden bei Bedarf aus dem gestrigen
    # OUTPUT übernommen (siehe _hat_neue_struktur/_carry_over_essays, jetzt
    # modulweite Funktionen oben).
    with open(TEMPLATE, encoding="utf-8") as fh:
        html = fh.read()

    if os.path.exists(OUTPUT):
        bestehendes_html = open(OUTPUT, encoding="utf-8").read()
        if _hat_neue_struktur(bestehendes_html):
            log(f"Verwende Template als Basis, übernehme dynamische Inhalte aus {OUTPUT}.")
            html = _carry_over_essays(html, bestehendes_html)
        else:
            log(f"  {OUTPUT} hat noch die alte Struktur (vor dem Redesign) — "
                f"keine Altdaten übernommen, reines Template als Basis "
                f"(einmaliger Migrationsschritt).")
    else:
        log("Verwende Template als Basis (kein vorheriges OUTPUT vorhanden).")

    html = inject(html, essays, date_label)
    open(OUTPUT, "w", encoding="utf-8").write(html)
    log(f"{OUTPUT} geschrieben ({len(essays)} Essays).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
