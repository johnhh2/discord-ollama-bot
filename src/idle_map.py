"""Draws the !idle realm: the 0..MAP_SIZE grid, its landmarks, every
character as a dot, and a journey quest's two waypoints. Pure Pillow, no
Discord — the cog wraps the PNG bytes in a discord.File.

The IRC original served this picture from its website; here it rides on
`!idle status`, `!idle map`, `!idle quest` and a journey's announcement.
"""
from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

from src.idlerpg import LANDMARKS, MAP_SIZE

MAP_FILENAME = "idle_map.png"

_SCALE = 1.2                 # pixels per map unit
_MARGIN = 34
_SIDE = int(MAP_SIZE * _SCALE) + 2 * _MARGIN
_GRID_EVERY = 100
# Past this many characters only the highlighted ones keep a name label.
_MAX_LABELS = 20

_PARCHMENT = (232, 216, 178)
_GRID = (208, 190, 148)
_INK = (74, 54, 32)
_FAINT_INK = (138, 116, 84)
_PLAYER = (44, 86, 140)
_QUESTER = (30, 128, 72)
_HIGHLIGHT = (196, 48, 40)
_ROUTE = (150, 60, 50)


def _px(x: int, y: int) -> "tuple[int, int]":
    return _MARGIN + int(x * _SCALE), _MARGIN + int(y * _SCALE)


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:   # Pillow < 10.1 has only the fixed bitmap font
        return ImageFont.load_default()


def _dot(draw, x: int, y: int, radius: int, fill, outline=None) -> None:
    cx, cy = _px(x, y)
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=fill, outline=outline, width=2)


def _label(draw, x: int, y: int, text: str, font, fill, offset: int = 7) -> None:
    cx, cy = _px(x, y)
    width = draw.textlength(text, font=font)
    # Flip the label to the dot's left near the right edge so it stays on the sheet.
    tx = cx + offset if cx + offset + width < _SIDE - 4 else cx - offset - width
    draw.text((tx, cy - 7), text, font=font, fill=fill)


def render_map(players: list, *, highlight=(), quest: "dict | None" = None) -> bytes:
    """`players` is [(uid, name, x, y)]; `highlight` the uids drawn large;
    `quest` a journey dict ({members, stage, p1, p2}) or None."""
    image = Image.new("RGB", (_SIDE, _SIDE), _PARCHMENT)
    draw = ImageDraw.Draw(image)
    small, normal = _font(11), _font(13)

    for value in range(0, MAP_SIZE + 1, _GRID_EVERY):
        (x0, y0), (x1, y1) = _px(value, 0), _px(value, MAP_SIZE)
        draw.line((x0, y0, x1, y1), fill=_GRID)
        draw.line((y0, x0, y1, x1), fill=_GRID)
        draw.text((x0 - 8, _MARGIN - 16), str(value), font=small, fill=_FAINT_INK)
        draw.text((4, x0 - 6), str(value), font=small, fill=_FAINT_INK)
    draw.rectangle((*_px(0, 0), *_px(MAP_SIZE, MAP_SIZE)), outline=_INK, width=2)

    journey = quest if quest and quest.get("p1") and quest.get("p2") else None
    waypoints = {tuple(journey["p1"]), tuple(journey["p2"])} if journey else set()
    for name, (x, y) in LANDMARKS.items():
        cx, cy = _px(x, y)
        draw.polygon([(cx, cy - 5), (cx + 5, cy), (cx, cy + 5), (cx - 5, cy)], outline=_INK, fill=_PARCHMENT)
        # A waypoint ring is drawn over this landmark: keep the name clear of it.
        _label(draw, x, y, name, small, _FAINT_INK, offset=14 if (x, y) in waypoints else 8)

    questers: set = set()
    if journey:
        questers = set(quest.get("members") or ())
        draw.line((*_px(*quest["p1"]), *_px(*quest["p2"])), fill=_ROUTE, width=2)
        for number, point in (("1", quest["p1"]), ("2", quest["p2"])):
            cx, cy = _px(*point)
            reached = number == "1" and quest.get("stage") == 2
            draw.ellipse((cx - 9, cy - 9, cx + 9, cy + 9), outline=_ROUTE, width=2, fill=_GRID if reached else _PARCHMENT)
            draw.text((cx - 3, cy - 8), number, font=normal, fill=_ROUTE)

    highlight = set(highlight)
    label_all = len(players) <= _MAX_LABELS
    # Highlighted dots last, so they sit on top of a crowd.
    for uid, name, x, y in sorted(players, key=lambda p: p[0] in highlight):
        if uid in highlight:
            _dot(draw, x, y, 6, _HIGHLIGHT, outline=_INK)
            _label(draw, x, y, name, normal, _INK, offset=10)
            continue
        _dot(draw, x, y, 4, _QUESTER if uid in questers else _PLAYER)
        if label_all or uid in questers:
            _label(draw, x, y, name, small, _INK)

    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()
