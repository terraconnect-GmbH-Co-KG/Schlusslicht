#!/usr/bin/env python3
"""
make_og_image.py — Generiert og-image.png (1200×630) für schlusslicht.de
Einmalig ausführen: pip install Pillow requests && python make_og_image.py
"""

import io
import os
import requests
from PIL import Image, ImageDraw, ImageFont

# ── Farben (aus CSS-Variablen der Seite) ─────────────────────────────────────
BG       = (9,   8,  10)     # --bg
BG2      = (17,  14, 20)     # --bg2
RED      = (232,  0, 30)     # --red
RED_DIM  = (122,  0, 16)     # --red-dim
INK      = (232, 224, 216)   # --ink
INK_MID  = (176, 168, 152)   # --ink-mid
INK_MUTED= (106,  96, 104)   # --ink-muted
GOLD     = (201, 168,  76)   # --gold

W, H = 1200, 630

# ── Fonts von Google Fonts laden ─────────────────────────────────────────────
FONT_CACHE = os.path.join(os.path.dirname(__file__), ".font-cache")
os.makedirs(FONT_CACHE, exist_ok=True)

def fetch_gfont(family: str, filename: str) -> str:
    """Lädt eine TTF-Datei via Google Fonts CSS-API (Legacy-UA → TTF-Format)."""
    path = os.path.join(FONT_CACHE, filename)
    if not os.path.exists(path):
        print(f"  Lade Font: {filename} …")
        css = requests.get(
            f"https://fonts.googleapis.com/css?family={family.replace(' ', '+')}",
            headers={"User-Agent": "Mozilla/4.0 (compatible; MSIE 6.0)"},
            timeout=10,
        ).text
        import re as _re
        match = _re.search(r"src:\s*url\(([^)]+\.ttf)\)", css)
        if not match:
            raise RuntimeError(f"Keine TTF-URL für {family} gefunden.")
        url = match.group(1)
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
    return path

def system_font(names: list[str]) -> str:
    """Gibt den Pfad zum ersten gefundenen Windows-Systemfont zurück."""
    win_fonts = r"C:\Windows\Fonts"
    for name in names:
        p = os.path.join(win_fonts, name)
        if os.path.exists(p):
            return p
    raise FileNotFoundError(f"Keiner dieser Fonts gefunden: {names}")

BEBAS    = fetch_gfont("Bebas Neue", "BebasNeue-Regular.ttf")
IBM_MONO = system_font(["consola.ttf", "cour.ttf", "lucon.ttf"])  # Consolas → Courier → Lucida

# woff2 → Pillow braucht TTF/OTF; Fallback auf System-Font wenn nötig
def load_font(woff2_path: str, size: int) -> ImageFont.FreeTypeFont:
    """Versucht woff2 direkt (neuere Pillow-Versionen); fällt auf DejaVu zurück."""
    try:
        return ImageFont.truetype(woff2_path, size)
    except Exception:
        return ImageFont.load_default(size=size)

print("Lade Fonts …")
f_title  = load_font(BEBAS,    148)
f_stroke = load_font(BEBAS,    148)
f_sub    = load_font(BEBAS,     36)
f_mono   = load_font(IBM_MONO,  18)
f_mono_s = load_font(IBM_MONO,  15)

# ── Canvas ────────────────────────────────────────────────────────────────────
img  = Image.new("RGB", (W, H), BG)
draw = ImageDraw.Draw(img)

# Hintergrund-Textur: leichtes BG2-Rechteck rechts
draw.rectangle([(700, 0), (W, H)], fill=BG2)

# Vertikale Trennlinie
for i in range(H):
    alpha = int(80 * (1 - abs(i / H - 0.5) * 2))
    draw.point((700, i), fill=(232, 0, 30, alpha))
draw.line([(700, 0), (700, H)], fill=(*RED, 60), width=2)

# Roter Akzent-Streifen oben
draw.rectangle([(0, 0), (W, 4)], fill=RED)

# Roter Akzent-Streifen unten
draw.rectangle([(0, H - 4), (W, H)], fill=RED_DIM)

# ── Linke Seite: Titel ────────────────────────────────────────────────────────
PAD = 54

# Eyebrow
eyebrow = "// DAS MAGAZIN DER LETZTEN"
draw.text((PAD, 56), eyebrow, font=f_mono, fill=RED)

# "SCHLUSS" — outline/stroke style
title1 = "SCHLUSS"
x1, y1 = PAD, 100
# Stroke-Effekt: mehrfach leicht versetzt in INK_MUTED zeichnen
for dx, dy in [(-2,0),(2,0),(0,-2),(0,2)]:
    draw.text((x1+dx, y1+dy), title1, font=f_title, fill=INK_MUTED)
# Transparent-Effekt mit dunklem Kern → Outline
draw.text((x1, y1), title1, font=f_title, fill=(40, 36, 44))

# "LICHT" — rot, solid
title2 = "LICHT"
bbox1  = draw.textbbox((x1, y1), title1, font=f_title)
y2     = bbox1[3] - 12
draw.text((PAD, y2), title2, font=f_title, fill=RED)

# Tagline
tagline = "24 RUBRIKEN  ·  TÄGLICH AKTUELL  ·  SEIT 2025"
bbox2   = draw.textbbox((PAD, y2), title2, font=f_title)
draw.text((PAD, bbox2[3] + 18), tagline, font=f_mono, fill=INK_MUTED)

# URL
draw.text((PAD, H - 48), "schlusslicht.de", font=f_mono_s, fill=INK_MUTED)

# ── Rechte Seite: Badge ───────────────────────────────────────────────────────
CX = 950
CY = H // 2

# Badge-Box (leicht gedreht simulieren mit versetztem Schatten)
bw, bh = 340, 220
bx = CX - bw // 2
by = CY - bh // 2

# Schatten (roter Offset)
draw.rectangle([(bx + 7, by + 7), (bx + bw + 7, by + bh + 7)], fill=RED)
# Box
draw.rectangle([(bx, by), (bx + bw, by + bh)],
               fill=(9, 8, 10), outline=(*RED, 180), width=2)

# Badge-Inhalt
badge_top = "OFFIZIELLER RANG"
bt_bbox   = draw.textbbox((0, 0), badge_top, font=f_mono_s)
bt_w      = bt_bbox[2] - bt_bbox[0]
draw.text((CX - bt_w // 2, by + 22), badge_top, font=f_mono_s, fill=INK_MUTED)

rank      = "LETZTE(R)"
rk_font   = load_font(BEBAS, 72)
rk_bbox   = draw.textbbox((0, 0), rank, font=rk_font)
rk_w      = rk_bbox[2] - rk_bbox[0]
draw.text((CX - rk_w // 2, by + 52), rank, font=rk_font, fill=RED)

sub_      = "unter allen — und stolz darauf"
sb_bbox   = draw.textbbox((0, 0), sub_, font=f_mono_s)
sb_w      = sb_bbox[2] - sb_bbox[0]
draw.text((CX - sb_w // 2, by + 136), sub_, font=f_mono_s, fill=INK_MID)

# Trennlinie
draw.line([(bx + 20, by + 162), (bx + bw - 20, by + 162)],
          fill=(*INK_MUTED, 60), width=1)

award     = "★  SCHLUSSLICHT-AWARD  ★"
aw_bbox   = draw.textbbox((0, 0), award, font=f_mono_s)
aw_w      = aw_bbox[2] - aw_bbox[0]
draw.text((CX - aw_w // 2, by + 174), award, font=f_mono_s, fill=GOLD)

# ── Speichern ─────────────────────────────────────────────────────────────────
out = os.path.join(os.path.dirname(__file__), "og-image.png")
img.save(out, "PNG", optimize=True)
print(f"OK Gespeichert: {out}  ({W}x{H} px)")
