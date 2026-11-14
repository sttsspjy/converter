"""Draws assets/icon.ico: four bars in the media-type colors, tallest to shortest."""
from pathlib import Path
from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / 'assets' / 'icon.ico'
S = 512
img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((16, 16, S - 16, S - 16), radius=112, fill=(27, 31, 39, 255))
colors = [(129, 145, 255), (228, 110, 174), (53, 195, 162), (227, 173, 75)]
heights = [300, 210, 140, 84]
bw, gap = 62, 26
x = (S - (4 * bw + 3 * gap)) // 2
base = 400
for c, h in zip(colors, heights):
    d.rounded_rectangle((x, base - h, x + bw, base), radius=18, fill=c + (255,))
    x += bw + gap
OUT.parent.mkdir(exist_ok=True)
img.save(OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(OUT)
