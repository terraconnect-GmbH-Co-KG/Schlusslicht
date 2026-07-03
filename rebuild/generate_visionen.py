#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_visionen.py — Tagesaktualisierung für visionen.html
================================================================================
Wird vom GitHub-Actions-Workflow .github/workflows/daily-update.yml gestartet,
direkt im Anschluss an generate.py.

Ablauf:
  1. Liest die Vorlage  visionen.template.html.
  2. Recherchiert per OpenRouter-API (perplexity/sonar, eingebaute Websuche)
     a) ein Spotlight ("Heute im Licht"),
     b) 7 kurze, belegte gute Nachrichten aus unterschiedlichen Bereichen,
     c) 3 Hintergrundgeschichten mit Fakten und Einordnung.
  3. Baut die Inhalte fest in das HTML ein und schreibt  visionen.html.

WICHTIG zur Sorgfaltspflicht: Diese Seite behandelt Gesundheits-/Wissenschafts-
themen. Der Prompt verlangt ausdrücklich echte, prüfbare Quellen (WHO, IEA,
IUCN, UN, Weltbank, Fachjournale, offizielle Statistikämter) mit echter URL.
Es findet KEINE redaktionelle Prüfung vor Veröffentlichung mehr statt
(bewusste Entscheidung, siehe Commit-Historie) — die Sorgfalt muss deshalb im
Prompt und in der Quellenpflicht stecken, nicht in einem manuellen Schritt.
"""

import datetime
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
MODEL = "perplexity/sonar"
TEMPLATE = "visionen.template.html"
OUTPUT = "visionen.html"
TIMEOUT = 240

WOCHENTAGE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONATE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
          "August", "September", "Oktober", "November", "Dezember"]


def log(msg: str) -> None:
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


def verify_url(url: str, timeout: int = 8) -> bool:
    """Prüft, ob eine Quellen-URL tatsächlich existiert und erreichbar ist.
    Technische Absicherung gegen halluzinierte Quellen — siehe generate.py."""
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


# ── Recherche ─────────────────────────────────────────────────────────────────
def get_visionen_content(date_label: str):
    log("Recherchiere positive, belegte Nachrichten für visionen.html …")

    system = (
        "Du bist Redakteur der Rubrik 'Visionen' auf schlusslicht.de — der einzigen "
        "Seite des Magazins, die ausschließlich konstruktive, positive Entwicklungen "
        "zeigt. HÖCHSTE PRIORITÄT: Jede einzelne Meldung MUSS auf einer echten, "
        "existierenden, mit Websuche verifizierten Quelle beruhen (z. B. WHO, IEA, "
        "IUCN, UN, Weltbank/IMF, Fachjournale wie The Lancet/Nature, offizielle "
        "Statistikämter, Reuters/dpa für Fakten). Erfinde NIEMALS Zahlen, Studien, "
        "URLs oder Quellennamen — wenn du zu einem Thema keine echte, aktuelle, "
        "prüfbare Quelle findest, wähle ein anderes Thema, zu dem du eine hast. "
        "Jede Quelle braucht eine ECHTE, existierende URL. Ton: sachlich-warm, "
        "nüchtern, mit Zahlen belegt — keine Übertreibung, keine Effekthascherei. "
        "Wo eine gute Nachricht ein 'Aber' hat (z. B. Finanzierungslücke, "
        "Restrisiko), nenne es ehrlich, statt es wegzulassen. Antworte "
        "AUSSCHLIESSLICH auf Deutsch — keine chinesischen, kyrillischen, "
        "arabischen oder anderen nicht-lateinischen Schriftzeichen, auch "
        "nicht einzelne Wörter oder Zeichen davon. Wiederhole niemals "
        "denselben Fakt oder dieselbe Formulierung innerhalb einer Meldung "
        "oder über mehrere Meldungen hinweg — jeder Satz muss neue "
        "Information liefern. Antworte NUR mit "
        "einem einzigen validen JSON-Objekt, keine Erklärungen davor oder danach."
    )

    prompt = f"""Erstelle den Inhalt für die Visionen-Seite, Ausgabe {date_label}.

Liefere GENAU dieses JSON-Schema:

{{
  "stand_date": "{date_label}",
  "spotlight": {{
    "tag": "Bereich · Region (z. B. 'Gesundheit · weltweit')",
    "title": "Prägnante Überschrift der wichtigsten guten Nachricht der letzten Tage",
    "body_html": "1-2 Absätze als HTML-String, <strong> für Kernzahlen erlaubt, ehrliche Einordnung inkl. eventueller Einschränkungen",
    "source_name": "Name der Quelle",
    "source_url": "https://echte-existierende-url",
    "source_date": "Datum der Quelle, z. B. '8. Mai 2026'",
    "bignum": "kurze Kennzahl, z. B. '1 von 8' oder '+40%'",
    "bigcap": "1 Satz Erklärung der Kennzahl"
  }},
  "good_news": [
    {{
      "domain": "Themenbereich, z. B. 'Gesundheit'",
      "badge": "Region, z. B. 'Welt' oder 'Deutschland' oder 'Europa'",
      "icon": "ein passendes Emoji",
      "title": "Kurze, konkrete Überschrift",
      "body_html": "2-3 Sätze HTML-String mit Kernaussage und Zahl",
      "source_name": "Quellenname",
      "source_url": "https://echte-existierende-url",
      "source_date": "Datum, z. B. 'April 2026'"
    }}
    // genau 7 Einträge, aus möglichst unterschiedlichen Bereichen
    // (Gesundheit, Klima & Energie, Natur & Artenschutz, Gesellschaft,
    // Wissenschaft & Technik, Bildung o.ä.) — nicht alle aus demselben Bereich
  ],
  "stories": [
    {{
      "teaser_cat": "Bereich · Region",
      "teaser_title": "Kurztitel für die Vorschau-Kachel",
      "teaser_text": "1-2 Sätze Teaser",
      "modal_cat": "Bereich · Region · Jahr",
      "modal_title": "Ausführlicherer Titel für den Hintergrund",
      "lead": "1-2 Sätze Einstieg, warum das relevant ist",
      "intro_html": "1 Absatz HTML mit Kontext/Hintergrund",
      "facts": ["Fakt 1 mit Zahl", "Fakt 2 mit Zahl", "Fakt 3 mit Zahl"],
      "einordnung_html": "1 Absatz ehrliche Einordnung inkl. Grenzen/offener Fragen",
      "sources": [
        {{"name": "Quellenname", "url": "https://echte-url", "date": "Datum"}}
      ]
    }}
    // genau 3 Einträge, thematisch nach Möglichkeit unterschiedlich von den good_news
  ]
}}

Alle Themen müssen sich auf ECHTE, mit Websuche auffindbare aktuelle Entwicklungen
beziehen. Bevorzuge Meldungen der letzten Tage bis Wochen, wenn verfügbar."""

    raw = call_api(system, prompt, max_tokens=6000)
    data = extract_json(raw)
    if not data:
        log("  Keine verwertbaren Visionen-Inhalte erhalten.")
        return None
    return verify_visionen_sources(data)


def verify_visionen_sources(data: dict) -> dict:
    """Prüft technisch JEDE angegebene Quellen-URL (Spotlight, Good-News-
    Kacheln, Hintergrundstorys). Ohne nachweislich erreichbare URL wird der
    jeweilige Baustein komplett verworfen — keine Veröffentlichung ohne
    prüfbare Quelle."""
    log("  Verifiziere Quellen-URLs technisch (HTTP-Check) …")

    sp = data.get("spotlight")
    if sp:
        url = (sp.get("source_url") or "").strip()
        if not verify_url(url):
            log(f"  Spotlight: Quellen-URL fehlt oder nicht erreichbar "
                f"({url or 'keine URL angegeben'}) — Spotlight verworfen.")
            data["spotlight"] = None
        else:
            log(f"  Spotlight: Quelle verifiziert ({url})")

    verifizierte_news = []
    for item in data.get("good_news", []):
        url = (item.get("source_url") or "").strip()
        if not verify_url(url):
            log(f"  Meldung {item.get('title', '(ohne Titel)')!r}: Quellen-URL "
                f"fehlt oder nicht erreichbar ({url or 'keine URL angegeben'}) "
                f"— verworfen.")
            continue
        verifizierte_news.append(item)
    data["good_news"] = verifizierte_news

    verifizierte_storys = []
    for st in data.get("stories", []):
        quellen_ok = [
            s for s in (st.get("sources") or [])
            if verify_url((s.get("url") or "").strip())
        ]
        if not quellen_ok:
            log(f"  Story {st.get('teaser_title', '(ohne Titel)')!r}: keine "
                f"einzige erreichbare Quelle — komplett verworfen.")
            continue
        st["sources"] = quellen_ok
        verifizierte_storys.append(st)
    data["stories"] = verifizierte_storys

    log(f"  Ergebnis: Spotlight {'OK' if data.get('spotlight') else 'verworfen'}, "
        f"{len(data['good_news'])}/7 Meldungen, {len(data['stories'])}/3 Storys verifiziert.")
    return data


# ── HTML-Injektion ────────────────────────────────────────────────────────────
def set_text(node, value):
    if node is not None and value:
        node.clear()
        node.append(str(value))


def set_html(node, html_value):
    if node is not None and html_value:
        node.clear()
        node.append(BeautifulSoup(str(html_value), "html.parser"))


def make_source_html(name, url, date, prefix="Quelle"):
    name = (name or "").strip()
    url = (url or "").strip()
    date = (date or "").strip()
    if not name:
        return f"{prefix}: KI-recherchiert"
    if url:
        link = f'<a href="{url}" target="_blank" rel="noopener">{name}</a>'
    else:
        link = name
    return f"{prefix}: {link}" + (f" · {date}" if date else "")


def inject(html: str, data, date_label: str, build_time: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    # ── Spotlight ────────────────────────────────────────────────────────────
    sp = data.get("spotlight") or {}
    set_text(soup.select_one("#spot-tag"), sp.get("tag"))
    set_text(soup.select_one("#spot-title"), sp.get("title"))
    set_html(soup.select_one("#spot-text"), sp.get("body_html"))
    src_html = make_source_html(sp.get("source_name"), sp.get("source_url"), sp.get("source_date"))
    set_html(soup.select_one("#spot-src"), src_html)
    set_text(soup.select_one("#spot-bignum"), sp.get("bignum"))
    set_text(soup.select_one("#spot-bigcap"), sp.get("bigcap"))
    set_text(soup.select_one("#spotStand"), f"Stand: {date_label}")

    # ── Good-News-Grid ───────────────────────────────────────────────────────
    for i, item in enumerate(data.get("good_news", [])[:7], start=1):
        set_text(soup.select_one(f"#gn{i}-dom"), item.get("domain"))
        set_text(soup.select_one(f"#gn{i}-badge"), item.get("badge"))
        icon = soup.select_one(f"#gn{i}-icon")
        if icon is not None and item.get("icon"):
            icon.clear()
            icon.append(str(item["icon"]))
        set_text(soup.select_one(f"#gn{i}-title"), item.get("title"))
        set_html(soup.select_one(f"#gn{i}-text"), item.get("body_html"))
        set_html(
            soup.select_one(f"#gn{i}-src"),
            make_source_html(item.get("source_name"), item.get("source_url"), item.get("source_date")),
        )

    # ── Hintergrundgeschichten ───────────────────────────────────────────────
    for i, st in enumerate(data.get("stories", [])[:3], start=1):
        set_text(soup.select_one(f"#vs{i}-cat"), st.get("teaser_cat"))
        set_text(soup.select_one(f"#vs{i}-title"), st.get("teaser_title"))
        set_text(soup.select_one(f"#vs{i}-teaser"), st.get("teaser_text"))

        set_text(soup.select_one(f"#vs{i}-modal-cat"), st.get("modal_cat"))
        set_text(soup.select_one(f"#vs{i}-modal-title"), st.get("modal_title"))
        set_text(soup.select_one(f"#vs{i}-lead"), st.get("lead"))
        set_html(soup.select_one(f"#vs{i}-intro"), st.get("intro_html"))

        facts = st.get("facts") or []
        for j in range(1, 4):
            node = soup.select_one(f"#vs{i}-fact{j}")
            if node is None:
                continue
            if j <= len(facts):
                fact_val = facts[j - 1]
                set_html(node, fact_val if "<" in fact_val else f"<strong>{fact_val}</strong>")
            # falls weniger als 3 Fakten geliefert wurden, bleibt der alte Fakt stehen
            # (kein Löschen, um leere Kacheln zu vermeiden)

        set_html(soup.select_one(f"#vs{i}-einordnung"), st.get("einordnung_html"))

        sources = st.get("sources") or []
        if sources:
            parts = []
            for s in sources[:3]:
                name = (s.get("name") or "").strip()
                url = (s.get("url") or "").strip()
                date = (s.get("date") or "").strip()
                if not name:
                    continue
                link = f'<a href="{url}" target="_blank" rel="noopener">{name}</a>' if url else name
                parts.append(link + (f", {date}" if date else ""))
            if parts:
                set_html(soup.select_one(f"#vs{i}-modal-src"), "Quellen: " + " · ".join(parts))

    # ── Transparenz-Hinweis: ehrlich auf Vollautomatisierung umgestellt ──────
    note = soup.select_one("#transp-note")
    if note is not None:
        note.clear()
        note.append(BeautifulSoup(
            f"<b>Ehrlich gesagt:</b> Diese Seite wird vollautomatisch durch eine "
            f"KI-gestützte Recherche mit Websuche erstellt (Stand dieser Ausgabe: "
            f"{date_label}). Jede Meldung muss eine echte, verlinkte Quelle "
            f"(WHO, IEA, IUCN, UN, Weltbank, Fachjournale u. a.) nennen — eine "
            f"manuelle Redaktionsprüfung vor Veröffentlichung findet nicht mehr "
            f"statt. Fehler gefunden? Schreiben Sie an "
            f'<a href="mailto:hallo@schlusslicht.de" style="color:#ffe1b0;">hallo@schlusslicht.de</a> '
            f"– wir korrigieren transparent.",
            "html.parser",
        ))

    # ── SEO: Title, Description, OG, Twitter ─────────────────────────────────
    if sp.get("title"):
        og_title = f"Visionen — {sp['title']} | SCHLUSSLICHT"
        title_tag = soup.find("title")
        if title_tag:
            title_tag.string = og_title
        og_desc = (BeautifulSoup(sp.get("body_html") or "", "html.parser").get_text())[:155]
        for sel in ["#meta-description", "#og-title", "#og-description", "#twitter-title", "#twitter-description"]:
            el = soup.select_one(sel)
            if el is None:
                continue
            if "title" in sel:
                el["content"] = og_title
            else:
                el["content"] = og_desc or el.get("content", "")

    return str(soup)


# ── Hauptprogramm ─────────────────────────────────────────────────────────────
def main() -> int:
    if not API_KEY:
        log("FEHLER: Umgebungsvariable OPENROUTER_API_KEY fehlt.")
        return 1

    today = datetime.date.today()
    date_label = f"{today.day}. {MONATE[today.month - 1]} {today.year}"
    build_time = datetime.datetime.utcnow().strftime("%d.%m.%Y %H:%M UTC")
    log(f"Visionen-Ausgabe: {date_label}")

    template_path = TEMPLATE if os.path.exists(TEMPLATE) else OUTPUT
    if not os.path.exists(template_path):
        log("FEHLER: Weder visionen.template.html noch visionen.html gefunden.")
        return 1
    with open(template_path, encoding="utf-8") as fh:
        html = fh.read()

    data = get_visionen_content(date_label)
    if not data:
        log("Keine Inhalte erzeugt — visionen.html bleibt unverändert.")
        return 0

    html = inject(html, data, date_label, build_time)

    with open(OUTPUT, "w", encoding="utf-8") as fh:
        fh.write(html)
    log(f"{OUTPUT} geschrieben ({len(html):,} Zeichen). Fertig.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
