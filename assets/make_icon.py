"""트레이 아이콘과 같은 모양(노란 원 + L)의 icon.ico 생성."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.ellipse((16, 16, S - 16, S - 16), fill="#ffd54a", outline="#222222", width=16)
try:
    font = ImageFont.truetype("malgunbd.ttf", 150)
except OSError:
    font = ImageFont.load_default()
d.text((S / 2, S / 2 + 4), "L", fill="#222222", font=font, anchor="mm")
out = Path(__file__).with_name("icon.ico")
img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(out)
