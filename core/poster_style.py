"""
Per-Fanspage poster identity: a color theme for the image prompt and a name
watermark stamped on the finished poster.

The watermark is drawn by the app, not requested from the image model: models
misspell small text and place it unpredictably, while this is exact every time
and costs nothing.
"""
import logging

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

DEFAULT_THEME = "parchment"

# key -> label shown in the UI, and the palette the image model must follow
THEMES = {
    "parchment": {
        "label": "Vintage Parchment — krem, cokelat sepia, emas",
        "palette": "aged cream parchment paper background, sepia-brown ink linework, muted gold "
                   "and rust-red accents, deep teal water",
        "swatch": ["#e9dcbc", "#6b4a2b", "#b8862b", "#2f6f6f"],
    },
    "midnight_gold": {
        "label": "Midnight Gold — biru malam, emas metalik",
        "palette": "deep midnight-navy and charcoal background, luminous metallic gold headings "
                   "and linework, ivory text, cool steel-blue water",
        "swatch": ["#14213d", "#2b2d33", "#d4af37", "#f5f0e1"],
    },
    "forest_copper": {
        "label": "Forest & Copper — hijau hutan, tembaga",
        "palette": "deep forest-green background panels, warm copper and bronze accents, cream "
                   "text, moss-green and slate-gray stone tones, clear blue-green water",
        "swatch": ["#1f3d2b", "#b87333", "#f3ead3", "#6b7f5a"],
    },
    "desert_sunset": {
        "label": "Desert Sunset — pasir, terakota, toska",
        "palette": "warm sand-beige background, terracotta and burnt-orange accents, turquoise "
                   "water, dark espresso-brown ink",
        "swatch": ["#e8d3a9", "#c4622d", "#2bb3b1", "#3b2418"],
    },
}


def normalize_theme(value) -> str:
    return value if value in THEMES else DEFAULT_THEME


def theme_prompt(theme: str | None) -> str:
    """Palette instruction appended to the image prompt. It is stated last and as
    overriding, because topic blueprints still describe parchment in places."""
    palette = THEMES[normalize_theme(theme)]["palette"]
    return (
        f" COLOR PALETTE (mandatory, overrides any other color or paper description above): "
        f"{palette}. Keep this palette consistent across the whole poster."
        " Leave the bottom-right corner free of text and important details."
    )


def apply_watermark(path, text: str | None) -> None:
    """Stamps `text` in a translucent pill in the bottom-right corner of the image at `path`."""
    label = (text or "").strip()[:60]
    if not label:
        return
    with Image.open(path) as source:
        image = source.convert("RGBA")
    width, height = image.size

    size = max(14, round(width * 0.028))
    font = ImageFont.load_default(size=size)
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    left, top, right, bottom = draw.textbbox((0, 0), label, font=font)
    pad, margin = round(size * 0.5), round(width * 0.025)

    x1, y1 = width - margin, height - margin
    x0, y0 = x1 - (right - left) - 2 * pad, y1 - (bottom - top) - 2 * pad
    draw.rounded_rectangle((x0, y0, x1, y1), radius=pad, fill=(0, 0, 0, 120))
    draw.text((x0 + pad - left, y0 + pad - top), label, font=font, fill=(255, 255, 255, 240))

    Image.alpha_composite(image, overlay).convert("RGB").save(path, format="JPEG", quality=95)
    logger.info(f"Watermark '{label}' applied to {path}")
