"""Draw the generic microphone app icon: python3 tools/make_icon.py AppIcon.png (needs Pillow)."""
import sys
from PIL import Image, ImageDraw, ImageFilter

S = 4                      # supersampling factor
N = 1024 * S
out = sys.argv[1]

def p(v):                  # 1024-grid units -> canvas pixels
    return int(round(v * S))

canvas = Image.new("RGBA", (N, N), (0, 0, 0, 0))

# Soft drop shadow under the tile
shadow = Image.new("RGBA", (N, N), (0, 0, 0, 0))
ImageDraw.Draw(shadow).rounded_rectangle([p(100), p(118), p(924), p(942)], radius=p(185), fill=(0, 0, 0, 90))
shadow = shadow.filter(ImageFilter.GaussianBlur(p(18)))
canvas.alpha_composite(shadow)

# Tile with a vertical gradient (indigo -> blue)
top, bottom = (88, 86, 214), (40, 132, 245)
grad = Image.new("RGBA", (N, N))
gd = ImageDraw.Draw(grad)
for y in range(N):
    t = y / (N - 1)
    gd.line([(0, y), (N, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,))
mask = Image.new("L", (N, N), 0)
ImageDraw.Draw(mask).rounded_rectangle([p(100), p(100), p(924), p(924)], radius=p(185), fill=255)
canvas.paste(grad, (0, 0), mask)

# Microphone glyph (white)
glyph = Image.new("RGBA", (N, N), (0, 0, 0, 0))
g = ImageDraw.Draw(glyph)
white = (255, 255, 255, 255)
stroke = p(46)
cx = 512
# capsule head
g.rounded_rectangle([p(cx - 100), p(228), p(cx + 100), p(566)], radius=p(100), fill=white)
# holder: U-shaped arc (stroke centred on arc_r) plus short side arms
arc_r = 170
arc_cy = 470
half = stroke // 2
g.arc([p(cx - arc_r) - half, p(arc_cy - arc_r) - half, p(cx + arc_r) + half, p(arc_cy + arc_r) + half],
      start=0, end=180, fill=white, width=stroke)
for x in (cx - arc_r, cx + arc_r):
    g.rectangle([p(x) - half, p(400), p(x) + half, p(arc_cy) + 1], fill=white)
    g.ellipse([p(x) - half, p(400) - half, p(x) + half, p(400) + half], fill=white)
# stem and base
g.rectangle([p(cx) - half, p(arc_cy + arc_r) - 1, p(cx) + half, p(768)], fill=white)
g.rounded_rectangle([p(cx - 126), p(746), p(cx + 126), p(792)], radius=p(23), fill=white)
# grille slits cut into the capsule
for y in (320, 384, 448):
    g.rounded_rectangle([p(cx - 56), p(y - 9), p(cx + 56), p(y + 9)], radius=p(9), fill=(255, 255, 255, 0))

# glyph shadow for depth
gshadow = glyph.split()[3].filter(ImageFilter.GaussianBlur(p(10)))
shade = Image.new("RGBA", (N, N), (20, 20, 80, 0))
shade.putalpha(gshadow.point(lambda a: int(a * 0.35)))
canvas.alpha_composite(shade, (0, p(8)))
canvas.alpha_composite(glyph)

canvas.resize((1024, 1024), Image.LANCZOS).save(out)
print("saved", out)
