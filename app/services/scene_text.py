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
})

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

def parse_style(raw: object, label: str, errors: list[str]) -> dict:
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
        elif key in ("color", "outline_color", "box_color"):
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
        elif key == "box":
            if not isinstance(value, bool):
                errors.append(f"{label}.box: must be true or false")
            else:
                style[key] = value
        elif key == "box_opacity":
            if not _is_number(value) or not 0 <= value <= 1:
                errors.append(f"{label}.box_opacity: must be a number from 0 to 1")
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
            spec["style"] = parse_style(value, f"{label}.style", errors)
        else:
            errors.append(f"{label}: unknown field: {key}")
    return spec


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
            if key not in ("text", "style", "start", "end"):
                errors.append(f"{name}: unknown field: {key}")
        text = item.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_CARD_TEXT:
            errors.append(f"{name}.text: must be 1 to {MAX_CARD_TEXT} characters")
            text = ""
        style = parse_style(item.get("style"), f"{name}.style", errors)
        start, end = item.get("start"), item.get("end")
        if not _is_number(start) or start < 0:
            errors.append(f"{name}.start: must be a number of seconds, 0 or more")
            start = 0.0
        if not _is_number(end) or end <= start:
            errors.append(f"{name}.end: must be a number of seconds after start")
            end = float(start) + 1.0
        cards.append(TextCard(text, style, float(start), float(end)))
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


def scene_overlays(narration: str, audio_duration: float, caption: dict,
                   cards: tuple[TextCard, ...], card_defaults: dict, width: int,
                   height: int) -> list[DrawLine]:
    """Every line of text to burn into one scene, with its on-screen window."""
    items: list[DrawLine] = []
    if caption.get("enabled"):
        style = resolve_style("caption", width, height, caption.get("style", {}))
        for cue in caption_cues(narration, audio_duration, caption["mode"]):
            if cue.end > cue.start:
                items.extend(_draw_lines(cue.text, style, cue.start, cue.end, width, height))
    for card in cards:
        style = resolve_style("card", width, height, card_defaults, card.style)
        items.extend(_draw_lines(card.text, style, card.start, card.end, width, height))
    return items


def overlay_key(items: list[DrawLine]) -> list:
    """A JSON-friendly description of the overlays, including the font files
    (name and size in bytes) so a replaced font invalidates the cache."""
    fonts = {}
    for item in items:
        if item.font not in fonts:
            fonts[item.font] = (FONT_DIR / item.font).stat().st_size
    return [
        [[i.text, i.font, i.size, i.color, i.outline_color, i.outline_width, i.y, i.box,
          i.box_color, i.box_opacity, i.start, i.end] for i in items],
        sorted(fonts.items()),
    ]


# -- ffmpeg ---------------------------------------------------------------

def _ffmpeg_color(value: str) -> str:
    return "0x" + value[1:].upper()


def drawtext_chain(items: list[DrawLine], prefix: str) -> Chain:
    """Build comma-joined drawtext filters. Text and fonts are referenced by
    short relative names, so no path or text needs escaping; the caller writes
    ``files`` into the working directory and links ``fonts`` there."""
    filters = []
    files: dict[str, str] = {}
    fonts: dict[str, Path] = {}
    aliases: dict[str, str] = {}
    for index, item in enumerate(items):
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
            "x=(w-text_w)/2",
            f"y={item.y}",
            f"enable='gte(t,{item.start:.3f})*lt(t,{item.end:.3f})'",
        ]
        if item.box:
            parts += [
                "box=1",
                f"boxcolor={_ffmpeg_color(item.box_color)}@{item.box_opacity:g}",
                f"boxborderw={max(4, round(item.size * 0.25))}",
            ]
        filters.append(":".join(parts))
    return Chain(",".join(filters), files, fonts)
