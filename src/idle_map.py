"""Draws the !idle realm on the IRC game's own map: every character as a
dot, and a journey quest's two waypoints. Pure Pillow, no Discord — the cog
wraps the PNG bytes in a discord.File.

The background (assets/idle_map.png) is the 500×500 world map from the
IdleRPG website package, which its README releases to the public domain;
one map unit is one of its pixels. The original site served this picture
from PHP; here it rides on `!idle status`, `!idle map`, `!idle quest` and a
journey's announcement.
"""
from __future__ import annotations

import io
import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from src.idlerpg import LANDMARKS, MAP_SIZE, MARKET_RADIUS, TOWNS

log = logging.getLogger(__name__)

MAP_FILENAME = "idle_map.png"
_BACKGROUND_PATH = Path(__file__).resolve().parent.parent / "assets" / "idle_map.png"

# The art is 500 px square; doubled (nearest-neighbour, to keep its hard
# two-colour look) so names stay legible in a Discord embed.
_SCALE = 2
_SIDE = MAP_SIZE * _SCALE
# Past this many characters only highlighted ones and questers keep a name.
_MAX_LABELS = 20

_PARCHMENT = (255, 255, 204)
_BROWN = (102, 51, 0)
_PLAYER = (30, 80, 170)
_QUESTER = (20, 130, 60)
_HIGHLIGHT = (210, 40, 40)
_ROUTE = (210, 40, 40)
_MARKET = (176, 120, 40)

_background: "Image.Image | None" = None


def _load_background() -> "Image.Image":
    global _background
    if _background is None:
        try:
            art = Image.open(_BACKGROUND_PATH).convert("RGB")
            _background = art.resize((_SIDE, _SIDE), Image.NEAREST)
        except OSError:
            # The realm still works without its picture.
            log.warning("idle map: %s is missing; drawing on blank parchment", _BACKGROUND_PATH)
            _background = Image.new("RGB", (_SIDE, _SIDE), _PARCHMENT)
    return _background.copy()


def _px(x: int, y: int) -> "tuple[int, int]":
    # Coordinates run 0..MAP_SIZE inclusive; the last one shares the edge pixel.
    return min(x * _SCALE, _SIDE - 1), min(y * _SCALE, _SIDE - 1)


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:   # Pillow < 10.1 has only the fixed bitmap font
        return ImageFont.load_default()


def _dot(draw, x: int, y: int, radius: int, fill) -> None:
    cx, cy = _px(x, y)
    # The parchment rim keeps a dot visible on the map's brown regions.
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=fill, outline=_PARCHMENT, width=2)


def _label(draw, x: int, y: int, text: str, font, offset: int) -> None:
    """A name on a brown plate, as the original site drew it — legible over
    parchment and mountains alike. Flips left / up near the far edges."""
    cx, cy = _px(x, y)
    width, height = int(draw.textlength(text, font=font)) + 8, font.size + 6 if hasattr(font, "size") else 16
    left = cx + offset if cx + offset + width < _SIDE else cx - offset - width
    top = min(max(cy - height // 2, 0), _SIDE - height)
    draw.rectangle((left, top, left + width, top + height), fill=_BROWN)
    draw.text((left + 4, top + 2), text, font=font, fill=_PARCHMENT)


def render_map(players: list, *, highlight=(), quest: "dict | None" = None) -> bytes:
    """`players` is [(uid, name, x, y)]; `highlight` the uids drawn large;
    `quest` a journey dict ({members, stage, p1, p2}) or None."""
    image = _load_background()
    draw = ImageDraw.Draw(image)
    small, normal = _font(15), _font(18)

    # Where `!idle shop` is open.
    for town in TOWNS:
        cx, cy = _px(*LANDMARKS[town])
        reach = MARKET_RADIUS * _SCALE
        draw.ellipse((cx - reach, cy - reach, cx + reach, cy + reach), outline=_MARKET, width=2)

    questers: set = set()
    if quest and quest.get("p1") and quest.get("p2"):
        questers = set(quest.get("members") or ())
        draw.line((*_px(*quest["p1"]), *_px(*quest["p2"])), fill=_ROUTE, width=3)
        for number, point in (("1", quest["p1"]), ("2", quest["p2"])):
            cx, cy = _px(*point)
            reached = number == "1" and quest.get("stage") == 2
            draw.ellipse((cx - 13, cy - 13, cx + 13, cy + 13), outline=_ROUTE, width=3, fill=_ROUTE if reached else _PARCHMENT)
            draw.text((cx - 5, cy - 11), number, font=normal, fill=_PARCHMENT if reached else _ROUTE)

    highlight = set(highlight)
    label_all = len(players) <= _MAX_LABELS
    # Highlighted dots last, so they sit on top of a crowd.
    for uid, name, x, y in sorted(players, key=lambda p: p[0] in highlight):
        if uid in highlight:
            _dot(draw, x, y, 9, _HIGHLIGHT)
            _label(draw, x, y, name, normal, offset=14)
            continue
        _dot(draw, x, y, 6, _QUESTER if uid in questers else _PLAYER)
        if label_all or uid in questers:
            _label(draw, x, y, name, small, offset=10)

    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()
