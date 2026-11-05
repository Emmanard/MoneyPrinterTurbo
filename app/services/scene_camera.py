"""Camera moves with easing for still images: a zoom and a focus point that travel from one
state to another over the length of a clip.

Pure functions: validation, and the ffmpeg ``zoompan`` expressions. The preset motions in
``scene_pipeline`` (``zoom_in``, ``pan_left`` and the rest) are untouched; a camera is an
alternative to ``motion``, never combined with it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

EASES = ("linear", "ease_in", "ease_out", "ease_in_out")
DEFAULT_EASE = "ease_in_out"
MIN_ZOOM = 1.0
MAX_ZOOM = 3.0
_STATE_KEYS = ("zoom", "x", "y")


@dataclass(frozen=True)
class Camera:
    """From (zoom, x, y) to (zoom, x, y). x and y are where the centre of the visible window
    sits, as fractions of the picture (0 to 1); the window never leaves the picture."""

    from_zoom: float
    from_x: float
    from_y: float
    to_zoom: float
    to_x: float
    to_y: float
    ease: str = DEFAULT_EASE

    def key(self) -> list:
        return ["camera", self.from_zoom, self.from_x, self.from_y, self.to_zoom, self.to_x,
                self.to_y, self.ease]

    def describe(self) -> str:
        return (f"camera zoom {self.from_zoom:g}->{self.to_zoom:g}, "
                f"focus ({self.from_x:g},{self.from_y:g})->({self.to_x:g},{self.to_y:g}), {self.ease}")


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _state(raw: object, label: str, base: tuple[float, float, float], errors: list[str]):
    """Parse one end of a camera move; missing fields take ``base``."""
    if not isinstance(raw, dict):
        errors.append(f"{label}: must be an object like {{\"zoom\": 1.2, \"x\": 0.5, \"y\": 0.4}}")
        return None
    values = list(base)
    ok = True
    for key in raw:
        if key not in _STATE_KEYS:
            errors.append(f"{label}: unknown field: {key}")
    for index, key in enumerate(_STATE_KEYS):
        if key not in raw:
            continue
        value = raw[key]
        low, high = (MIN_ZOOM, MAX_ZOOM) if key == "zoom" else (0.0, 1.0)
        if not _is_number(value) or not low <= value <= high:
            errors.append(f"{label}.{key}: must be a number from {low:g} to {high:g}")
            ok = False
        else:
            values[index] = float(value)
    return tuple(values) if ok else None


def parse_camera(raw: object, label: str, errors: list[str]) -> Camera | None:
    """Validate {from, to, ease}. ``to`` is required; ``from`` defaults to the whole picture
    (zoom 1, centred); fields missing from ``to`` stay as in ``from``."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        errors.append(f"{label}: must be an object like {{\"to\": {{\"zoom\": 1.25}}}}")
        return None
    for key in raw:
        if key not in ("from", "to", "ease"):
            errors.append(f"{label}: unknown field: {key}")
    ease = raw.get("ease", DEFAULT_EASE)
    if ease not in EASES:
        errors.append(f"{label}.ease: must be one of {', '.join(EASES)}")
        ease = DEFAULT_EASE
    start = (1.0, 0.5, 0.5)
    if "from" in raw:
        start = _state(raw["from"], f"{label}.from", start, errors)
    if "to" not in raw:
        errors.append(f"{label}.to: required (where the camera ends up)")
        return None
    end = _state(raw["to"], f"{label}.to", start or (1.0, 0.5, 0.5), errors)
    if start is None or end is None:
        return None
    return Camera(*start, *end, ease)


def ease_expression(ease: str, progress: str) -> str:
    if ease == "linear":
        return progress
    if ease == "ease_in":
        return f"({progress})*({progress})"
    if ease == "ease_out":
        return f"(1-(1-({progress}))*(1-({progress})))"
    if ease == "ease_in_out":
        return f"(0.5-0.5*cos(PI*({progress})))"
    raise ValueError(f"unknown ease: {ease}")


def camera_expressions(camera: Camera, frames: int) -> tuple[str, str, str]:
    """(z, x, y) for ffmpeg zoompan. ``on`` is the output frame number. The window is
    clamped to the picture, and x and y use the same zoom expression rather than the
    ``zoom`` variable (which lags one frame behind)."""
    e = ease_expression(camera.ease, f"on/{frames}")
    z = f"{camera.from_zoom:.6f}+({camera.to_zoom - camera.from_zoom:.6f})*{e}"
    fx = f"({camera.from_x:.6f}+({camera.to_x - camera.from_x:.6f})*{e})"
    fy = f"({camera.from_y:.6f}+({camera.to_y - camera.from_y:.6f})*{e})"
    zoom = f"({z})"
    x = f"max(0,min(iw-iw/{zoom},{fx}*iw-iw/{zoom}/2))"
    y = f"max(0,min(ih-ih/{zoom},{fy}*ih-ih/{zoom}/2))"
    return z, x, y
