#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
remove_widget.py — Entfernt das "Heute ganz hinten"-Signature-Widget
VOLLSTÄNDIG aus einer HTML-Datei: das <aside class="upside ...">-Element
selbst, den vorangehenden Kommentar, die zugehörigen toten CSS-Regeln
(.upside, .upside-top, .lrow, .upside-foot, @keyframes pulse) und stellt
das Hero-Grid auf einspaltig um — unabhängig vom genauen Zeileninhalt
des Widgets (funktioniert also auch, wenn die Live-Datei andere Werte
zeigt als das Template).

WICHTIG: @keyframes rise und .rise bleiben erhalten, da diese auch für
andere Hero-Elemente (Überschrift, CTA-Buttons) verwendet werden.

Nutzung:
    python remove_widget.py index.html
    python remove_widget.py index.en.html
"""
import re
import sys


def remove_widget(path: str) -> bool:
    with open(path, encoding="utf-8") as fh:
        html = fh.read()

    changed = False

    # 1) Das <aside class="upside ...">...</aside>-Element samt
    #    vorangehendem Kommentar entfernen — unabhängig vom Inhalt.
    pattern_aside = re.compile(
        r'(<!--\s*SIGNATURE:[^>]*-->\s*)?'
        r'<aside\b[^>]*\bclass="upside[^"]*"[^>]*>.*?</aside>',
        re.DOTALL,
    )
    new_html, n = pattern_aside.subn("", html)
    if n > 0:
        html = new_html
        changed = True
        print(f"  {path}: {n}x <aside class=\"upside...\"> entfernt.")
    else:
        print(f"  {path}: kein <aside class=\"upside...\"> gefunden (evtl. bereits entfernt).")

    # 2) .hero-grid auf einspaltig umstellen (Widget war die 2. Spalte).
    new_html, n = re.subn(
        r'(\.hero-grid\s*\{\s*display:grid;\s*grid-template-columns:)[^;]+(;)',
        r'\g<1>1fr\g<2>',
        html,
        count=1,
    )
    if n > 0:
        html = new_html
        changed = True
        print(f"  {path}: .hero-grid auf einspaltig umgestellt.")

    # 3) Totes CSS für .upside/.lrow entfernen (NICHT .rise/@keyframes rise,
    #    die werden anderweitig verwendet). Block beginnt beim Kommentar
    #    "signature: the leaderboard..." und endet direkt vor
    #    "@keyframes rise".
    pattern_css = re.compile(
        r'/\*\s*signature:[^*]*\*/\s*'
        r'\.upside\s*\{.*?'
        r'@keyframes pulse[^}]*\}\s*',
        re.DOTALL,
    )
    new_html, n = pattern_css.subn("", html)
    if n > 0:
        html = new_html
        changed = True
        print(f"  {path}: totes CSS (.upside/.lrow/@keyframes pulse) entfernt.")

    if not changed:
        print(f"  {path}: keine Änderungen nötig.")
        return False

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Nutzung: python remove_widget.py <datei1.html> [datei2.html ...]")
        sys.exit(1)
    for p in sys.argv[1:]:
        remove_widget(p)
