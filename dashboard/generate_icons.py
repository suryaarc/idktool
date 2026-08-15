from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ACCENT = (107, 70, 193)
STATIC = Path(__file__).parent / "static"


def make_icon(size: int, path: Path) -> None:
    img = Image.new("RGB", (size, size), ACCENT)
    draw = ImageDraw.Draw(img)
    text = "G"
    font_size = int(size * 0.58)
    try:
        font = ImageFont.truetype("arialbd.ttf", font_size)
    except OSError:
        font = ImageFont.load_default(size=font_size)
    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - w) / 2 - bbox[0], (size - h) / 2 - bbox[1]), text, fill="white", font=font)
    img.save(path)


make_icon(192, STATIC / "icon-192.png")
make_icon(512, STATIC / "icon-512.png")
make_icon(180, STATIC / "apple-touch-icon.png")
print("icons written")
