#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
reset_rubrik_placeholder.py — EINMALIGE Sofort-Korrektur für eine
aktuell live sichtbare, kategorie-fehlzugeordnete Rubrik-Karte (z. B. der
gemeldete Fall: Rubrik 06 "Klimaschutz-Index" zeigte tatsächlich einen
Korruptions-Text).

Setzt Schlagzeile UND Kommentar dieser EINEN Rubrik auf einen klar
erkennbaren, ehrlichen Platzhalter zurück ("wird beim nächsten Lauf
aktualisiert") — OHNE neue Inhalte zu erfinden. Die Rubrik-Bezeichnung
(z.B. "Klimaschutz-Index") bleibt unverändert korrekt stehen.

Der nächste erfolgreiche, jetzt kategorietreue Lauf ersetzt den
Platzhalter automatisch durch einen echten, thematisch passenden Fakt.

Nutzung:
    python reset_rubrik_placeholder.py index.html 06
    python reset_rubrik_placeholder.py index.en.html 06
"""
import re
import sys


def reset_placeholder(path: str, rubrik_num: str) -> bool:
    with open(path, encoding="utf-8") as fh:
        html = fh.read()

    # Finde die <article class="rub" data-rubrik="NN">...</article>-Karte
    pattern = re.compile(
        rf'(<article class="rub" data-rubrik="{re.escape(rubrik_num)}">.*?</article>)',
        re.DOTALL,
    )
    m = pattern.search(html)
    if not m:
        print(f"  {path}: Rubrik {rubrik_num} nicht gefunden.")
        return False

    card = m.group(1)

    # Schlagzeile (h3.rtit): Präfix vor " — " behalten, Rest ersetzen
    def fix_title(match):
        prefix = match.group(1)
        return f'{prefix} — Wird beim nächsten Lauf aktualisiert.</h3>'

    new_card = re.sub(
        r'(<h3 class="rub-title rtit">[^—<]*) — [^<]*</h3>',
        fix_title,
        card,
    )

    # Kommentar (p.rub-quip): komplett neutral ersetzen
    new_card = re.sub(
        r'(<p class="rub-quip realsatire">).*?(</p>)',
        r'\1„Diese Meldung wird beim nächsten Lauf aktualisiert.\2',
        new_card,
    )

    if new_card == card:
        print(f"  {path}: Rubrik {rubrik_num} — keine Änderung (Muster nicht gefunden, "
              f"bitte manuell prüfen).")
        return False

    html = html[: m.start()] + new_card + html[m.end():]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"  {path}: Rubrik {rubrik_num} auf neutralen Platzhalter zurückgesetzt.")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Nutzung: python reset_rubrik_placeholder.py <datei.html> <rubrik-nummer, z.B. 06>")
        sys.exit(1)
    reset_placeholder(sys.argv[1], sys.argv[2])
