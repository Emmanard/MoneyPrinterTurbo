"""Caption and text-card layout for the scene pipeline.

Pure functions: validation of style, caption and text-card fields; caption
chunking and timing from the narration text and the real audio duration; text
wrapping with real font metrics; and the ffmpeg drawtext filters that burn the
result into a scene. No speech recognition and no model downloads are involved.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from app.services import scene_sfx

FONT_DIR = Path(__file__).resolve().parents[2] / "resource" / "fonts"
FONT_EXTENSIONS = frozenset({".ttf", ".ttc", ".otf"})
DEFAULT_CAPTION_FONT = "BeVietnamPro-Bold.ttf"
DEFAULT_CARD_FONT = "BeVietnamPro-Medium.ttf"

POSITIONS = ("top", "center", "bottom")
CAPTION_MODES = ("chunks", "words", "lines")
MODE_WORDS = {"chunks": 3, "words": 1, "lines": 7}
STYLE_KEYS = frozenset({
    "font", "size", "color", "outline_color", "outline_width", "position",
    "box", "box_color", "box_opacity",
    "align", "panel", "panel_color", "panel_opacity", "accent_color", "accent_width", "accent_side",
})
# Keys that only text cards use; captions reject them.
CARD_ONLY_KEYS = frozenset({
    "align", "panel", "panel_color", "panel_opacity", "accent_color", "accent_width", "accent_side",
})
ALIGNS = ("center", "left")
ACCENT_SIDES = ("left", "top")
MAX_ACCENT_WIDTH = 40
CARD_KINDS = ("plain", "claim", "quote", "fact")
# Fields each kind accepts besides style, start and end. "text" is the main text.
KIND_FIELDS = {
    "plain": ("text",),
    "claim": ("text", "label"),
    "quote": ("text", "source"),
    "fact": ("text", "label", "value"),
}
MAX_LABEL = 40
MAX_CLAIM_TEXT = 140
MAX_QUOTE_TEXT = 200
MAX_QUOTE_SOURCE = 60
MAX_FACT_VALUE = 14
MAX_FACT_TEXT = 80
MIN_FACT_VALUE_RATIO = 48 / 1080  # the value never shrinks below 48 px at 1080 (scaled with the frame)
QUOTE_SOURCE_COLOR = "#D0D0D0"
QUOTE_MARK_SCALE = 2.4   # the opening mark is drawn this many times the base size
QUOTE_MARK_HEIGHT = 0.45  # and reserves this share of its line height (the glyph sits at the top)
DEFAULT_ACCENT_WIDTH = 10
ANIM_IN = ("none", "fade", "slide_up", "slide_left")
ANIM_OUT = ("none", "fade")
ANIM_KEYS = ("in", "out", "duration", "stagger")
MIN_ANIM_SECONDS = 0.05
MAX_ANIM_SECONDS = 2.0
DEFAULT_ANIM_SECONDS = 0.3
MAX_STAGGER_SECONDS = 0.5
SLIDE_DISTANCE = 1.0  # a slide starts this many font sizes away from its final place
DEFAULT_ACCENT = "#FFD400"
# Defaults a card kind applies on top of settings.text_card_style (a card's own style wins).
KIND_DEFAULTS = {
    "plain": {},
    "claim": {"align": "left", "panel": True, "box": False},
    "quote": {"align": "left", "panel": True, "box": False, "accent_width": DEFAULT_ACCENT_WIDTH,
              "accent_side": "left"},
    "fact": {"align": "center", "panel": True, "box": False, "accent_width": DEFAULT_ACCENT_WIDTH,
             "accent_side": "top"},
}

MAX_CUES_PER_SCENE = 400
MAX_TEXT_CARDS = 20
MAX_CARD_TEXT = 500
MIN_FONT_SIZE = 10

# Safe area: horizontal margin on both sides, top margin, and a larger bottom
# margin on vertical video, where platform controls cover the lower part.
MARGIN_X = 0.06
MARGIN_TOP = 0.08
MARGIN_BOTTOM_VERTICAL = 0.14
MARGIN_BOTTOM_HORIZONTAL = 0.08

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_PAUSE_TAG = re.compile(
    r"[\[\(]\s*(?:pause|pausa|silence|silencio|silencio|silenzio|stille|"
    r"пауза|тишина|停顿|暂停|静音|ポーズ|一時停止|無音|일시중지|정지)\b[^\]\)]*[\]\)]",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"[.!?…][\"'”’)]*$")
_CLAUSE_END = re.compile(r"[,;:—][\"'”’)]*$")


class TextLayoutError(ValueError):
    pass


@dataclass(frozen=True)
class TextCard:
    text: str
    style: dict
    start: float
    end: float
    kind: str = "plain"
    label: str = ""
    value: str = ""
    source: str = ""
    animation: dict | None = None
    sfx: dict | None = None


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class DrawLine:
    """One centered line of text shown during [start, end), in scene time."""

    text: str
    font: str
    size: int
    color: str
    outline_color: str
    outline_width: float
    y: int
    box: bool
    box_color: str
    box_opacity: float
    start: float
    end: float
    x: int | None = None  # left edge; None = centred
    anim: tuple | None = None  # (in, out, duration, delay) for an animated card line


@dataclass(frozen=True)
class DrawBox:
    """A filled rectangle (a panel behind a card) shown during [start, end)."""

    x: int
    y: int
    w: int
    h: int
    color: str
    opacity: float
    start: float
    end: float


@dataclass(frozen=True)
class Chain:
    filters: str
    files: dict
    fonts: dict


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


# -- validation -----------------------------------------------------------

def parse_style(raw: object, label: str, errors: list[str], card: bool = True) -> dict:
    """Validate a style object; return only the keys that were given."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        errors.append(f"{label}: must be an object")
        return {}
    style: dict = {}
    for key, value in raw.items():
        if key not in STYLE_KEYS:
            errors.append(f"{label}: unknown field: {key}")
        elif key in CARD_ONLY_KEYS and not card:
            errors.append(f"{label}.{key}: only text cards use this; captions do not")
        elif key == "font":
            if (
                not isinstance(value, str)
                or value != Path(value).name
                or Path(value).suffix.lower() not in FONT_EXTENSIONS
                or not (FONT_DIR / value).is_file()
            ):
                errors.append(f"{label}.font: must be the name of a font file in resource/fonts")
            else:
                style[key] = value
        elif key == "size":
            if isinstance(value, bool) or not isinstance(value, int) or not 8 <= value <= 400:
                errors.append(f"{label}.size: must be a whole number from 8 to 400")
            else:
                style[key] = value
        elif key in ("color", "outline_color", "box_color", "panel_color", "accent_color"):
            if not isinstance(value, str) or not _HEX.fullmatch(value):
                errors.append(f"{label}.{key}: must be a color like #RRGGBB")
            else:
                style[key] = value
        elif key == "outline_width":
            if not _is_number(value) or not 0 <= value <= 20:
                errors.append(f"{label}.outline_width: must be a number from 0 to 20")
            else:
                style[key] = float(value)
        elif key == "position":
            if value not in POSITIONS:
                errors.append(f"{label}.position: must be one of {', '.join(POSITIONS)}")
            else:
                style[key] = value
        elif key in ("box", "panel"):
            if not isinstance(value, bool):
                errors.append(f"{label}.{key}: must be true or false")
            else:
                style[key] = value
        elif key == "align":
            if value not in ALIGNS:
                errors.append(f"{label}.align: must be one of {', '.join(ALIGNS)}")
            else:
                style[key] = value
        elif key == "accent_side":
            if value not in ACCENT_SIDES:
                errors.append(f"{label}.accent_side: must be one of {', '.join(ACCENT_SIDES)}")
            else:
                style[key] = value
        elif key == "accent_width":
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_ACCENT_WIDTH:
                errors.append(f"{label}.accent_width: must be a whole number from 0 to {MAX_ACCENT_WIDTH}")
            else:
                style[key] = value
        elif key in ("box_opacity", "panel_opacity"):
            if not _is_number(value) or not 0 <= value <= 1:
                errors.append(f"{label}.{key}: must be a number from 0 to 1")
            else:
                style[key] = float(value)
    return style


def parse_caption_spec(raw: object, label: str, errors: list[str]) -> dict | None:
    """Validate {enabled, mode, style}; return only the keys that were given."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        errors.append(f"{label}: must be an object")
        return None
    spec: dict = {}
    for key, value in raw.items():
        if key == "enabled":
            if not isinstance(value, bool):
                errors.append(f"{label}.enabled: must be true or false")
            else:
                spec["enabled"] = value
        elif key == "mode":
            if value not in CAPTION_MODES:
                errors.append(f"{label}.mode: must be one of {', '.join(CAPTION_MODES)}")
            else:
                spec["mode"] = value
        elif key == "style":
            spec["style"] = parse_style(value, f"{label}.style", errors, card=False)
        else:
            errors.append(f"{label}: unknown field: {key}")
    return spec


def parse_animation(raw: object, label: str, errors: list[str]) -> dict:
    """Validate {in, out, duration, stagger}; return only the keys that were given."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        errors.append(f"{label}: must be an object like {{\"in\": \"slide_up\", \"duration\": 0.3}}")
        return {}
    spec: dict = {}
    for key, value in raw.items():
        if key not in ANIM_KEYS:
            errors.append(f"{label}: unknown field: {key}")
        elif key == "in":
            if value not in ANIM_IN:
                errors.append(f"{label}.in: must be one of {', '.join(ANIM_IN)}")
            else:
                spec[key] = value
        elif key == "out":
            if value not in ANIM_OUT:
                errors.append(f"{label}.out: must be one of {', '.join(ANIM_OUT)}")
            else:
                spec[key] = value
        elif key == "duration":
            if not _is_number(value) or not MIN_ANIM_SECONDS <= value <= MAX_ANIM_SECONDS:
                errors.append(f"{label}.duration: must be a number from {MIN_ANIM_SECONDS:g} to "
                              f"{MAX_ANIM_SECONDS:g} seconds")
            else:
                spec[key] = float(value)
        elif key == "stagger":
            if not _is_number(value) or not 0 <= value <= MAX_STAGGER_SECONDS:
                errors.append(f"{label}.stagger: must be a number from 0 to {MAX_STAGGER_SECONDS:g} seconds")
            else:
                spec[key] = float(value)
    return spec


def resolve_animation(card_spec: dict | None, default: dict | None) -> tuple | None:
    """Combine the project default with a card's own animation. Returns
    (in, out, duration, stagger) or None when the card does not animate."""
    merged = {**(default or {}), **(card_spec or {})}
    kind_in, kind_out = merged.get("in", "none"), merged.get("out", "none")
    if kind_in == "none" and kind_out == "none":
        return None
    return (kind_in, kind_out, merged.get("duration", DEFAULT_ANIM_SECONDS), merged.get("stagger", 0.0))


def parse_text_cards(raw: object, label: str, errors: list[str]) -> tuple[TextCard, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > MAX_TEXT_CARDS:
        errors.append(f"{label}.text_cards: must be a list of at most {MAX_TEXT_CARDS} cards")
        return ()
    cards = []
    for index, item in enumerate(raw):
        name = f"{label}.text_cards[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{name}: must be an object")
            continue
        for key in item:
            if key not in ("text", "style", "start", "end", "kind", "label", "value", "source", "animation", "sfx"):
                errors.append(f"{name}: unknown field: {key}")
        kind = item.get("kind", "plain")
        if kind not in CARD_KINDS:
            errors.append(f"{name}.kind: must be one of {', '.join(CARD_KINDS)}")
            kind = "plain"
        for field in ("label", "value", "source"):
            if field in item and field not in KIND_FIELDS[kind]:
                owners = " and ".join(k for k in CARD_KINDS if field in KIND_FIELDS[k])
                errors.append(f"{name}.{field}: only used by {owners} cards")

        def one_line(field: str, limit: int, required: bool) -> str:
            value = item.get(field, "")
            ok = isinstance(value, str) and "\n" not in value and len(value) <= limit
            if ok and required and not value.strip():
                ok = False
            if not ok:
                errors.append(
                    f"{name}.{field}: needs a one-line {field} of 1 to {limit} characters" if required
                    else f"{name}.{field}: must be one line of at most {limit} characters")
                return ""
            return " ".join(value.split())

        label = value = source = ""
        text = item.get("text", "")
        if kind == "claim":
            label = one_line("label", MAX_LABEL, True)
        elif kind == "fact":
            label = one_line("label", MAX_LABEL, False) if "label" in item else ""
            value = one_line("value", MAX_FACT_VALUE, True)
        elif kind == "quote":
            source = one_line("source", MAX_QUOTE_SOURCE, False) if "source" in item else ""
        limit = {"claim": MAX_CLAIM_TEXT, "quote": MAX_QUOTE_TEXT, "fact": MAX_FACT_TEXT}.get(kind, MAX_CARD_TEXT)
        text_optional = kind == "fact"
        if text_optional and ("text" not in item or item.get("text") == ""):
            text = ""
        elif not isinstance(text, str) or not text.strip() or len(text) > limit:
            errors.append(f"{name}.text: must be 1 to {limit} characters")
            text = ""
        style = parse_style(item.get("style"), f"{name}.style", errors)
        start, end = item.get("start"), item.get("end")
        if not _is_number(start) or start < 0:
            errors.append(f"{name}.start: must be a number of seconds, 0 or more")
            start = 0.0
        if not _is_number(end) or end <= start:
            errors.append(f"{name}.end: must be a number of seconds after start")
            end = float(start) + 1.0
        animation = parse_animation(item.get("animation"), f"{name}.animation", errors)
        sfx = scene_sfx.parse_sfx(item.get("sfx"), f"{name}.sfx", errors)
        cards.append(TextCard(text, style, float(start), float(end), kind, label, value, source,
                              animation or None, sfx))
    return tuple(cards)


def check_card_times(cards: tuple[TextCard, ...], scene_length: float, label: str,
                     errors: list[str]) -> None:
    for index, card in enumerate(cards):
        if card.end > scene_length + 1e-6:
            errors.append(
                f"{label}.text_cards[{index}]: ends at {card.end:g}s but the scene lasts "
                f"{scene_length:.2f}s"
            )


# -- styles ---------------------------------------------------------------

def merge_caption(defaults: dict, scene: dict | None) -> dict:
    """Combine the settings-level caption default with a scene's own spec."""
    defaults = defaults or {}
    scene = scene or {}
    return {
        "enabled": scene.get("enabled", defaults.get("enabled", False)),
        "mode": scene.get("mode", defaults.get("mode", "chunks")),
        "style": {**defaults.get("style", {}), **scene.get("style", {})},
    }


def resolve_style(kind: str, width: int, height: int, *layers: dict) -> dict:
    """Fill in a complete style. ``kind`` is "caption" or "card"."""
    caption = kind == "caption"
    size = round(min(width, height) * (0.055 if caption else 0.06))
    style = {
        "font": DEFAULT_CAPTION_FONT if caption else DEFAULT_CARD_FONT,
        "size": size,
        "color": "#FFFFFF",
        "outline_color": "#000000",
        "outline_width": None,
        "position": "bottom" if caption else "center",
        "box": not caption,
        "box_color": "#000000",
        "box_opacity": 0.55,
        "align": "center",
        "panel": False,
        "panel_color": "#000000",
        "panel_opacity": 0.72,
        "accent_color": DEFAULT_ACCENT,
        "accent_width": 0,
        "accent_side": "left",
    }
    for layer in layers:
        style.update(layer or {})
    if style["outline_width"] is None:
        style["outline_width"] = float(max(2, round(style["size"] / 14)))
    return style


# -- captions -------------------------------------------------------------

def clean_text(text: str) -> str:
    """Drop TTS pause tags and collapse whitespace."""
    return " ".join(_PAUSE_TAG.sub(" ", text).split())


def split_chunks(text: str, mode: str) -> list[str]:
    limit = MODE_WORDS[mode]
    chunks: list[str] = []
    current: list[str] = []
    for word in clean_text(text).split():
        current.append(word)
        sentence_end = bool(_SENTENCE_END.search(word))
        clause_end = bool(_CLAUSE_END.search(word)) and len(current) >= (2 if limit <= 3 else 3)
        if len(current) >= limit or sentence_end or (limit > 1 and clause_end):
            chunks.append(" ".join(current))
            current = []
    if current:
        chunks.append(" ".join(current))
    return chunks


def _weight(chunk: str) -> float:
    weight = float(max(1, len(re.sub(r"\W+", "", chunk))))
    if _SENTENCE_END.search(chunk):
        weight += 4
    elif _CLAUSE_END.search(chunk):
        weight += 2
    return weight


def caption_cues(text: str, duration: float, mode: str) -> list[Cue]:
    """Split ``text`` into short cues that cover [0, duration] without gaps.

    Each cue gets a share of the duration proportional to its length, with extra
    weight after sentence and clause ends. The first cue starts at 0 and the last
    ends exactly at ``duration``.
    """
    chunks = split_chunks(text, mode)
    if not chunks:
        return []
    if len(chunks) > MAX_CUES_PER_SCENE:
        raise TextLayoutError(
            f"narration splits into {len(chunks)} captions; the limit is {MAX_CUES_PER_SCENE} per "
            "scene (split the scene or use a longer caption mode)"
        )
    weights = [_weight(chunk) for chunk in chunks]
    total = sum(weights)
    cues = []
    cumulative = 0.0
    previous = 0.0
    for index, chunk in enumerate(chunks):
        cumulative += weights[index]
        end = duration if index == len(chunks) - 1 else max(previous, round(duration * cumulative / total, 3))
        cues.append(Cue(previous, min(end, duration), chunk))
        previous = end
    return cues


# -- layout ---------------------------------------------------------------

_font_cache: dict = {}


def _font(path: Path, size: int):
    key = (str(path), size)
    if key not in _font_cache:
        try:
            from PIL import ImageFont
        except ImportError as exc:
            raise TextLayoutError("Pillow is required to lay out captions and text cards") from exc
        _font_cache[key] = ImageFont.truetype(str(path), size)
    return _font_cache[key]


def wrap_lines(text: str, font, max_width: float) -> list[str]:
    """Greedy word wrap using the font's real metrics. Over-long words are
    split by character so no line exceeds ``max_width``."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        current = ""
        for word in paragraph.split():
            candidate = word if not current else f"{current} {word}"
            if font.getlength(candidate) <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current)
                current = ""
            if font.getlength(word) <= max_width:
                current = word
                continue
            piece = ""
            for char in word:
                if not piece or font.getlength(piece + char) <= max_width:
                    piece += char
                else:
                    lines.append(piece)
                    piece = char
            current = piece
        if current:
            lines.append(current)
    return lines


def layout_block(text: str, style: dict, width: int, height: int) -> tuple[list[str], int, list[int]]:
    """Wrap ``text`` inside the safe area, shrinking the font if needed.

    Returns (lines, font size used, top y of each line).
    """
    path = FONT_DIR / style["font"]
    margin_x = round(MARGIN_X * width)
    top = round(MARGIN_TOP * height)
    bottom = round((MARGIN_BOTTOM_VERTICAL if height > width else MARGIN_BOTTOM_HORIZONTAL) * height)
    max_width = width - 2 * margin_x - 2 * math.ceil(style["outline_width"])
    available = height - top - bottom
    size = style["size"]
    while True:
        font = _font(path, size)
        lines = wrap_lines(text, font, max_width)
        ascent, descent = font.getmetrics()
        line_height = ascent + descent
        spacing = round(size * 0.1)
        block = len(lines) * line_height + max(0, len(lines) - 1) * spacing
        if lines and block <= available:
            break
        if size <= MIN_FONT_SIZE:
            raise TextLayoutError(
                f"text does not fit inside the safe area even at {MIN_FONT_SIZE}px: {text[:40]!r}"
            )
        size = max(MIN_FONT_SIZE, int(size * 0.92))
    if style["position"] == "top":
        first = top
    elif style["position"] == "center":
        first = max(top, min((height - block) // 2, height - bottom - block))
    else:
        first = height - bottom - block
    return lines, size, [first + k * (line_height + spacing) for k in range(len(lines))]


def _draw_lines(text: str, style: dict, start: float, end: float, width: int,
                height: int) -> list[DrawLine]:
    lines, size, ys = layout_block(text, style, width, height)
    return [
        DrawLine(line, style["font"], size, style["color"], style["outline_color"],
                 style["outline_width"], y, style["box"], style["box_color"],
                 style["box_opacity"], start, end)
        for line, y in zip(lines, ys)
    ]


@dataclass(frozen=True)
class Part:
    """One run of lines in a text block: its text, size relative to the block's base size,
    colour (None = the style colour) and whether it must stay on one line."""

    text: str
    scale: float = 1.0
    color: str | None = None
    single_line: bool = False
    min_size: int = 0  # the block shrinks until this part fits, but never below this size
    height_ratio: float = 1.0  # share of the line height the part reserves (a hanging quote mark uses less)


@dataclass(frozen=True)
class Block:
    lines: list  # (text, size, color, y, part index) in drawing order
    x: int | None  # left edge of the text, None = centred
    panel: tuple | None  # (x, y, w, h) or None
    bar: tuple | None = None  # (x, y, w, h) of the accent bar, inside the panel, or None


PANEL_PAD_X = 0.5   # panel padding, as a fraction of the base size
PANEL_PAD_Y = 0.45
PART_GAP = 0.3      # space between parts, as a fraction of the base size


def layout_parts(parts: list[Part], style: dict, width: int, height: int) -> Block:
    """Lay out one or more parts as a block inside the safe area.

    This is the shared text-block primitive: parts of different sizes and colours, left
    or centred alignment, and an optional panel behind the whole block. The base size
    shrinks until the block fits; the panel and the text never leave the safe margins
    (6% on the sides, 8% at the top, 14% at the bottom of vertical video)."""
    path = FONT_DIR / style["font"]
    margin_x = round(MARGIN_X * width)
    top = round(MARGIN_TOP * height)
    bottom = round((MARGIN_BOTTOM_VERTICAL if height > width else MARGIN_BOTTOM_HORIZONTAL) * height)
    outline = math.ceil(style["outline_width"])
    panel = style["panel"]
    left = style["align"] == "left"
    bar_width = style["accent_width"] if panel else 0
    bar_side = style["accent_side"]
    base = style["size"]
    while True:
        if panel:
            pad_x, pad_y = round(base * PANEL_PAD_X), round(base * PANEL_PAD_Y)
            # The bar takes room of its own, so it never covers text: on the left it is
            # added to the left padding (and to the right one when text is centred, to keep
            # it centred); on top it is added to the top padding.
            extra_left = bar_width if bar_side == "left" else 0
            extra_top = bar_width if bar_side == "top" else 0
        else:
            pad_x = max(4, round(base * 0.25)) if style["box"] and left else 0
            pad_y = 0
            extra_left = extra_top = 0
        pad_right = pad_x + (0 if left else extra_left)
        max_width = width - 2 * margin_x - pad_x - extra_left - pad_right - 2 * outline
        gap = round(base * PART_GAP)
        rows = []  # (text, size, color, line_height, spacing)
        widest = 0.0
        fits = True
        for part in parts:
            size = max(MIN_FONT_SIZE, round(base * part.scale))
            font = _font(path, size)
            wrapped = wrap_lines(part.text, font, max_width)
            if size < part.min_size or not wrapped or (part.single_line and len(wrapped) != 1):
                fits = False
                break
            ascent, descent = font.getmetrics()
            for text in wrapped:
                widest = max(widest, font.getlength(text))
            rows.append((wrapped, size, part.color or style["color"],
                         round((ascent + descent) * part.height_ratio), round(size * 0.1)))
        if fits:
            block = sum(len(w) * h + max(0, len(w) - 1) * s for w, _, _, h, s in rows)
            block += gap * (len(rows) - 1)
            total = block + 2 * pad_y + extra_top
            if total <= height - top - bottom:
                break
        if base <= MIN_FONT_SIZE:
            raise TextLayoutError(
                f"text does not fit inside the safe area even at {MIN_FONT_SIZE}px: "
                f"{parts[0].text[:40]!r}"
            )
        base = max(MIN_FONT_SIZE, int(base * 0.92))
    if style["position"] == "top":
        y0 = top
    elif style["position"] == "center":
        y0 = max(top, min((height - total) // 2, height - bottom - total))
    else:
        y0 = height - bottom - total
    panel_width = min(width - 2 * margin_x,
                      math.ceil(widest) + 2 * outline + pad_x + extra_left + pad_right)
    panel_x = margin_x if left else (width - panel_width) // 2
    lines = []
    y = y0 + pad_y + extra_top
    for index, (wrapped, size, color, height_of_line, spacing) in enumerate(rows):
        if index:
            y += gap
        for text in wrapped:
            lines.append((text, size, color, y, index))
            y += height_of_line + spacing
        y -= spacing
    x = panel_x + pad_x + extra_left + outline if left else None
    bar = None
    if bar_width:
        bar = ((panel_x, y0, bar_width, total) if bar_side == "left"
               else (panel_x, y0, panel_width, bar_width))
    return Block(lines, x, (panel_x, y0, panel_width, total) if panel else None, bar)


def card_parts(card: TextCard, style: dict, width: int = 1080, height: int = 1920) -> list[Part]:
    """The parts of a card, top to bottom."""
    accent = style["accent_color"]
    if card.kind == "plain":
        return [Part(card.text)]
    if card.kind == "claim":
        return [Part(card.label.upper(), 0.55, accent, single_line=True), Part(card.text)]
    if card.kind == "quote":
        parts = [Part("\u201c", QUOTE_MARK_SCALE, accent, single_line=True, height_ratio=QUOTE_MARK_HEIGHT),
                 Part(card.text)]
        if card.source:
            parts.append(Part("\u2014 " + card.source, 0.65, QUOTE_SOURCE_COLOR))
        return parts
    parts = []  # fact: small label, big value, optional caption
    if card.label:
        parts.append(Part(card.label.upper(), 0.5, accent, single_line=True))
    parts.append(Part(card.value, 2.2, None, single_line=True,
                      min_size=round(min(width, height) * MIN_FACT_VALUE_RATIO)))
    if card.text:
        parts.append(Part(card.text, 0.85))
    return parts


def card_items(card: TextCard, style: dict, width: int, height: int,
               animation: tuple | None = None) -> list:
    """Overlay items (DrawBox and DrawLine) for one text card. ``animation`` is the resolved
    (in, out, duration, stagger) or None; the panel and the bar cut in and out, the text lines
    animate (each part starts ``stagger`` seconds after the one above it)."""
    plain = card.kind == "plain"
    if plain and style["align"] == "center" and not style["panel"] and animation is None:
        # The original path, untouched: same lines, same keys, same output.
        return _draw_lines(card.text, style, card.start, card.end, width, height)
    block = layout_parts(card_parts(card, style, width, height), style, width, height)
    items: list = []
    if block.panel is not None:
        px, py, pw, ph = block.panel
        items.append(DrawBox(px, py, pw, ph, style["panel_color"], style["panel_opacity"],
                             card.start, card.end))
    if block.bar is not None:
        bx, by, bw, bh = block.bar
        items.append(DrawBox(bx, by, bw, bh, style["accent_color"], 1.0, card.start, card.end))
    window = card.end - card.start
    for text, size, color, y, part in block.lines:
        anim = None
        if animation is not None:
            kind_in, kind_out, seconds, stagger = animation
            delay = part * stagger
            need = delay + (seconds if kind_in != "none" else 0) + (seconds if kind_out != "none" else 0)
            if need > window + 1e-6:
                raise TextLayoutError(
                    f"the card animation needs {need:.2f}s (in, out and stagger) but the card is on screen "
                    f"for {window:.2f}s: {card.text[:30] or card.value[:30]!r}")
            anim = (kind_in, kind_out, seconds, delay)
        items.append(DrawLine(text, style["font"], size, color, style["outline_color"],
                              style["outline_width"], y, style["box"], style["box_color"],
                              style["box_opacity"], card.start, card.end, block.x, anim))
    return items


def scene_overlays(narration: str, audio_duration: float, caption: dict,
                   cards: tuple[TextCard, ...], card_defaults: dict, width: int,
                   height: int, animation_default: dict | None = None) -> list[DrawLine]:
    """Every line of text to burn into one scene, with its on-screen window."""
    items: list[DrawLine] = []
    if caption.get("enabled"):
        style = resolve_style("caption", width, height, caption.get("style", {}))
        for cue in caption_cues(narration, audio_duration, caption["mode"]):
            if cue.end > cue.start:
                items.extend(_draw_lines(cue.text, style, cue.start, cue.end, width, height))
    for card in cards:
        style = resolve_style("card", width, height, card_defaults, KIND_DEFAULTS[card.kind], card.style)
        items.extend(card_items(card, style, width, height,
                                resolve_animation(card.animation, animation_default)))
    return items


def overlay_key(items: list[DrawLine]) -> list:
    """A JSON-friendly description of the overlays, including the font files
    (name and size in bytes) so a replaced font invalidates the cache."""
    fonts = {}
    for item in items:
        if not isinstance(item, DrawLine):
            continue
        if item.font not in fonts:
            fonts[item.font] = (FONT_DIR / item.font).stat().st_size
    # Rows for original (centred, no panel) lines are unchanged, so caches made before the
    # panel and alignment options still match; the new parts are only added when used.
    rows = []
    for i in items:
        if isinstance(i, DrawBox):
            rows.append(["panel", i.x, i.y, i.w, i.h, i.color, i.opacity, i.start, i.end])
            continue
        row = [i.text, i.font, i.size, i.color, i.outline_color, i.outline_width, i.y, i.box,
               i.box_color, i.box_opacity, i.start, i.end]
        if i.x is not None or i.anim is not None:
            row.append(i.x)
        if i.anim is not None:
            row.append(["anim", *i.anim])
        rows.append(row)
    return [rows, sorted(fonts.items())]


# -- ffmpeg ---------------------------------------------------------------

def _ffmpeg_color(value: str) -> str:
    return "0x" + value[1:].upper()


def _progress(item: DrawLine) -> str:
    """0 to 1 over the entrance of an animated line (cubic ease-out is applied by the caller)."""
    _, _, seconds, delay = item.anim
    return f"clip((t-{item.start + delay:.3f})/{seconds:g},0,1)"


def _slide_offset(item: DrawLine) -> str:
    return f"{round(item.size * SLIDE_DISTANCE)}*pow(1-{_progress(item)},3)"


def _x_expression(item: DrawLine) -> str:
    base = "(w-text_w)/2" if item.x is None else str(item.x)
    if item.anim is not None and item.anim[0] == "slide_left":
        return f"x='{base}-{_slide_offset(item)}'"
    return f"x={base}"


def _y_expression(item: DrawLine) -> str:
    if item.anim is not None and item.anim[0] == "slide_up":
        return f"y='{item.y}+{_slide_offset(item)}'"
    return f"y={item.y}"


def _alpha_expression(item: DrawLine) -> str:
    """Fade in with the entrance and fade out before the end; empty for static lines."""
    if item.anim is None:
        return ""
    kind_in, kind_out, seconds, _ = item.anim
    parts = []
    if kind_in != "none":
        parts.append(_progress(item))
    if kind_out == "fade":
        parts.append(f"clip(({item.end:.3f}-t)/{seconds:g},0,1)")
    return "*".join(parts)


def drawtext_chain(items: list[DrawLine], prefix: str) -> Chain:
    """Build comma-joined drawtext filters. Text and fonts are referenced by
    short relative names, so no path or text needs escaping; the caller writes
    ``files`` into the working directory and links ``fonts`` there."""
    filters = []
    files: dict[str, str] = {}
    fonts: dict[str, Path] = {}
    aliases: dict[str, str] = {}
    for index, item in enumerate(items):
        if isinstance(item, DrawBox):
            filters.append(
                f"drawbox=x={item.x}:y={item.y}:w={item.w}:h={item.h}:"
                f"color={_ffmpeg_color(item.color)}@{item.opacity:g}:t=fill:"
                f"enable='gte(t,{item.start:.3f})*lt(t,{item.end:.3f})'"
            )
            continue
        if item.font not in aliases:
            alias = f"{prefix}font{len(aliases)}{Path(item.font).suffix.lower()}"
            aliases[item.font] = alias
            fonts[alias] = FONT_DIR / item.font
        name = f"{prefix}line{index}.txt"
        files[name] = item.text
        parts = [
            f"drawtext=fontfile={aliases[item.font]}",
            f"textfile={name}",
            "expansion=none",
            f"fontsize={item.size}",
            f"fontcolor={_ffmpeg_color(item.color)}",
            f"borderw={item.outline_width:g}",
            f"bordercolor={_ffmpeg_color(item.outline_color)}",
            _x_expression(item),
            _y_expression(item),
            f"enable='gte(t,{item.start:.3f})*lt(t,{item.end:.3f})'",
        ]
        alpha = _alpha_expression(item)
        if alpha:
            parts.append(f"alpha='{alpha}'")
        if item.box:
            parts += [
                "box=1",
                f"boxcolor={_ffmpeg_color(item.box_color)}@{item.box_opacity:g}",
                f"boxborderw={max(4, round(item.size * 0.25))}",
            ]
        filters.append(":".join(parts))
    return Chain(",".join(filters), files, fonts)
