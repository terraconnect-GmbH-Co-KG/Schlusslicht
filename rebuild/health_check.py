#!/usr/bin/env python3
"""
health_check.py — Erkennt anhaltenden, sonst unsichtbaren Ausfall der
echten Tagesgenerierung.

WARUM DIESES SKRIPT EXISTIERT:
Bisher galt ein Lauf schon dann als "Erfolg" (grüner Workflow), wenn
IRGENDETWAS geschrieben wurde — auch wenn das nur fallback_update.py war,
das lediglich das Datum aktualisiert, weil die echte KI-Generierung an
diesem Tag fehlgeschlagen ist (z.B. abgelaufener/ratenlimitierter API-Key,
Modell-Fehler). Für Besucher der Seite sah das täglich frisch aus (neues
Datum), tatsächlich blieben Schlagzeilen/Kolumnen/Essays aber tagelang
identisch — und niemand wurde benachrichtigt, weil der Workflow immer grün
blieb (dank durchgängigem `continue-on-error: true`).

Dieses Skript macht genau diesen Unterschied sichtbar: Es zählt pro Seite,
wie viele Tage IN FOLGE nur der Datums-Fallback lief (nicht die echte
KI-Generierung), committet diesen Zähler in health_status.json, und lässt
den Workflow-Job bewusst fehlschlagen (rote Anzeige + GitHubs Standard-
Benachrichtigung an Repo-Beobachter), sobald eine Seite mehr als
STALE_THRESHOLD_DAYS Tage in Folge nicht mehr echt aktualisiert wurde.
Zusätzlich verwaltet der Workflow darüber ein GitHub Issue für dauerhafte,
nicht wegklickbare Sichtbarkeit (siehe daily-update.yml).
"""
import datetime
import json
import os
import sys

HEALTH_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "health_status.json")
STALE_THRESHOLD_DAYS = 1  # bereits beim ERSTEN Ausfalltag alarmieren

FILES = [
    "index.html", "index.en.html",
    "brightside.html", "brightside.en.html",
    "insights.html", "insights.en.html",
    "nonconformist.html", "nonconformist.en.html",
]


def load_health() -> dict:
    if not os.path.exists(HEALTH_PATH):
        return {}
    try:
        with open(HEALTH_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def main() -> int:
    fallback_needed = set()
    marker_path = "/tmp/schlusslicht_fallback_needed.txt"
    if os.path.exists(marker_path):
        with open(marker_path, encoding="utf-8") as fh:
            fallback_needed = {line.strip() for line in fh if line.strip()}

    health = load_health()
    today = datetime.date.today().isoformat()
    unhealthy = []

    for f in FILES:
        entry = health.get(f, {"consecutive_stale_days": 0, "last_real_update": today})
        if f in fallback_needed:
            entry["consecutive_stale_days"] = int(entry.get("consecutive_stale_days", 0)) + 1
        else:
            entry["consecutive_stale_days"] = 0
            entry["last_real_update"] = today
        health[f] = entry
        if entry["consecutive_stale_days"] >= STALE_THRESHOLD_DAYS:
            unhealthy.append(
                f"{f}: seit {entry['consecutive_stale_days']} Tag(en) keine echte "
                f"KI-Generierung mehr, nur Datums-Fallback (zuletzt echt "
                f"aktualisiert: {entry.get('last_real_update', 'unbekannt')})"
            )

    with open(HEALTH_PATH, "w", encoding="utf-8") as fh:
        json.dump(health, fh, ensure_ascii=False, indent=2)

    with open("/tmp/schlusslicht_unhealthy.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(unhealthy))

    if unhealthy:
        print("::error::Mindestens eine Seite läuft nur noch auf Datums-Fallback "
              "statt echter KI-Generierung:")
        for u in unhealthy:
            print(f"::error::  {u}")
        return 1

    print("Gesundheitsprüfung OK: alle Seiten wurden heute echt (KI-generiert) aktualisiert.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
