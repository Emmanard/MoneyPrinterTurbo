"""Manifest-driven scene pipeline: per-scene narration and per-scene visuals.

Reads ``scenes.json`` from a project folder, produces one audio file and one
visual clip per scene (each clip lasts exactly as long as the scene's real audio),
and renders through ``video_project`` so every scene is cached and re-rendered
individually. Captions, text cards, transitions, pauses and background music are
applied afterwards by ``scene_post`` as further cached stages; ``scene_text``
holds the caption and text-card layout. The schema is documented in
docs/scene-pipeline.md.

Run ``python -m app.services.scene_pipeline --help``.

Project layout::

    scenes.json      the scene manifest (filenames only)
    assets/          images and videos
    audio/           optional pre-made narration files
    .scene_cache/    generated narration and image clips
    render/          video_project store (revisions, artifacts)
    out/             the finished video
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from app.services import scene_text
from app.services.video_project import (
    _ID,
    ProjectError,
    RenderSettings,
    VideoProject,
)

SCHEMA_VERSION = 1
MAX_SCENES = 1000
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 100_000_000
MAX_SCENE_SECONDS = 3600
MIN_VIDEO_ASSET_SECONDS = 0.1
CLIP_MARGIN_FRAMES = 3
COMMAND_TIMEOUT = 600

IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"})
AUDIO_EXTENSIONS = frozenset({".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"})

# Image zoom engine and oversampling factor. Chosen from a jitter test on real
# stills; see docs/scene-pipeline.md.
ZOOM_ENGINE = "zoompan"
ZOOM_OVERSAMPLE = 2

MOTIONS = ("zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up", "pan_down", "static")
FITS = ("cover", "contain_blur")
# Fields that are part of the format but not implemented yet. Everything else in
# the schema is in use; the empty tuples keep the validation hooks in place.
RESERVED_MOTIONS: tuple[str, ...] = ()
RESERVED_FITS: tuple[str, ...] = ()
RESERVED_TOP_LEVEL = ("full_narration_audio",)
RESERVED_SCENE_FIELDS: tuple[str, ...] = ()
USED_TOP_LEVEL = frozenset({"schema_version", "project_id", "voice", "settings", "bgm", "scenes"})
USED_SCENE_FIELDS = frozenset({
    "id", "narration", "asset", "assets", "audio", "motion", "fit", "pause_after",
    "captions", "text_cards", "transition",
})
RENDER_SETTINGS = ("width", "height", "fps", "narration_volume")
TRIM_SETTINGS = ("trim_silence", "trim_padding")
TEXT_SETTINGS = ("captions", "text_card_style")
USED_SETTINGS = RENDER_SETTINGS + TRIM_SETTINGS + TEXT_SETTINGS
DEFAULT_SETTINGS = {"width": 1080, "height": 1920, "fps": 30, "narration_volume": 1.0}

# Several visuals in one scene.
MAX_SCENE_ASSETS = 50
ASSET_SUM_TOLERANCE = 0.25
MIN_OPEN_ASSET_SECONDS = 0.1
MAX_PAUSE_AFTER = 30.0
TRANSITION_TYPES = ("cut", "crossfade", "fade_black", "fade_white", "dissolve",
                    "wipe_left", "wipe_right", "slide_left", "slide_right")
DEFAULT_TRANSITION_SECONDS = 0.5
MIN_TRANSITION_SECONDS = 0.1
MAX_TRANSITION_SECONDS = 3.0
# ffmpeg's xfade produces no frame from 1-frame inputs, so shorter transitions
# are rejected.
MIN_TRANSITION_FRAMES = 2

# Background music.
DEFAULT_BGM_VOLUME = 0.25
DEFAULT_DUCK_AMOUNT = 12.0
MAX_DUCK_AMOUNT = 18.0
DEFAULT_DUCK_ATTACK_MS = 150.0
DEFAULT_DUCK_RELEASE_MS = 800.0

# Trailing silence is trimmed from every scene's audio, keeping a short pad.
DEFAULT_TRIM_PADDING = 0.15
MAX_TRIM_PADDING = 2.0
TRIM_THRESHOLD_DB = -50
MAX_TRIM_SECONDS = 600

Synthesizer = Callable[[str, str, float, Path], bool]


class PipelineError(ValueError):
    pass


class ManifestError(PipelineError):
    pass


@dataclass(frozen=True)
class AssetRef:
    """One visual inside a scene. ``start`` is the in-point inside a video file;
    ``duration`` is the time on the scene timeline (None = share what is left)."""

    file: str
    start: float = 0.0
    duration: float | None = None
    motion: str | None = None


@dataclass(frozen=True)
class Transition:
    type: str
    duration: float


@dataclass(frozen=True)
class Ducking:
    amount: float
    attack: float
    release: float


@dataclass(frozen=True)
class Bgm:
    file: str
    volume: float
    ducking: Ducking | None


@dataclass(frozen=True)
class Scene:
    id: str
    narration: str
    asset: str
    audio: str | None
    motion: str
    fit: str
    assets: tuple[AssetRef, ...] = ()
    pause_after: float = 0.0
    captions: dict | None = None
    text_cards: tuple = ()
    transition: Transition | None = None


@dataclass(frozen=True)
class TrimSettings:
    enabled: bool
    padding: float


@dataclass(frozen=True)
class SceneManifest:
    project_id: str
    voice_name: str | None
    voice_rate: float
    settings: RenderSettings
    trim: TrimSettings
    scenes: tuple[Scene, ...]
    captions: dict | None = None
    text_card_style: dict | None = None
    bgm: Bgm | None = None


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    kind: str
    width: int
    height: int
    duration: float


def _reserved(name: str) -> str:
    return f"{name}: reserved for a later phase, not implemented yet"


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _plain_filename(value: object, label: str, extensions: frozenset[str], folder: Path,
                    errors: list[str]) -> Path | None:
    if not isinstance(value, str) or not value:
        errors.append(f"{label}: must be a non-empty filename")
        return None
    if (
        value != os.path.basename(value)
        or "\\" in value
        or "\x00" in value
        or value.startswith(".")
        or len(value) > 255
    ):
        errors.append(f"{label}: must be a plain filename, not a path: {value!r}")
        return None
    if Path(value).suffix.lower() not in extensions:
        errors.append(
            f"{label}: unsupported extension for {value!r} "
            f"(allowed: {', '.join(sorted(extensions))})"
        )
        return None
    path = folder / value
    if path.is_symlink():
        errors.append(f"{label}: symbolic links are not allowed: {value!r}")
        return None
    if not path.is_file():
        errors.append(f"{label}: {value!r} not found in {folder.name}/")
        return None
    return path


def parse_manifest(raw: object, project: Path) -> SceneManifest:
    """Validate a decoded scenes.json. Every problem is reported together."""
    if not isinstance(raw, dict):
        raise ManifestError("scenes.json must contain a JSON object")
    errors: list[str] = []
    for key in raw:
        if key not in USED_TOP_LEVEL and key not in RESERVED_TOP_LEVEL:
            errors.append(f"unknown top-level field: {key}")
    for key in RESERVED_TOP_LEVEL:
        if raw.get(key) is not None:
            errors.append(_reserved(key))
    if raw.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    project_id = raw.get("project_id")
    if not isinstance(project_id, str) or not _ID.fullmatch(project_id):
        errors.append("project_id: 1-64 letters, digits, underscores or hyphens")
        project_id = "invalid"

    settings_raw = raw.get("settings", {})
    settings = None
    trim = TrimSettings(True, DEFAULT_TRIM_PADDING)
    caption_defaults: dict = {}
    card_defaults: dict = {}
    if not isinstance(settings_raw, dict):
        errors.append("settings: must be an object")
    else:
        for key in settings_raw:
            if key == "bgm_volume":
                errors.append("settings.bgm_volume: not used; set bgm.volume instead")
            elif key not in USED_SETTINGS:
                errors.append(f"settings: unknown field: {key}")
        caption_defaults = scene_text.parse_caption_spec(
            settings_raw.get("captions"), "settings.captions", errors) or {}
        card_defaults = scene_text.parse_style(
            settings_raw.get("text_card_style"), "settings.text_card_style", errors)
        try:
            settings = RenderSettings.parse(
                {**DEFAULT_SETTINGS, **{k: v for k, v in settings_raw.items() if k in RENDER_SETTINGS}}
            )
        except (ProjectError, TypeError) as exc:
            errors.append(f"settings: {exc}")
        enabled = settings_raw.get("trim_silence", True)
        padding = settings_raw.get("trim_padding", DEFAULT_TRIM_PADDING)
        if not isinstance(enabled, bool):
            errors.append("settings.trim_silence: must be true or false")
            enabled = True
        if not _is_number(padding) or not 0 <= padding <= MAX_TRIM_PADDING:
            errors.append(f"settings.trim_padding: must be a number from 0 to {MAX_TRIM_PADDING}")
            padding = DEFAULT_TRIM_PADDING
        trim = TrimSettings(enabled, float(padding))

    voice_name = None
    voice_rate = 1.0
    voice_raw = raw.get("voice")
    if voice_raw is not None:
        if not isinstance(voice_raw, dict):
            errors.append("voice: must be an object")
        else:
            for key in voice_raw:
                if key not in ("name", "rate"):
                    errors.append(f"voice: unknown field: {key}")
            name = voice_raw.get("name")
            if not isinstance(name, str) or not name.strip():
                errors.append("voice.name: must be a non-empty string")
            else:
                voice_name = name.strip()
            if "rate" in voice_raw:
                rate = voice_raw["rate"]
                if not _is_number(rate) or not 0.5 <= rate <= 2.0:
                    errors.append("voice.rate: must be a number from 0.5 to 2.0")
                else:
                    voice_rate = float(rate)

    bgm = _parse_bgm(raw.get("bgm"), project / "audio", errors)

    scenes: list[Scene] = []
    scenes_raw = raw.get("scenes")
    if not isinstance(scenes_raw, list) or not 1 <= len(scenes_raw) <= MAX_SCENES:
        errors.append(f"scenes: provide a list of 1 to {MAX_SCENES} scenes")
        scenes_raw = []
    seen: set[str] = set()
    assets_dir = project / "assets"
    audio_dir = project / "audio"
    for index, item in enumerate(scenes_raw):
        label = f"scenes[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{label}: must be an object")
            continue
        scene_id = item.get("id")
        if not isinstance(scene_id, str) or not _ID.fullmatch(scene_id):
            errors.append(f"{label}.id: 1-64 letters, digits, underscores or hyphens")
            scene_id = f"#{index}"
        elif scene_id in seen:
            errors.append(f"{label}.id: duplicate scene id {scene_id!r}")
        else:
            seen.add(scene_id)
        label = f"scene {scene_id}"
        for key in item:
            if key not in USED_SCENE_FIELDS and key not in RESERVED_SCENE_FIELDS:
                errors.append(f"{label}: unknown field: {key}")
        for key in RESERVED_SCENE_FIELDS:
            if item.get(key) is not None:
                errors.append(f"{label}: {_reserved(key)}")
        motion = item.get("motion", "zoom_in")
        if motion in RESERVED_MOTIONS:
            errors.append(f"{label}: {_reserved('motion ' + repr(motion))}")
        elif motion not in MOTIONS:
            errors.append(f"{label}.motion: must be one of {', '.join(MOTIONS)}")
        fit = item.get("fit", "cover")
        if fit in RESERVED_FITS:
            errors.append(f"{label}: {_reserved('fit ' + repr(fit))}")
        elif fit not in FITS:
            errors.append(f"{label}.fit: must be one of {', '.join(FITS)}")
        narration = item.get("narration", "")
        if not isinstance(narration, str) or len(narration) > 100000:
            errors.append(f"{label}.narration: must be text under 100000 characters")
            narration = ""
        asset = item.get("asset")
        assets: tuple[AssetRef, ...] = ()
        if item.get("assets") is not None and asset is not None:
            errors.append(f"{label}: set either asset or assets, not both")
        elif item.get("assets") is not None:
            assets = _parse_assets(item["assets"], label, assets_dir, errors)
        else:
            _plain_filename(asset, f"{label}.asset", IMAGE_EXTENSIONS | VIDEO_EXTENSIONS,
                            assets_dir, errors)
        audio = item.get("audio")
        if audio is not None:
            _plain_filename(audio, f"{label}.audio", AUDIO_EXTENSIONS, audio_dir, errors)
        elif not narration.strip():
            errors.append(f"{label}: needs narration text or an audio file")
        if audio is None and voice_name is None:
            errors.append(f"{label}: no audio file, so voice.name is required for TTS")
        pause_after = item.get("pause_after", 0)
        if not _is_number(pause_after) or not 0 <= pause_after <= MAX_PAUSE_AFTER:
            errors.append(f"{label}.pause_after: must be a number from 0 to {MAX_PAUSE_AFTER:g} seconds")
            pause_after = 0
        captions = scene_text.parse_caption_spec(item.get("captions"), f"{label}.captions", errors)
        cards = scene_text.parse_text_cards(item.get("text_cards"), label, errors)
        transition = _parse_transition(
            item.get("transition"), label, errors,
            settings.fps if settings is not None else DEFAULT_SETTINGS["fps"],
        )
        if transition is not None and transition.type != "cut" and index == len(scenes_raw) - 1:
            errors.append(f"{label}.transition: the last scene has no next scene to transition to")
        if scene_text.merge_caption(caption_defaults, captions)["enabled"] and not narration.strip():
            errors.append(f"{label}.captions: captions need narration text; turn them off for this scene")
        scenes.append(
            Scene(
                id=scene_id,
                narration=narration,
                asset=asset if isinstance(asset, str) else "",
                audio=audio if isinstance(audio, str) else None,
                motion=motion if isinstance(motion, str) else "zoom_in",
                fit=fit if isinstance(fit, str) else "cover",
                assets=assets,
                pause_after=float(pause_after),
                captions=captions,
                text_cards=cards,
                transition=transition,
            )
        )

    for folder in (assets_dir, audio_dir):
        if folder.is_symlink():
            errors.append(f"{folder.name}/: must be a real directory, not a symbolic link")
    if errors or settings is None:
        raise ManifestError("\n".join(dict.fromkeys(errors)))
    return SceneManifest(project_id, voice_name, voice_rate, settings, trim, tuple(scenes),
                         caption_defaults, card_defaults, bgm)


def _parse_assets(raw: object, label: str, folder: Path, errors: list[str]) -> tuple[AssetRef, ...]:
    """Validate scenes[].assets: files exist, numbers are sane, and at most one
    entry per scene can be left to share the remaining time unevenly."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_SCENE_ASSETS:
        errors.append(f"{label}.assets: must be a list of 1 to {MAX_SCENE_ASSETS} entries")
        return ()
    refs = []
    for index, item in enumerate(raw):
        name = f"{label}.assets[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{name}: must be an object")
            continue
        for key in item:
            if key not in ("file", "start", "duration", "motion"):
                errors.append(f"{name}: unknown field: {key}")
        file = item.get("file")
        _plain_filename(file, f"{name}.file", IMAGE_EXTENSIONS | VIDEO_EXTENSIONS, folder, errors)
        start = item.get("start", 0)
        if not _is_number(start) or start < 0:
            errors.append(f"{name}.start: must be a number of seconds, 0 or more")
            start = 0
        elif start > 0 and isinstance(file, str) and Path(file).suffix.lower() in IMAGE_EXTENSIONS:
            errors.append(f"{name}.start: only applies to video files, not images")
        duration = item.get("duration")
        if duration is not None and (not _is_number(duration) or not 0 < duration <= MAX_SCENE_SECONDS):
            errors.append(f"{name}.duration: must be a number of seconds above 0")
            duration = None
        motion = item.get("motion")
        if motion is not None and motion not in MOTIONS:
            errors.append(f"{name}.motion: must be one of {', '.join(MOTIONS)}")
            motion = None
        refs.append(AssetRef(file if isinstance(file, str) else "", float(start),
                             None if duration is None else float(duration), motion))
    return tuple(refs)


def _parse_transition(raw: object, label: str, errors: list[str], fps: int) -> Transition | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        errors.append(f"{label}.transition: must be an object")
        return None
    for key in raw:
        if key not in ("type", "duration"):
            errors.append(f"{label}.transition: unknown field: {key}")
    kind = raw.get("type")
    if kind not in TRANSITION_TYPES:
        errors.append(f"{label}.transition.type: must be one of {', '.join(TRANSITION_TYPES)}")
        return None
    duration = raw.get("duration", DEFAULT_TRANSITION_SECONDS)
    if kind == "cut":
        return Transition("cut", 0.0)
    if not _is_number(duration) or not MIN_TRANSITION_SECONDS <= duration <= MAX_TRANSITION_SECONDS:
        errors.append(
            f"{label}.transition.duration: must be a number from {MIN_TRANSITION_SECONDS:g} "
            f"to {MAX_TRANSITION_SECONDS:g} seconds"
        )
        return None
    if round(duration * fps) < MIN_TRANSITION_FRAMES:
        errors.append(
            f"{label}.transition.duration: {duration:g}s is under {MIN_TRANSITION_FRAMES} frames at "
            f"{fps} fps; the minimum for this frame rate is {MIN_TRANSITION_FRAMES / fps:.3g}s"
        )
        return None
    return Transition(kind, float(duration))


def _parse_bgm(raw: object, folder: Path, errors: list[str]) -> Bgm | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        errors.append("bgm: must be an object like {file, volume, ducking}")
        return None
    for key in raw:
        if key not in ("file", "volume", "ducking"):
            errors.append(f"bgm: unknown field: {key}")
    file = raw.get("file")
    _plain_filename(file, "bgm.file", AUDIO_EXTENSIONS, folder, errors)
    volume = raw.get("volume", DEFAULT_BGM_VOLUME)
    if not _is_number(volume) or not 0 < volume <= 1:
        errors.append("bgm.volume: must be a number above 0 and at most 1")
        volume = DEFAULT_BGM_VOLUME
    ducking = None
    ducking_raw = raw.get("ducking")
    if ducking_raw is not None:
        if not isinstance(ducking_raw, dict):
            errors.append("bgm.ducking: must be an object like {amount, attack, release}")
        else:
            for key in ducking_raw:
                if key not in ("amount", "attack", "release"):
                    errors.append(f"bgm.ducking: unknown field: {key}")
            amount = ducking_raw.get("amount", DEFAULT_DUCK_AMOUNT)
            attack = ducking_raw.get("attack", DEFAULT_DUCK_ATTACK_MS)
            release = ducking_raw.get("release", DEFAULT_DUCK_RELEASE_MS)
            if not _is_number(amount) or not 0 <= amount <= MAX_DUCK_AMOUNT:
                errors.append(f"bgm.ducking.amount: must be a number of dB from 0 to {MAX_DUCK_AMOUNT:g}")
                amount = DEFAULT_DUCK_AMOUNT
            if not _is_number(attack) or not 1 <= attack <= 2000:
                errors.append("bgm.ducking.attack: must be a number of milliseconds from 1 to 2000")
                attack = DEFAULT_DUCK_ATTACK_MS
            if not _is_number(release) or not 10 <= release <= 9000:
                errors.append("bgm.ducking.release: must be a number of milliseconds from 10 to 9000")
                release = DEFAULT_DUCK_RELEASE_MS
            ducking = Ducking(float(amount), float(attack), float(release))
    return Bgm(file if isinstance(file, str) else "", float(volume), ducking)


def load_manifest(project: Path) -> SceneManifest:
    path = project / "scenes.json"
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise ManifestError("scenes.json is too large")
        with path.open(encoding="utf-8") as source:
            raw = json.load(source)
    except FileNotFoundError as exc:
        raise ManifestError(f"scenes.json not found in {project}") from exc
    except (OSError, ValueError) as exc:
        if isinstance(exc, ManifestError):
            raise
        raise ManifestError(f"scenes.json is not readable JSON: {exc}") from exc
    return parse_manifest(raw, project)


def _run(args: list[str], what: str, timeout: float = COMMAND_TIMEOUT) -> str:
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PipelineError(f"{what}: {exc}") from exc
    if done.returncode:
        raise PipelineError(f"{what} failed: {done.stderr[-2000:].strip()}")
    return done.stdout


def _ffprobe(path: Path, select: str, entries: str) -> dict:
    out = _run(
        [
            "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
            "-select_streams", select, "-show_entries", entries, "-of", "json",
            str(path),
        ],
        f"ffprobe {path.name}",
        timeout=60,
    )
    try:
        return json.loads(out or "{}")
    except ValueError as exc:
        raise PipelineError(f"ffprobe {path.name}: unreadable output") from exc


def _duration_of(probe: dict, name: str) -> float:
    try:
        value = float(probe["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PipelineError(f"{name}: duration is unknown") from exc
    if not math.isfinite(value) or not 0 < value <= MAX_SCENE_SECONDS:
        raise PipelineError(
            f"{name}: duration must be between 0 and {MAX_SCENE_SECONDS} seconds"
        )
    return value


def probe_audio(path: Path) -> float:
    """Return the real duration of an audio file, in seconds."""
    probe = _ffprobe(path, "a:0", "stream=codec_type:format=duration")
    if not probe.get("streams"):
        raise PipelineError(f"{path.name}: has no audio stream")
    return _duration_of(probe, path.name)


def _display_rotation(path: Path, stream: dict) -> int:
    """Degrees ffmpeg turns a picture before filtering, from its display matrix.

    Videos carry it as stream side data. A JPEG's EXIF orientation is reported only
    on its decoded frame, so images get a second, one-frame probe."""
    for item in stream.get("side_data_list") or []:
        if "rotation" in item:
            return int(round(float(item["rotation"])))
    if path.suffix.lower() in IMAGE_EXTENSIONS:
        out = _run(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
                    "-select_streams", "v:0", "-read_intervals", "%+#1",
                    "-show_entries", "frame=width,height:frame_side_data=rotation",
                    "-of", "json", str(path)], f"ffprobe {path.name}", timeout=60)
        for frame in json.loads(out or "{}").get("frames") or []:
            for item in frame.get("side_data_list") or []:
                if "rotation" in item:
                    return int(round(float(item["rotation"])))
    return 0


def probe_visual(path: Path) -> MediaInfo:
    probe = _ffprobe(path, "v:0", "stream=width,height:stream_side_data=rotation:format=duration")
    streams = probe.get("streams") or []
    if not streams:
        raise PipelineError(f"{path.name}: has no readable video or image stream")
    width = int(streams[0].get("width") or 0)
    height = int(streams[0].get("height") or 0)
    if _display_rotation(path, streams[0]) % 180 != 0:
        width, height = height, width  # ffmpeg shows the picture turned by 90 or 270 degrees
    if width < 2 or height < 2:
        raise PipelineError(f"{path.name}: unreadable dimensions")
    if width * height > MAX_IMAGE_PIXELS:
        raise PipelineError(f"{path.name}: larger than {MAX_IMAGE_PIXELS} pixels")
    kind = "image" if path.suffix.lower() in IMAGE_EXTENSIONS else "video"
    duration = 0.0
    if kind == "video":
        duration = _duration_of(probe, path.name)
        if duration < MIN_VIDEO_ASSET_SECONDS:
            raise PipelineError(f"{path.name}: video is shorter than {MIN_VIDEO_ASSET_SECONDS}s")
    return MediaInfo(path, kind, width, height, duration)


def scene_files(scene: Scene) -> list[str]:
    """The visual files a scene uses, in playing order."""
    return [ref.file for ref in scene.assets] if scene.assets else [scene.asset]


def inspect_media(manifest: SceneManifest, project: Path) -> dict[str, MediaInfo]:
    """Probe every visual asset once; report all unreadable ones together."""
    infos: dict[str, MediaInfo] = {}
    errors: list[str] = []
    for scene in manifest.scenes:
        for name in scene_files(scene):
            if name in infos:
                continue
            try:
                infos[name] = probe_visual(project / "assets" / name)
            except PipelineError as exc:
                errors.append(f"scene {scene.id}: {exc}")
        for ref in scene.assets:
            info = infos.get(ref.file)
            if info is not None and info.kind == "video" and ref.start >= info.duration:
                errors.append(
                    f"scene {scene.id}: {ref.file} is {info.duration:.2f}s long but its start is "
                    f"{ref.start:g}s"
                )
    if manifest.bgm is not None:
        try:
            probe_audio(project / "audio" / manifest.bgm.file)
        except PipelineError as exc:
            errors.append(f"bgm: {exc}")
    if errors:
        raise ManifestError("\n".join(errors))
    return infos


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _key(*parts: object) -> str:
    return hashlib.sha256(
        json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def tts_cache_path(project: Path, voice_name: str, rate: float, text: str) -> Path:
    return project / ".scene_cache" / "tts" / f"{_key('tts-v1', voice_name, float(rate), text)}.mp3"


def _synthesize(text: str, voice_name: str, rate: float, output: Path) -> bool:
    """Generate narration with the existing TTS code. Imported lazily because
    the voice module loads the whole application configuration."""
    from app.services import voice

    sub_maker = voice.tts(
        text=text,
        voice_name=voice.parse_voice_name(voice_name),
        voice_rate=rate,
        voice_file=str(output),
    )
    return sub_maker is not None


def audio_source(project: Path, manifest: SceneManifest, scene: Scene) -> tuple[str, Path]:
    """Return ("premade" | "cached" | "tts", path). For "tts" the path is the
    cache location that generation will fill."""
    if scene.audio:
        return "premade", project / "audio" / scene.audio
    path = tts_cache_path(project, manifest.voice_name or "", manifest.voice_rate, scene.narration)
    if path.is_file() and path.stat().st_size > 0:
        return "cached", path
    return "tts", path


def ensure_scene_audio(project: Path, manifest: SceneManifest, scene: Scene,
                       synthesize: Synthesizer) -> tuple[str, Path, float]:
    source, path = audio_source(project, manifest, scene)
    if source == "tts":
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, staged_name = tempfile.mkstemp(prefix=".tts-", suffix=".mp3", dir=path.parent)
        os.close(descriptor)
        staged = Path(staged_name)
        try:
            if not synthesize(scene.narration, manifest.voice_name or "", manifest.voice_rate, staged):
                raise PipelineError("narration synthesis failed; check the voice name and network")
            probe_audio(staged)
            os.replace(staged, path)
        finally:
            staged.unlink(missing_ok=True)
    duration = probe_audio(path)
    if manifest.trim.enabled:
        path, duration = ensure_trimmed_audio(project, path, manifest.trim.padding)
    return source, path, duration


def trim_trailing_silence(source: Path, output: Path, padding: float) -> None:
    """Cut silence from the end of ``source``, keeping ``padding`` seconds of it.

    The audio is reversed so the trailing silence becomes leading silence, which
    silenceremove handles directly. Reversal buffers the audio in memory, so
    scenes are limited to MAX_TRIM_SECONDS.
    """
    if probe_audio(source) > MAX_TRIM_SECONDS:
        raise PipelineError(
            f"{source.name}: longer than {MAX_TRIM_SECONDS}s; split the scene or set "
            "settings.trim_silence to false"
        )
    chain = (
        "aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=stereo,areverse,"
        f"silenceremove=start_periods=1:start_threshold={TRIM_THRESHOLD_DB}dB:"
        f"start_silence={padding:.4f},areverse"
    )
    _run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-threads", "1",
            "-filter_threads", "1", "-protocol_whitelist", "file,pipe", "-i", str(source),
            "-vn", "-af", chain, "-c:a", "pcm_s16le", "-f", "wav", str(output),
        ],
        f"silence trim for {source.name}",
    )


def ensure_trimmed_audio(project: Path, source: Path, padding: float) -> tuple[Path, float]:
    key = _key("trim-v1", _sha256_file(source), f"{padding:.4f}", TRIM_THRESHOLD_DB)
    folder = project / ".scene_cache" / "audio"
    final = folder / f"{key}.wav"
    if not (final.is_file() and final.stat().st_size > 0):
        folder.mkdir(parents=True, exist_ok=True)
        descriptor, staged_name = tempfile.mkstemp(prefix=".trim-", suffix=".wav", dir=folder)
        os.close(descriptor)
        staged = Path(staged_name)
        try:
            trim_trailing_silence(source, staged, padding)
            try:
                probe_audio(staged)
            except PipelineError as exc:
                raise PipelineError(f"{source.name}: audio is silent or could not be trimmed ({exc})") from exc
            os.replace(staged, final)
        finally:
            staged.unlink(missing_ok=True)
    return final, probe_audio(final)


def _zoom_total(duration: float) -> float:
    """Total zoom over the scene: slow, and bounded for very long scenes."""
    return min(0.15, max(0.04, 0.03 * duration))


def _even(value: float) -> int:
    return max(2, int(value) // 2 * 2)


def build_zoom_filter(width: int, height: int, fps: int, frames: int, motion: str,
                      engine: str, oversample: int) -> str:
    cover = f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,crop={width}:{height}"
    tail = "setsar=1,format=yuv420p"
    if motion == "static":
        return f"{cover},{tail}"
    total = _zoom_total(frames / fps)
    big_w, big_h = _even(width * oversample), _even(height * oversample)
    big = (
        f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={big_w}:{big_h}"
    )
    if engine == "zoompan":
        z, x, y = _zoompan_motion(motion, total, frames)
        return (
            f"{big},zoompan=z='{z}':"
            f"x='{x}':y='{y}':d={frames}:s={width}x{height}:fps={fps},{tail}"
        )
    if engine == "scale_crop":
        seconds = frames / fps
        factor, crop_x, crop_y = _scale_crop_motion(motion, total, seconds, big_w, big_h)
        return (
            f"{big},scale=w='iw*{factor}':"
            f"h='ih*{factor}':eval=frame:flags=bicubic,"
            f"crop={big_w}:{big_h}:'{crop_x}':'{crop_y}',"
            f"scale={width}:{height}:flags=lanczos,{tail}"
        )
    raise PipelineError(f"unknown zoom engine: {engine}")


# A pan needs a window smaller than the picture, so pans hold a fixed zoom.
PAN_MIN_ZOOM = 0.10


def _zoompan_motion(motion: str, total: float, frames: int) -> tuple[str, str, str]:
    """(z, x, y) expressions for ffmpeg zoompan. ``on`` is the output frame number."""
    center_x = "iw/2-(iw/zoom/2)"
    center_y = "ih/2-(ih/zoom/2)"
    if motion == "zoom_in":
        return f"1+{total:.6f}*on/{frames}", center_x, center_y
    if motion == "zoom_out":
        return f"1+{total:.6f}-{total:.6f}*on/{frames}", center_x, center_y
    zoom = f"{1 + max(total, PAN_MIN_ZOOM):.6f}"
    progress = f"on/{frames}"
    if motion == "pan_right":
        return zoom, f"(iw-iw/zoom)*{progress}", center_y
    if motion == "pan_left":
        return zoom, f"(iw-iw/zoom)*(1-{progress})", center_y
    if motion == "pan_down":
        return zoom, center_x, f"(ih-ih/zoom)*{progress}"
    if motion == "pan_up":
        return zoom, center_x, f"(ih-ih/zoom)*(1-{progress})"
    raise PipelineError(f"unknown motion: {motion}")


def _scale_crop_motion(motion: str, total: float, seconds: float, big_w: int,
                       big_h: int) -> tuple[str, str, str]:
    """(scale factor, crop x, crop y) expressions for the scale_crop engine.

    The scale filter grows every frame, but crop's iw/ih keep the size the stream
    started with, so (iw-ow)/2 would always be 0 and the window would stick to the
    top-left corner. When the size changes, centre from the known sizes instead.
    """
    center_x = "(iw-ow)/2"
    center_y = "(ih-oh)/2"
    if motion == "zoom_in":
        grow = f"{total:.6f}*t/{seconds:.6f}"
        return f"(1+{grow})", f"{big_w}*({grow})/2", f"{big_h}*({grow})/2"
    if motion == "zoom_out":
        shrink = f"({total:.6f}-{total:.6f}*t/{seconds:.6f})"
        return f"(1+{shrink})", f"{big_w}*{shrink}/2", f"{big_h}*{shrink}/2"
    factor = f"{1 + max(total, PAN_MIN_ZOOM):.6f}"
    progress = f"t/{seconds:.6f}"
    if motion == "pan_right":
        return factor, f"(iw-ow)*{progress}", center_y
    if motion == "pan_left":
        return factor, f"(iw-ow)*(1-{progress})", center_y
    if motion == "pan_down":
        return factor, center_x, f"(ih-oh)*{progress}"
    if motion == "pan_up":
        return factor, center_x, f"(ih-oh)*(1-{progress})"
    raise PipelineError(f"unknown motion: {motion}")


def render_image_clip(image: Path, output: Path, *, duration: float, width: int, height: int,
                      fps: int, motion: str = "zoom_in", engine: str = ZOOM_ENGINE,
                      oversample: int = ZOOM_OVERSAMPLE) -> None:
    """Render a still to an H.264 clip that lasts at least ``duration`` seconds.

    Fractional durations are supported: the clip gets ceil(duration * fps) frames
    plus a small margin, and video_project trims it to the exact audio length.
    """
    if not math.isfinite(duration) or duration <= 0:
        raise PipelineError("clip duration must be positive")
    frames = max(2, math.ceil(duration * fps) + CLIP_MARGIN_FRAMES)
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-threads", "1", "-filter_threads", "1", "-protocol_whitelist", "file,pipe",
    ]
    if motion == "static" or engine != "zoompan":
        command += ["-loop", "1", "-framerate", str(fps)]
    command += [
        "-i", str(image),
        "-vf", build_zoom_filter(width, height, fps, frames, motion, engine, oversample),
        "-frames:v", str(frames), "-r", str(fps), "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(output),
    ]
    _run(command, f"image clip for {image.name}")


def ensure_image_clip(project: Path, image: Path, *, duration: float, settings: RenderSettings,
                      motion: str) -> tuple[Path, bool]:
    key = _key("clip-v1", _sha256_file(image), f"{duration:.4f}", settings.width,
               settings.height, settings.fps, motion, ZOOM_ENGINE, ZOOM_OVERSAMPLE)
    folder = project / ".scene_cache" / "clips"
    final = folder / f"{key}.mp4"
    if final.is_file() and final.stat().st_size > 0:
        return final, False
    folder.mkdir(parents=True, exist_ok=True)
    descriptor, staged_name = tempfile.mkstemp(prefix=".clip-", suffix=".mp4", dir=folder)
    os.close(descriptor)
    staged = Path(staged_name)
    try:
        render_image_clip(image, staged, duration=duration, width=settings.width,
                          height=settings.height, fps=settings.fps, motion=motion)
        os.replace(staged, final)
    finally:
        staged.unlink(missing_ok=True)
    return final, True


# -- scenes beyond one asset with the default look ------------------------

def legacy_visual(scene: Scene, info: MediaInfo) -> bool:
    """True for the original single-asset path: one cover-fitted image with a
    zoom_in or static look, or any single video. That path is unchanged."""
    return (
        not scene.assets
        and scene.fit == "cover"
        and (info.kind == "video" or scene.motion in ("zoom_in", "static"))
    )


def plan_asset_durations(refs: tuple[AssetRef, ...], total: float) -> list[float]:
    """Seconds each visual plays on the scene timeline.

    * Assets with a duration keep it; assets without one share what is left equally.
    * If every asset has a duration, their sum must match the scene's audio length
      within ASSET_SUM_TOLERANCE seconds; the last asset absorbs the difference.
      Beyond the tolerance, or when nothing is left for assets without a duration,
      the scene is rejected before any rendering.
    """
    given = sum(ref.duration for ref in refs if ref.duration is not None)
    open_count = sum(1 for ref in refs if ref.duration is None)
    if open_count == 0:
        difference = total - given
        if abs(difference) > ASSET_SUM_TOLERANCE:
            raise PipelineError(
                f"asset durations add up to {given:.2f}s but the scene lasts {total:.2f}s "
                f"(allowed difference {ASSET_SUM_TOLERANCE:g}s); change a duration, or leave "
                "one out to fill the rest"
            )
        seconds = [float(ref.duration) for ref in refs]
        seconds[-1] += difference
        if seconds[-1] < MIN_OPEN_ASSET_SECONDS:
            raise PipelineError("asset durations leave no time for the last asset")
        return seconds
    left = total - given
    if left < MIN_OPEN_ASSET_SECONDS * open_count:
        raise PipelineError(
            f"asset durations add up to {given:.2f}s, leaving {max(left, 0):.2f}s for "
            f"{open_count} asset(s) without a duration in a {total:.2f}s scene"
        )
    share = left / open_count
    return [float(ref.duration) if ref.duration is not None else share for ref in refs]


def allocate_frames(seconds: list[float], total_frames: int, fps: int) -> list[int]:
    """Whole frames per visual; the last one takes the rounding and the margin."""
    frames = [max(1, round(value * fps)) for value in seconds[:-1]]
    last = total_frames - sum(frames)
    if last < 1:
        raise PipelineError("asset durations leave no time for the last asset")
    return [*frames, last]


def _contain_blur_filter(info: MediaInfo, frames: int, motion: str, settings: RenderSettings) -> str:
    """The whole picture inside the frame over a blurred, enlarged copy of itself."""
    width, height, fps = settings.width, settings.height, settings.fps
    scale = min(width / info.width, height / info.height)
    fit_w, fit_h = _even(info.width * scale), _even(info.height * scale)
    blur_w, blur_h = _even(width / 4), _even(height / 4)
    background = (
        f"[bg]scale={blur_w}:{blur_h}:force_original_aspect_ratio=increase:flags=bicubic,"
        f"crop={blur_w}:{blur_h},boxblur=8:2,scale={width}:{height}:flags=bicubic,"
        "eq=brightness=-0.06[b]"
    )
    if info.kind == "image" and motion != "static":
        z, x, y = _zoompan_motion(motion, _zoom_total(frames / fps), frames)
        foreground = (
            f"[fg]scale={_even(fit_w * ZOOM_OVERSAMPLE)}:{_even(fit_h * ZOOM_OVERSAMPLE)}:"
            f"flags=lanczos,zoompan=z='{z}':x='{x}':y='{y}':d=1:s={fit_w}x{fit_h}:fps={fps}[f]"
        )
    else:
        foreground = f"[fg]scale={fit_w}:{fit_h}:flags=lanczos[f]"
    return (
        f"split[bg][fg];{background};{foreground};"
        f"[b][f]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={fps},format=yuv420p"
    )


def render_asset_clip(info: MediaInfo, output: Path, *, frames: int, start: float, motion: str,
                      fit: str, settings: RenderSettings) -> None:
    """Render one visual to exactly ``frames`` frames of H.264 (no B-frames, so
    clips can be joined by stream copy). Videos loop from the start unless an
    in-point is given; motion applies to images only."""
    width, height, fps = settings.width, settings.height, settings.fps
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-threads", "1", "-filter_threads", "1", "-protocol_whitelist", "file,pipe",
    ]
    if info.kind == "image":
        if fit == "contain_blur":
            graph = _contain_blur_filter(info, frames, motion, settings)
            command += ["-loop", "1", "-framerate", str(fps)]
        else:
            graph = build_zoom_filter(width, height, fps, frames, motion, ZOOM_ENGINE, ZOOM_OVERSAMPLE)
            if motion == "static" or ZOOM_ENGINE != "zoompan":
                command += ["-loop", "1", "-framerate", str(fps)]
    else:
        command += ["-ss", f"{start:.3f}"] if start > 0 else ["-stream_loop", "-1"]
        if fit == "contain_blur":
            graph = _contain_blur_filter(info, frames, "static", settings)
        else:
            graph = (
                f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={width}:{height},setsar=1,fps={fps},format=yuv420p"
            )
    command += [
        "-i", str(info.path), "-vf", graph, "-frames:v", str(frames), "-r", str(fps), "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", "-bf", "0",
        "-movflags", "+faststart", str(output),
    ]
    _run(command, f"clip for {info.path.name}")


def ensure_asset_clip(project: Path, info: MediaInfo, *, frames: int, start: float, motion: str,
                      fit: str, settings: RenderSettings) -> tuple[Path, bool]:
    key = _key("aclip-v1", _sha256_file(info.path), info.kind, f"{start:.3f}", frames,
               motion if info.kind == "image" else None, fit, settings.width, settings.height,
               settings.fps, ZOOM_ENGINE, ZOOM_OVERSAMPLE)
    folder = project / ".scene_cache" / "clips"
    final = folder / f"{key}.mp4"
    if final.is_file() and final.stat().st_size > 0:
        return final, False
    folder.mkdir(parents=True, exist_ok=True)
    descriptor, staged_name = tempfile.mkstemp(prefix=".clip-", suffix=".mp4", dir=folder)
    os.close(descriptor)
    staged = Path(staged_name)
    try:
        render_asset_clip(info, staged, frames=frames, start=start, motion=motion, fit=fit,
                          settings=settings)
        os.replace(staged, final)
    finally:
        staged.unlink(missing_ok=True)
    return final, True


def ensure_joined_footage(clips: list[Path]) -> tuple[Path, bool]:
    """Join a scene's clips, in order, into one footage file by stream copy."""
    folder = clips[0].parent
    key = _key("joined-v1", [clip.stem for clip in clips])
    final = folder / f"{key}.mp4"
    if final.is_file() and final.stat().st_size > 0:
        return final, False
    listing = folder / f"{key}.txt"
    listing.write_text("".join(f"file '{clip.name}'\n" for clip in clips), encoding="utf-8")
    descriptor, staged_name = tempfile.mkstemp(prefix=".join-", suffix=".mp4", dir=folder)
    os.close(descriptor)
    staged = Path(staged_name)
    try:
        _run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "concat",
             "-safe", "1", "-protocol_whitelist", "file,pipe", "-i", str(listing), "-c", "copy",
             "-an", "-movflags", "+faststart", str(staged)],
            "joining the clips of a scene",
        )
        os.replace(staged, final)
    finally:
        staged.unlink(missing_ok=True)
    return final, True


def ensure_scene_visual(project: Path, manifest: SceneManifest, scene: Scene,
                        infos: dict[str, MediaInfo], duration: float) -> tuple[Path, int]:
    """Footage for scenes outside the original single-asset path: several
    visuals in sequence, other motions, or contain_blur. Returns (footage, clips
    rendered). The footage lasts at least ``duration`` seconds plus a margin."""
    settings = manifest.settings
    refs = scene.assets or (AssetRef(scene.asset),)
    seconds = plan_asset_durations(refs, duration)
    total = max(2, math.ceil(duration * settings.fps) + CLIP_MARGIN_FRAMES)
    counts = allocate_frames(seconds, total, settings.fps)
    clips: list[Path] = []
    rendered = 0
    for ref, count in zip(refs, counts):
        info = infos[ref.file]
        if info.kind == "video" and ref.start > 0 and ref.start + count / settings.fps > info.duration + 0.05:
            raise PipelineError(
                f"{ref.file} has {max(info.duration - ref.start, 0):.2f}s after its start of "
                f"{ref.start:g}s but {count / settings.fps:.2f}s are needed; a video only loops "
                "when it starts at 0"
            )
        clip, created = ensure_asset_clip(
            project, info, frames=count, start=ref.start, motion=ref.motion or scene.motion,
            fit=scene.fit, settings=settings,
        )
        clips.append(clip)
        rendered += int(created)
    if len(clips) == 1:
        return clips[0], rendered
    footage, joined = ensure_joined_footage(clips)
    return footage, rendered + int(joined)


def uses_post(manifest: SceneManifest) -> bool:
    """True when the manifest needs the post stages: captions, text cards,
    transitions, pauses or music. Without any of them the original path runs."""
    if manifest.bgm is not None:
        return True
    for scene in manifest.scenes:
        if scene.pause_after > 0 or scene.text_cards:
            return True
        if scene.transition is not None and scene.transition.type != "cut":
            return True
        if scene_text.merge_caption(manifest.captions, scene.captions)["enabled"]:
            return True
    return False


def needs_network(project: Path) -> bool:
    """True when at least one scene still needs narration generated."""
    manifest = load_manifest(project)
    return any(audio_source(project, manifest, scene)[0] == "tts" for scene in manifest.scenes)


def plan_report(project: Path) -> list[dict]:
    manifest = load_manifest(project)
    infos = inspect_media(manifest, project)
    report = []
    for scene in manifest.scenes:
        source, path = audio_source(project, manifest, scene)
        visual = "multi" if scene.assets else infos[scene.asset].kind
        report.append({"scene": scene.id, "audio": source, "visual": visual,
                       "audio_file": path.name})
    return report


def _say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def build(project: Path, *, synthesize: Synthesizer | None = None,
          progress: Callable[[str], None] = _say) -> dict:
    """Prepare every scene, render through video_project, and publish out/<id>.mp4."""
    synthesize = synthesize or _synthesize
    manifest = load_manifest(project)
    infos = inspect_media(manifest, project)
    scenes_request = []
    durations: list[float] = []
    generated = 0
    clips = 0
    total = len(manifest.scenes)
    for number, scene in enumerate(manifest.scenes, start=1):
        try:
            source, audio_path, duration = ensure_scene_audio(project, manifest, scene, synthesize)
            durations.append(duration)
            info = None if scene.assets else infos[scene.asset]
            if info is not None and legacy_visual(scene, info):
                if info.kind == "image":
                    footage, rendered = ensure_image_clip(
                        project, info.path, duration=duration, settings=manifest.settings,
                        motion=scene.motion,
                    )
                    clips += int(rendered)
                else:
                    footage = info.path
            else:
                footage, rendered = ensure_scene_visual(project, manifest, scene, infos, duration)
                clips += rendered
        except PipelineError as exc:
            raise PipelineError(f"scene {scene.id}: {exc}") from exc
        generated += int(source == "tts")
        scenes_request.append({"id": scene.id, "footage": str(footage), "audio": str(audio_path),
                               "narration": scene.narration})
        kind = "multi" if info is None else info.kind
        progress(f"[{number}/{total}] {scene.id}: audio {source}, {duration:.2f}s, {kind}")

    post_stages = uses_post(manifest)
    if post_stages:
        from app.services import scene_post

        scene_post.validate_timeline(manifest, durations)

    cache = project / ".scene_cache"
    cache.mkdir(exist_ok=True)
    request = {"project_id": manifest.project_id, "settings": asdict(manifest.settings),
               "scenes": scenes_request}
    manifest_path = cache / "project_manifest.json"
    manifest_path.write_text(json.dumps(request, indent=2), encoding="utf-8")

    renderer = VideoProject(project / "render")
    spec = renderer.create_revision(request, relative_to=cache)
    status = renderer.render(
        spec["revision_id"],
        on_stage=lambda stage, reused: progress(
            f"  {stage.key}: {'reused' if reused else 'building'}"
        ) if stage.kind in ("assembly", "export") else None,
    )
    result = {
        "revision": spec["revision_id"],
        "scenes": total,
        "narration_generated": generated,
        "image_clips_rendered": clips,
        "stages": {key: value["reused"] for key, value in status["stages"].items()},
    }
    if post_stages:
        post = scene_post.assemble(project, manifest, durations, status, progress)
        result.update(output=post["output"], post_stages=post["stages"], frames=post["frames"],
                      seconds=post["seconds"], warnings=post["warnings"])
        return result
    exported = Path(status["export"]["path"])
    out_dir = project / "out"
    out_dir.mkdir(exist_ok=True)
    final = out_dir / f"{manifest.project_id}.mp4"
    staged = out_dir / f".{manifest.project_id}.partial.mp4"
    try:
        shutil.copyfile(exported, staged)
        os.replace(staged, final)
    finally:
        staged.unlink(missing_ok=True)
    result["output"] = str(final)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scene-manifest pipeline: per-scene narration and visuals"
    )
    parser.add_argument("--project", required=True, help="Project folder containing scenes.json")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="Check the manifest and probe every asset")
    commands.add_parser("plan", help="Show where each scene's audio will come from")
    commands.add_parser("build", help="Prepare scenes, render, and write out/<project_id>.mp4")
    commands.add_parser("status", help="Show the current render revision status")
    commands.add_parser("needs-network", help="Print yes if any scene still needs TTS, else no")
    args = parser.parse_args(argv)
    project = Path(args.project).resolve()
    try:
        if not project.is_dir():
            raise PipelineError(f"project folder not found: {project}")
        if args.command == "validate":
            manifest = load_manifest(project)
            inspect_media(manifest, project)
            result = {"valid": True, "project_id": manifest.project_id,
                      "scenes": len(manifest.scenes)}
        elif args.command == "plan":
            result = plan_report(project)
        elif args.command == "build":
            result = build(project)
        elif args.command == "needs-network":
            print("yes" if needs_network(project) else "no")
            return 0
        else:
            renderer = VideoProject(project / "render")
            result = renderer.status()
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        # PipelineError, ManifestError and ProjectError are all ValueErrors. Running
        # this file with -m loads a second copy of the module for the post stages,
        # whose exception classes differ from this copy's, so catch the base class.
        print(f"scene pipeline: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
