"""Optional look for a scene: colour grade, vignette, and the flash on a transition.

Pure functions: validation, field-by-field merging of the project default with a scene's
own look, and the ffmpeg filter strings. Everything is off unless it is set, and a manifest
without any of it produces the filter chains it always did.
"""

from __future__ import annotations

import math
import re

GRADE_PRESETS = ("warm", "cool", "faded", "high_contrast")
LOOK_KEYS = ("vignette", "grade")
GRADE_KEYS = ("preset", "contrast", "saturation", "brightness")
FLASH_KEYS = ("duration", "strength", "color")
MIN_FLASH_SECONDS = 0.03
MAX_FLASH_SECONDS = 0.3
DEFAULT_FLASH_STRENGTH = 0.8
DEFAULT_FLASH_COLOR = "#FFFFFF"

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

# eq values and a colour balance per preset. User values for contrast, saturation and
# brightness replace the preset's own.
_PRESETS = {
    "warm": {"eq": (1.04, 1.08, 0.0), "balance": "rs=0.06:bs=-0.06:rm=0.05:bm=-0.05:rh=0.03:bh=-0.03"},
    "cool": {"eq": (1.04, 1.0, 0.0), "balance": "rs=-0.05:bs=0.06:rm=-0.04:bm=0.05:rh=-0.02:bh=0.03"},
    "faded": {"eq": (0.88, 0.82, 0.04), "balance": ""},
    "high_contrast": {"eq": (1.25, 1.1, -0.02), "balance": ""},
}


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def parse_look(raw: object, label: str, errors: list[str]) -> dict:
    """Validate {vignette: {amount}, grade: {preset, contrast, saturation, brightness}}.
    Returns only what was given."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        errors.append(f"{label}: must be an object")
        return {}
    look: dict = {}
    for key, value in raw.items():
        if key not in LOOK_KEYS:
            errors.append(f"{label}: unknown field: {key}")
        elif key == "vignette":
            if not isinstance(value, dict):
                errors.append(f"{label}.vignette: must be an object like {{\"amount\": 0.35}}")
                continue
            for field in value:
                if field != "amount":
                    errors.append(f"{label}.vignette: unknown field: {field}")
            amount = value.get("amount")
            if not _is_number(amount) or not 0 <= amount <= 1:
                errors.append(f"{label}.vignette.amount: must be a number from 0 to 1")
            else:
                look["vignette"] = {"amount": float(amount)}
        elif key == "grade":
            if not isinstance(value, dict):
                errors.append(f"{label}.grade: must be an object")
                continue
            grade: dict = {}
            for field, item in value.items():
                if field not in GRADE_KEYS:
                    errors.append(f"{label}.grade: unknown field: {field}")
                elif field == "preset":
                    if item not in GRADE_PRESETS:
                        errors.append(f"{label}.grade.preset: must be one of {', '.join(GRADE_PRESETS)}")
                    else:
                        grade[field] = item
                else:
                    low, high = {"contrast": (0.5, 2.0), "saturation": (0.0, 3.0),
                                 "brightness": (-0.5, 0.5)}[field]
                    if not _is_number(item) or not low <= item <= high:
                        errors.append(f"{label}.grade.{field}: must be a number from {low:g} to {high:g}")
                    else:
                        grade[field] = float(item)
            if not value:
                errors.append(f"{label}.grade: give a preset or at least one of contrast, saturation, brightness")
            elif grade:
                look["grade"] = grade
    return look


def merge_look(default: dict | None, scene: dict | None) -> dict:
    """A scene's look over the project default, field by field inside vignette and grade."""
    merged: dict = {}
    for key in LOOK_KEYS:
        parts = {**(default or {}).get(key, {}), **(scene or {}).get(key, {})}
        if parts:
            merged[key] = parts
    return merged


def look_filters(look: dict) -> str:
    """The ffmpeg filters for a look (grade, then vignette), or "" when nothing is set."""
    filters = []
    grade = look.get("grade")
    if grade:
        preset = _PRESETS.get(grade.get("preset"), {"eq": (1.0, 1.0, 0.0), "balance": ""})
        contrast, saturation, brightness = preset["eq"]
        contrast = grade.get("contrast", contrast)
        saturation = grade.get("saturation", saturation)
        brightness = grade.get("brightness", brightness)
        if (contrast, saturation, brightness) != (1.0, 1.0, 0.0):
            filters.append(f"eq=contrast={contrast:g}:saturation={saturation:g}:brightness={brightness:g}")
        if preset["balance"]:
            filters.append(f"colorbalance={preset['balance']}")
    vignette = look.get("vignette")
    if vignette and vignette["amount"] > 0:
        # A larger lens angle darkens the corners more (measured: 0.24 rad dims a corner by about
        # 10%, 0.39 by about 27%): 0 is off, 1 is a strong vignette (0.94 rad).
        angle = (math.pi / 2) * 0.6 * vignette["amount"]
        filters.append(f"vignette=angle={angle:.4f}")
    return ",".join(filters)


def look_key(look: dict) -> list | None:
    """Cache-key part: None for no look, so keys of manifests without a look do not change."""
    return None if not look_filters(look) else [look_filters(look)]


def parse_flash(raw: object, label: str, transition_seconds: float, errors: list[str]) -> dict | None:
    """Validate a transition's flash {duration, strength, color}."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        errors.append(f"{label}.flash: must be an object like {{\"duration\": 0.08, \"strength\": 0.8}}")
        return None
    for key in raw:
        if key not in FLASH_KEYS:
            errors.append(f"{label}.flash: unknown field: {key}")
    duration = raw.get("duration")
    strength = raw.get("strength", DEFAULT_FLASH_STRENGTH)
    color = raw.get("color", DEFAULT_FLASH_COLOR)
    ok = True
    if not _is_number(duration) or not MIN_FLASH_SECONDS <= duration <= MAX_FLASH_SECONDS:
        errors.append(f"{label}.flash.duration: must be a number from {MIN_FLASH_SECONDS:g} to "
                      f"{MAX_FLASH_SECONDS:g} seconds")
        ok = False
    elif duration >= transition_seconds:
        errors.append(f"{label}.flash.duration: must be shorter than the transition ({transition_seconds:g}s)")
        ok = False
    if not _is_number(strength) or not 0 < strength <= 1:
        errors.append(f"{label}.flash.strength: must be a number above 0 and at most 1")
        ok = False
    if not isinstance(color, str) or not _HEX.fullmatch(color):
        errors.append(f"{label}.flash.color: must be a color like #RRGGBB")
        ok = False
    if not ok:
        return None
    return {"duration": float(duration), "strength": float(strength), "color": color.upper()}


def flash_graph(flash: dict, fps: int, width: int, height: int, shared_frames: int) -> str:
    """Filters that overlay a brief colour flash centred on the middle of a junction.

    ``[v]`` is the cross-faded picture; the result is ``[vf]``. The flash is a solid colour
    whose alpha rises to ``strength`` and falls again (a triangular envelope), placed so its
    peak is at the middle of the transition."""
    seconds = shared_frames / fps
    length = max(flash["duration"], 2.0 / fps)
    half = length / 2
    start = max(0.0, seconds / 2 - half)
    color = "0x" + flash["color"][1:]
    return (
        f"color=c={color}:s={width}x{height}:r={fps}:d={length:.6f},format=yuva420p,"
        f"fade=t=in:st=0:d={half:.6f}:alpha=1,fade=t=out:st={half:.6f}:d={half:.6f}:alpha=1,"
        f"colorchannelmixer=aa={flash['strength']:g},setpts=PTS+{start:.6f}/TB[fl];"
        "[v][fl]overlay=eof_action=pass:format=auto[vf]"
    )


def flash_key(flash: dict | None) -> list | None:
    return None if flash is None else [flash["duration"], flash["strength"], flash["color"]]
