"""
fallback_update.py — Fallback-Update wenn API-Key fehlt.

WICHTIG (Bugfix): Die verschiedenen Seiten haben UNTERSCHIEDLICHE Datums-
Elemente:
  - index.html / index.en.html:            <span class="meta" id="update-time">
  - brightside.html / brightside.en.html:   <span class="meta" id="spotStand">
  - nonconformist.html / .en.html:          <p ... id="ncf-stand">
  - insights.html / insights.en.html:       KEIN vergleichbares Element vorhanden

Die alte Version suchte IMMER nur nach id="update-time" — das passte nur
für index.html. Für brightside/nonconformist wurde dadurch nichts
gefunden und geändert, obwohl das Skript trotzdem "Fallback erfolgreich"
meldete (irreführende Erfolgsmeldung ohne tatsächliche Änderung). Für
insights.html gibt es ohnehin kein Datumsfeld — dort ist "nichts zu tun"
die ehrliche, korrekte Antwort, nicht eine vorgetäuschte Aktualisierung.

Diese Version prüft alle bekannten ID-Muster, aktualisiert JEDES gefundene,
und meldet nur dann Erfolg, wenn wirklich etwas geändert wurde.
"""
import os
import re
import datetime

WOCHENTAGE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
MONATE = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]


def fallback_update(output_file, lang="de"):
    """Aktualisiert JEDES bekannte Datums-Element, das in der Datei
    tatsächlich vorkommt. Gibt True nur zurück, wenn wirklich etwas
    geändert wurde — sonst False mit einer ehrlichen Meldung."""
    if not os.path.exists(output_file):
        print(f"X {output_file} nicht gefunden")
        return False

    try:
        with open(output_file, "r", encoding="utf-8") as f:
            html = f.read()
        original_html = html

        today = datetime.date.today()
        now = datetime.datetime.now(datetime.timezone.utc)

        if lang == "en":
            date_str = f"{WOCHENTAGE[today.weekday()]}, {MONATE[today.month - 1]} {today.day}, {today.year}"
            spot_str = f"As of: {today.strftime('%B %d, %Y')}"
            ncf_str = f"As of: {today.strftime('%B %d, %Y')} - automatically generated"
        else:
            tag_name = WOCHENTAGE[today.weekday()]
            monat_name = MONATE[today.month - 1]
            date_str = f"{tag_name}, {today.day}. {monat_name} {today.year}"
            spot_str = f"Stand: {today.day}. {monat_name} {today.year}"
            ncf_str = f"Stand: {today.day}. {monat_name} {today.year} - automatisch erstellt"

        changed_parts = []

        # index.html / index.en.html
        new_html, n = re.subn(
            r'(<span class="meta" id="update-time">).*?(</span>)',
            lambda m: (m.group(1) + f'Stand: {today.day}.{today.month}.{today.year} '
                       f'{now.strftime("%H:%M UTC")} - automatisch erstellt am '
                       f'{date_str}' + m.group(2)),
            html, count=1,
        )
        if n:
            html = new_html
            changed_parts.append("update-time (index)")

        # brightside.html / brightside.en.html
        new_html, n = re.subn(
            r'(<span class="meta" id="spotStand">).*?(</span>)',
            lambda m: m.group(1) + spot_str + m.group(2),
            html, count=1,
        )
        if n:
            html = new_html
            changed_parts.append("spotStand (brightside)")

        # nonconformist.html / .en.html
        new_html, n = re.subn(
            r'(id="ncf-stand"[^>]*>).*?(</p>)',
            lambda m: m.group(1) + ncf_str + m.group(2),
            html, count=1,
        )
        if n:
            html = new_html
            changed_parts.append("ncf-stand (nonconformist)")

        if html == original_html:
            print(f"- {output_file}: kein bekanntes Datums-Element gefunden "
                  f"(z.B. insights.html hat keins) - ehrlich NICHTS geaendert, "
                  f"keine vorgetaeuschte Erfolgsmeldung.")
            return False

        with open(output_file, "w", encoding="utf-8") as f:
            f.write(html)

        print(f"OK {output_file} - aktualisiert: {', '.join(changed_parts)} ({date_str})")
        return True
    except Exception as e:
        print(f"X Fehler bei {output_file}: {e}")
        return False


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Nutzung: python fallback_update.py <datei> [sprache]")
        sys.exit(1)

    lang = sys.argv[2] if len(sys.argv) > 2 else "de"
    fallback_update(sys.argv[1], lang)
    # Exit-Code immer 0 - auch bei "nichts zu tun" (kein echter Fehler, z.B.
    # bei insights.html). Nur echte Exceptions werden im Log sichtbar.
    sys.exit(0)
