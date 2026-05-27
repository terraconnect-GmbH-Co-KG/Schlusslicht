#!/usr/bin/env python3
"""
make_favicons.py — Generiert alle Favicon-Varianten für schlusslicht.de
Einmalig ausführen: pip install Pillow requests && python make_favicons.py

Ausgabe:
  favicon.ico                  (16 + 32 + 48 px, multi-size)
  favicon-16x16.png
  favicon-32x32.png
  favicon-96x96.png
  apple-touch-icon.png         (180x180, iOS)
  android-chrome-192x192.png   (PWA)
  android-chrome-512x512.png   (PWA Splash)
  site.webmanifest
"""

import json
import os

import requests
from PIL import Image, ImageDraw, ImageFont

# ── Farben ────────────────────────────────────────────────────────────────────
BG      = (9,   8,  10)
BG2     = (25,  20, 30)
RED     = (232,  0, 30)
INK     = (232, 224, 216)
MUTED   = (80,  72,  80)

# ── Font (aus Cache falls make_og_image.py schon lief) ───────────────────────
FONT_CACHE = os.path.join(os.path.dirname(__file__), ".font-cache")
os.makedirs(FONT_CACHE, exist_ok=True)

def get_bebas(size: int) -> ImageFont.FreeTypeFont:
    path = os.path.join(FONT_CACHE, "BebasNeue-Regular.ttf")
    if not os.path.exists(path):
        print("  Lade Bebas Neue …")
        css = requests.get(
            "https://fonts.googleapis.com/css?family=Bebas+Neue",
            headers={"User-Agent": "Mozilla/4.0 (compatible; MSIE 6.0)"},
            timeout=10,
        ).text
        import re
        match = re.search(r"src:\s*url\(([^)]+\.ttf)\)", css)
        if not match:
            raise RuntimeError("Keine TTF-URL für Bebas Neue gefunden.")
        r = requests.get(match.group(1), timeout=15)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default(size=size)


def make_icon(size: int) -> Image.Image:
    """Rendert ein quadratisches Icon in der gewünschten Größe."""
    img  = Image.new("RGBA", (size, size), (*BG, 255))
    draw = ImageDraw.Draw(img)

    # Roter Rahmen (1-2px je nach Größe)
    border = max(1, size // 32)
    draw.rectangle([(0, 0), (size - 1, size - 1)],
                   outline=RED, width=border)

    # Roter Akzent-Streifen oben
    strip = max(1, size // 20)
    draw.rectangle([(border, border), (size - border - 1, border + strip - 1)],
                   fill=RED)

    if size <= 24:
        # Sehr klein: einfaches "S" in Rot, zentriert
        font = get_bebas(int(size * 0.72))
        letter = "S"
        bb = draw.textbbox((0, 0), letter, font=font)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        x = (size - tw) // 2 - bb[0]
        y = (size - th) // 2 - bb[1] + strip
        draw.text((x, y), letter, font=font, fill=RED)

    else:
        # Größer: "S" grau (outline-Stil) + "L" rot, gestapelt
        pad  = max(2, size // 16)
        avail_h = size - 2 * pad - strip - border

        # Schriftgrößen: "S" und "L" füllen je ca. 50 % der Höhe
        fs = int(avail_h * 0.54)
        font = get_bebas(fs)

        # "S" — dunkelgrau (outline-Effekt durch Versatz)
        s_bb = draw.textbbox((0, 0), "S", font=font)
        s_w, s_h = s_bb[2] - s_bb[0], s_bb[3] - s_bb[1]
        sx = (size - s_w) // 2 - s_bb[0]
        sy = border + strip + pad - s_bb[1]

        for dx, dy in [(-1,0),(1,0),(0,-1),(0,1)]:
            draw.text((sx+dx, sy+dy), "S", font=font, fill=MUTED)
        draw.text((sx, sy), "S", font=font, fill=BG2)

        # "L" — kräftiges Rot, direkt darunter
        l_bb = draw.textbbox((0, 0), "L", font=font)
        l_w  = l_bb[2] - l_bb[0]
        lx = (size - l_w) // 2 - l_bb[0]
        ly = sy + s_h - int(fs * 0.08)   # leichtes Overlap
        draw.text((lx, ly), "L", font=font, fill=RED)

    return img


# ── Alle Größen generieren ────────────────────────────────────────────────────
SIZES = {
    "favicon-16x16.png":          16,
    "favicon-32x32.png":          32,
    "favicon-96x96.png":          96,
    "apple-touch-icon.png":      180,
    "android-chrome-192x192.png":192,
    "android-chrome-512x512.png":512,
}

print("Generiere Favicons …")
icons: dict[int, Image.Image] = {}
for filename, size in SIZES.items():
    img = make_icon(size)
    img.save(filename, "PNG", optimize=True)
    icons[size] = img
    print(f"  {filename} ({size}x{size})")

# favicon.ico — multi-size (16, 32, 48)
ico_48 = make_icon(48)
icons[16].save(
    "favicon.ico",
    format="ICO",
    sizes=[(16, 16), (32, 32), (48, 48)],
    append_images=[icons[32], ico_48],
)
print("  favicon.ico (16+32+48)")

# ── site.webmanifest ──────────────────────────────────────────────────────────
manifest = {
    "name": "SCHLUSSLICHT — Das Magazin der Letzten",
    "short_name": "Schlusslicht",
    "description": "24 Rubriken. Täglich aktuelle Schlusslichter. Echte Daten, trockener Kommentar.",
    "start_url": "/",
    "display": "standalone",
    "theme_color": "#09080a",
    "background_color": "#09080a",
    "icons": [
        {"src": "/android-chrome-192x192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/android-chrome-512x512.png", "sizes": "512x512", "type": "image/png"},
    ],
}
with open("site.webmanifest", "w", encoding="utf-8") as f:
    json.dump(manifest, f, ensure_ascii=False, indent=2)
print("  site.webmanifest")

print("Fertig.")
