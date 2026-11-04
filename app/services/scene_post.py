"""Post-render stages for the scene pipeline: overlays, transitions, pauses, music.

video_project renders each scene (picture plus narration) and caches it. This
module works on those scene artifacts and adds its own cached stages:

  scene segment   one encode per scene body (captions and text cards burned in,
                  pause_after hold, trimmed to the part not covered by a transition)
  junction        the overlap between two scenes (xfade for picture, acrossfade
                  for narration); one short encode per transition
  concat          segments joined by stream copy, picture and narration separately
  final           narration plus optional ducked music, muxed with the picture

Every stage key contains all of its inputs, so changing one scene rebuilds that
scene's segments, its neighbouring junctions, and the later join and mux stages.
Everything streams through ffmpeg; no stage holds a whole video in memory.

Timeline rule. Each scene lasts n frames, where n = round((audio seconds +
pause_after) * fps). A transition of t frames between scene A and scene B makes
B start t frames before A ends, so the total is the sum of n minus the sum of t.
Narration is cut and overlapped with the same frame counts, so picture and sound
stay in sync.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.services import scene_text
from app.services.scene_pipeline import (
    PipelineError,
    SceneManifest,
    _key,
    _sha256_file,
)

SAMPLE_RATE = 48000
SEGMENT_VERSION = "seg-v1"
ENCODE = [
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-bf", "0",
]
TRANSITION_TYPES = {
    "cut": None,
    "crossfade": "fade",
    "fade_black": "fadeblack",
    "fade_white": "fadewhite",
    "dissolve": "dissolve",
    "wipe_left": "wipeleft",
    "wipe_right": "wiperight",
    "slide_left": "slideleft",
    "slide_right": "slideright",
}
OVERLAP_WARNING_SECONDS = 0.05
COMMAND_TIMEOUT = 1800


@dataclass
class _Scene:
    id: str
    frames: int
    head: int
    tail: int
    pad_seconds: float
    video: Path
    audio: Path
    video_sha: str
    audio_sha: str
    lines: list
    lines_key: list


@dataclass
class _Context:
    project: Path
    post: Path
    fps: int
    width: int
    height: int
    progress: Callable[[str], None]
    stages: dict = field(default_factory=dict)


# -- timeline math --------------------------------------------------------

def scene_frames(manifest: SceneManifest, durations: list[float]) -> list[int]:
    """Frames per scene: narration plus pause_after, at the project frame rate."""
    fps = manifest.settings.fps
    return [
        max(1, round((duration + scene.pause_after) * fps))
        for scene, duration in zip(manifest.scenes, durations)
    ]


def transition_frames(manifest: SceneManifest, frames: list[int]) -> tuple[list[int], list[int], list[str]]:
    """Return (head, tail, errors): frames shared with the previous / next scene."""
    fps = manifest.settings.fps
    count = len(manifest.scenes)
    heads = [0] * count
    tails = [0] * count
    for index, scene in enumerate(manifest.scenes[:-1]):
        transition = scene.transition
        if transition is None or transition.type == "cut":
            continue
        shared = round(transition.duration * fps)
        tails[index] = shared
        heads[index + 1] = shared
    errors = []
    for index, scene in enumerate(manifest.scenes):
        if frames[index] < heads[index] + tails[index]:
            errors.append(
                f"scene {scene.id}: lasts {frames[index]} frames but its transitions need "
                f"{heads[index] + tails[index]}; shorten the transitions or lengthen the scene"
            )
    return heads, tails, errors


def total_frames(frames: list[int], tails: list[int]) -> int:
    return sum(frames) - sum(tails)


def _samples(frames: int, fps: int) -> int:
    return round(frames * SAMPLE_RATE / fps)


def _overlays(manifest: SceneManifest, scene, duration: float) -> list:
    """Every line of text for one scene; captions are timed over the audio."""
    caption = scene_text.merge_caption(manifest.captions, scene.captions)
    return scene_text.scene_overlays(
        scene.narration, duration, caption, scene.text_cards, manifest.text_card_style,
        manifest.settings.width, manifest.settings.height,
    )


def validate_timeline(manifest: SceneManifest, durations: list[float]) -> None:
    """Check everything that depends on real durations. Raises with all problems."""
    errors: list[str] = []
    frames = scene_frames(manifest, durations)
    _, _, problems = transition_frames(manifest, frames)
    errors.extend(problems)
    fps = manifest.settings.fps
    for index, scene in enumerate(manifest.scenes):
        length = frames[index] / fps
        scene_text.check_card_times(scene.text_cards, length, f"scene {scene.id}", errors)
        try:
            _overlays(manifest, scene, durations[index])
        except scene_text.TextLayoutError as exc:
            errors.append(f"scene {scene.id}: {exc}")
    if errors:
        raise PipelineError("\n".join(dict.fromkeys(errors)))


# -- running ffmpeg -------------------------------------------------------

def _input(path: Path, *flags: str) -> list[str]:
    return [*flags, "-protocol_whitelist", "file,pipe", "-i", str(path)]


def _ffmpeg(arguments: list[str], cwd: Path, what: str) -> None:
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-threads", "1",
        "-filter_threads", "1", *arguments,
    ]
    try:
        done = subprocess.run(command, capture_output=True, text=True, cwd=str(cwd),
                              timeout=COMMAND_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PipelineError(f"{what}: {exc}") from exc
    if done.returncode:
        raise PipelineError(f"{what} failed: {done.stderr[-2000:].strip()}")


def _ensure(ctx: _Context, folder: str, key: str, suffix: str, label: str,
            builder: Callable[[Path, Path], None]) -> Path:
    final = ctx.post / folder / f"{key}{suffix}"
    if final.is_file() and final.stat().st_size > 0:
        ctx.stages[label] = True
        return final
    final.parent.mkdir(parents=True, exist_ok=True)
    scratch = ctx.post / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="stage-", dir=scratch) as name:
        work = Path(name)
        staged = work / f"out{suffix}"
        builder(work, staged)
        if not staged.is_file() or staged.stat().st_size == 0:
            raise PipelineError(f"{label}: produced no output")
        os.replace(staged, final)
    ctx.stages[label] = False
    return final


# -- picture --------------------------------------------------------------

def _scene_chain(ctx: _Context, scene: _Scene, work: Path, prefix: str, first: int, last: int,
                 for_junction: bool) -> str:
    """Filters for one scene: hold the last frame for pauses, fix the length,
    burn in text, then keep frames [first, last)."""
    fps = ctx.fps
    parts = [
        f"tpad=stop_mode=clone:stop_duration={scene.pad_seconds:.3f}",
        f"trim=end_frame={scene.frames}",
        f"setpts=N/({fps}*TB)",
    ]
    chain = scene_text.drawtext_chain(scene.lines, prefix)
    if chain.filters:
        parts.append(chain.filters)
        for name, text in chain.files.items():
            (work / name).write_text(text, encoding="utf-8")
        for alias, font in chain.fonts.items():
            (work / alias).symlink_to(font)
    parts += [f"trim=start_frame={first}:end_frame={last}", f"setpts=N/({fps}*TB)"]
    if for_junction:
        parts.append(f"fps={fps}")
    parts.append("format=yuv420p")
    return ",".join(parts)


def _video_body(ctx: _Context, scene: _Scene) -> Path | None:
    length = scene.frames - scene.head - scene.tail
    if length <= 0:
        return None
    key = _key(SEGMENT_VERSION, "body", scene.video_sha, scene.lines_key, scene.frames,
               scene.head, scene.tail, ctx.fps, ctx.width, ctx.height, ENCODE)

    def build(work: Path, staged: Path) -> None:
        chain = _scene_chain(ctx, scene, work, "t", scene.head, scene.frames - scene.tail, False)
        (work / "graph.txt").write_text(chain, encoding="utf-8")
        _ffmpeg(
            [*_input(scene.video), "-filter_script:v", "graph.txt", "-frames:v", str(length),
             "-an", *ENCODE, "-r", str(ctx.fps), str(staged)],
            work, f"picture for scene {scene.id}",
        )

    return _ensure(ctx, "vseg", key, ".mp4", f"{scene.id}:picture", build)


def _video_junction(ctx: _Context, first: _Scene, second: _Scene, kind: str) -> Path:
    shared = first.tail
    key = _key(SEGMENT_VERSION, "junction", first.video_sha, first.lines_key, first.frames,
               second.video_sha, second.lines_key, second.frames, shared, kind, ctx.fps,
               ctx.width, ctx.height, ENCODE)

    def build(work: Path, staged: Path) -> None:
        a = _scene_chain(ctx, first, work, "a", first.frames - shared, first.frames, True)
        b = _scene_chain(ctx, second, work, "b", 0, shared, True)
        graph = (
            f"[0:v]{a}[va];[1:v]{b}[vb];"
            f"[va][vb]xfade=transition={kind}:duration={shared / ctx.fps:.6f}:offset=0[v]"
        )
        (work / "graph.txt").write_text(graph, encoding="utf-8")
        _ffmpeg(
            [*_input(first.video), *_input(second.video), "-filter_complex_script", "graph.txt",
             "-map", "[v]", "-frames:v", str(shared), "-an", *ENCODE, "-r", str(ctx.fps),
             str(staged)],
            work, f"transition {first.id} to {second.id}",
        )

    return _ensure(ctx, "vseg", key, ".mp4", f"{first.id}>{second.id}:picture", build)


# -- narration ------------------------------------------------------------

def _audio_body(ctx: _Context, scene: _Scene) -> Path | None:
    length = scene.frames - scene.head - scene.tail
    if length <= 0:
        return None
    start = _samples(scene.head, ctx.fps)
    end = _samples(scene.frames - scene.tail, ctx.fps)
    key = _key(SEGMENT_VERSION, "abody", scene.audio_sha, scene.frames, start, end)

    def build(work: Path, staged: Path) -> None:
        chain = (
            f"apad=pad_dur={scene.pad_seconds:.3f},atrim=start_sample={start}:end_sample={end},"
            "asetpts=PTS-STARTPTS"
        )
        _ffmpeg(
            [*_input(scene.audio), "-af", chain, "-c:a", "pcm_s16le", "-f", "wav", str(staged)],
            work, f"narration for scene {scene.id}",
        )

    return _ensure(ctx, "aseg", key, ".wav", f"{scene.id}:narration", build)


def _audio_junction(ctx: _Context, first: _Scene, second: _Scene) -> Path:
    shared = _samples(first.tail, ctx.fps)
    first_start = _samples(first.frames - first.tail, ctx.fps)
    first_end = _samples(first.frames, ctx.fps)
    key = _key(SEGMENT_VERSION, "ajunction", first.audio_sha, first.frames, second.audio_sha,
               second.frames, shared, first_start, first_end)

    def build(work: Path, staged: Path) -> None:
        graph = (
            f"[0:a]apad=pad_dur={first.pad_seconds:.3f},"
            f"atrim=start_sample={first_start}:end_sample={first_end},asetpts=PTS-STARTPTS[x];"
            f"[1:a]apad=pad_dur={second.pad_seconds:.3f},"
            f"atrim=start_sample=0:end_sample={shared},asetpts=PTS-STARTPTS[y];"
            f"[x][y]acrossfade=d={(shared - 4) / SAMPLE_RATE:.6f}:c1=qsin:c2=qsin[z];"
            f"[z]atrim=end_sample={shared},asetpts=PTS-STARTPTS[a]"
        )
        (work / "graph.txt").write_text(graph, encoding="utf-8")
        _ffmpeg(
            [*_input(first.audio), *_input(second.audio), "-filter_complex_script", "graph.txt",
             "-map", "[a]", "-c:a", "pcm_s16le", "-f", "wav", str(staged)],
            work, f"narration crossfade {first.id} to {second.id}",
        )

    return _ensure(ctx, "aseg", key, ".wav", f"{first.id}>{second.id}:narration", build)


# -- join and mux ---------------------------------------------------------

def _concat(ctx: _Context, segments: list[Path], folder: str, suffix: str, label: str,
            copy_args: list[str]) -> tuple[Path, str]:
    key = _key("concat-v1", suffix, [segment.stem for segment in segments])
    listing = segments[0].parent / f"{key}.txt"
    listing.write_text("".join(f"file '{segment.name}'\n" for segment in segments), encoding="utf-8")

    def build(work: Path, staged: Path) -> None:
        _ffmpeg(
            ["-f", "concat", "-safe", "1", *_input(listing), "-c", "copy", *copy_args, str(staged)],
            work, label,
        )

    return _ensure(ctx, folder, key, suffix, label, build), key


def duck_ratio(amount: float) -> float:
    """Compressor ratio that lowers music by about ``amount`` dB for narration at
    roughly -20 dBFS against a -40 dBFS threshold; louder narration ducks more."""
    return min(20.0, 1.0 / (1.0 - min(amount, 19.0) / 20.0))


def bgm_graph(volume: float, ducking) -> str:
    fmt = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
    parts = [f"[1:a]{fmt}[n]", f"[2:a]{fmt},volume={volume:.4f}[m]"]
    if ducking is None:
        parts.append("[n][m]amix=inputs=2:duration=first:dropout_transition=0,volume=2,"
                     "alimiter=limit=0.95:level=0[a]")
    else:
        parts += [
            "[n]asplit[n1][n2]",
            f"[m][n1]sidechaincompress=threshold=0.01:ratio={duck_ratio(ducking.amount):.4f}:"
            f"attack={ducking.attack:g}:release={ducking.release:g}:makeup=1:knee=2.828:"
            "link=average:detection=rms[d]",
            "[n2][d]amix=inputs=2:duration=first:dropout_transition=0,volume=2,"
            "alimiter=limit=0.95:level=0[a]",
        ]
    return ";".join(parts)


def _final(ctx: _Context, manifest: SceneManifest, video: Path, video_key: str, audio: Path,
           audio_key: str, frames: int) -> Path:
    bgm = manifest.bgm
    bgm_path = ctx.project / "audio" / bgm.file if bgm else None
    key = _key("final-v1", video_key, audio_key, frames, ctx.fps,
               None if bgm is None else [bgm.file, _sha256_file(bgm_path), bgm.volume,
                                         None if bgm.ducking is None else
                                         [bgm.ducking.amount, bgm.ducking.attack,
                                          bgm.ducking.release]])
    seconds = frames / ctx.fps

    def build(work: Path, staged: Path) -> None:
        command = [*_input(video), *_input(audio)]
        if bgm is None:
            command += ["-map", "0:v:0", "-map", "1:a:0"]
        else:
            command += _input(bgm_path, "-stream_loop", "-1")
            (work / "graph.txt").write_text(bgm_graph(bgm.volume, bgm.ducking), encoding="utf-8")
            command += ["-filter_complex_script", "graph.txt", "-map", "0:v:0", "-map", "[a]"]
        command += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-t", f"{seconds:.6f}",
                    "-movflags", "+faststart", str(staged)]
        _ffmpeg(command, work, "final mux")

    return _ensure(ctx, "final", key, ".mp4", "final", build)


# -- loudness -------------------------------------------------------------

LOUDNESS_VERSION = "loud-v2"
LOUDNESS_LRA = 11.0
LOUDNESS_OFF_TARGET_LU = 1.0
LOUDNESS_PEAK_SLACK_DB = 0.3
# loudnorm is given a true-peak target this much below the requested limit, so the AAC
# encode that follows (which can overshoot by a few tenths of a dB) stays under it.
LOUDNESS_PEAK_HEADROOM_DB = 0.3
LOUDNORM_MIN_TP = -9.0  # loudnorm refuses a TP target below this


def loudnorm_peak(loudness) -> float:
    """The true-peak target handed to loudnorm: the requested limit minus the headroom, but
    never below the -9 dBTP that loudnorm accepts."""
    return max(LOUDNORM_MIN_TP, loudness.true_peak_db - LOUDNESS_PEAK_HEADROOM_DB)


def _loudnorm_json(text: str, what: str) -> dict:
    """The JSON block loudnorm prints at the end of a run."""
    end = text.rfind("}")
    start = text.rfind("{", 0, end + 1) if end >= 0 else -1
    if start < 0:
        raise PipelineError(f"{what}: loudnorm printed no measurements")
    try:
        data = json.loads(text[start:end + 1])
    except ValueError as exc:
        raise PipelineError(f"{what}: could not read the loudnorm measurements: {exc}") from exc
    return data


def _number(data: dict, key: str, what: str) -> float:
    try:
        value = float(data[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise PipelineError(f"{what}: loudnorm result has no usable {key}") from exc
    if value != value or value in (float("inf"), float("-inf")):
        raise PipelineError(f"{what}: the audio is silent or too quiet to measure ({key} is {data[key]})")
    return value


def _loudnorm_run(arguments: list[str], cwd: Path, what: str) -> dict:
    """Run ffmpeg with loudnorm's info output visible and return its JSON."""
    command = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-threads", "1", "-filter_threads", "1",
               *arguments]
    try:
        done = subprocess.run(command, capture_output=True, text=True, cwd=str(cwd),
                              timeout=COMMAND_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PipelineError(f"{what}: {exc}") from exc
    if done.returncode:
        raise PipelineError(f"{what} failed: {done.stderr[-2000:].strip()}")
    return _loudnorm_json(done.stderr, what)


def _target_filter(loudness) -> str:
    return f"I={loudness.target_lufs:g}:TP={loudnorm_peak(loudness):g}:LRA={LOUDNESS_LRA:g}"


def measure_loudness(path: Path, loudness, cwd: Path, what: str) -> dict:
    """Integrated loudness (LUFS), true peak (dBTP) and loudness range (LU) of a file's audio."""
    data = _loudnorm_run(
        [*_input(path), "-vn", "-af", f"loudnorm={_target_filter(loudness)}:print_format=json",
         "-f", "null", "-"], cwd, what)
    return {
        "lufs": _number(data, "input_i", what),
        "true_peak_db": _number(data, "input_tp", what),
        "lra": _number(data, "input_lra", what),
    }


def loudness_key(loudness, final_key: str, final_sha: str) -> str:
    return _key(LOUDNESS_VERSION, final_key, final_sha, loudness.target_lufs, loudness.true_peak_db,
                LOUDNESS_PEAK_HEADROOM_DB, LOUDNESS_LRA)


LINEAR_GAIN_TOLERANCE_DB = 0.5


def loudnorm_mode(before: dict, after: dict, loudness) -> str:
    """"linear" when loudnorm applied one constant gain, else "dynamic".

    ffmpeg labels both passes "linear" even when it falls back to dynamic
    processing, so the label is not used. Linear mode needs the gain that reaches the
    target to keep the true peak under the limit; it also has to have produced
    exactly that gain (input plus gain equals the measured output).
    """
    gain = loudness.target_lufs - before["lufs"]
    if before["true_peak_db"] + gain > loudnorm_peak(loudness):
        return "dynamic"
    if abs(after["lufs"] - (before["lufs"] + gain)) > LINEAR_GAIN_TOLERANCE_DB:
        return "dynamic"
    return "linear"


def _normalise(final: Path, loudness, work: Path, staged: Path, seconds: float) -> dict:
    """Two-pass loudnorm on the final audio with the video copied; returns the report."""
    before = _loudnorm_run(
        [*_input(final), "-vn", "-af", f"loudnorm={_target_filter(loudness)}:print_format=json",
         "-f", "null", "-"], work, "loudness pass 1")
    measured = {k: _number(before, k, "loudness pass 1")
                for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")}
    graph = (f"loudnorm={_target_filter(loudness)}:measured_I={measured['input_i']}:"
             f"measured_TP={measured['input_tp']}:measured_LRA={measured['input_lra']}:"
             f"measured_thresh={measured['input_thresh']}:offset={measured['target_offset']}:"
             "linear=true:print_format=json")
    applied = _loudnorm_run(
        [*_input(final), "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy", "-af", graph,
         "-ar", str(SAMPLE_RATE), "-c:a", "aac", "-b:a", "192k", "-t", f"{seconds:.6f}",
         "-movflags", "+faststart", str(staged)], work, "loudness pass 2")
    after = measure_loudness(staged, loudness, work, "loudness check")
    input_side = {"lufs": measured["input_i"], "true_peak_db": measured["input_tp"],
                  "lra": measured["input_lra"]}
    return {
        "mode": loudnorm_mode(input_side, after, loudness),
        "ffmpeg_label": str(applied.get("normalization_type", "")).strip().lower(),
        "target_lufs": loudness.target_lufs,
        "true_peak_db": loudness.true_peak_db,
        "input": input_side,
        "output": after,
        "predicted_output": {"lufs": _number(applied, "output_i", "loudness pass 2"),
                             "true_peak_db": _number(applied, "output_tp", "loudness pass 2"),
                             "lra": _number(applied, "output_lra", "loudness pass 2")},
    }


def loudness_warnings(report: dict) -> list[str]:
    warnings = []
    out = report["output"]
    if abs(out["lufs"] - report["target_lufs"]) > LOUDNESS_OFF_TARGET_LU:
        warnings.append(
            f"loudness: the result is {out['lufs']:.1f} LUFS, more than {LOUDNESS_OFF_TARGET_LU:g} LU "
            f"from the {report['target_lufs']:g} target ({report['mode']} mode)"
        )
    if out["true_peak_db"] > report["true_peak_db"] + LOUDNESS_PEAK_SLACK_DB:
        warnings.append(
            f"loudness: the true peak is {out['true_peak_db']:.1f} dBTP, above the "
            f"{report['true_peak_db']:g} limit after AAC encoding"
        )
    return warnings


def _loudness_stage(ctx: _Context, manifest: SceneManifest, final: Path, final_key: str,
                    frames: int) -> tuple[Path, dict]:
    """Cached normalised copy of the final file and its measured report."""
    loudness = manifest.loudness
    key = loudness_key(loudness, final_key, _sha256_file(final))
    report_path = ctx.post / "loudness" / f"{key}.json"
    media = ctx.post / "loudness" / f"{key}.mp4"
    if media.is_file() and not report_path.is_file():
        media.unlink()
    seconds = frames / ctx.fps

    def build(work: Path, staged: Path) -> None:
        report = _normalise(final, loudness, work, staged, seconds)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        part = report_path.with_suffix(".json.part")
        part.write_text(json.dumps(report, indent=2), encoding="utf-8")
        os.replace(part, report_path)

    path = _ensure(ctx, "loudness", key, ".mp4", "loudness", build)
    return path, json.loads(report_path.read_text(encoding="utf-8"))


# -- entry point ----------------------------------------------------------

def _artifact(project: Path, entry: dict, suffix: str) -> Path:
    path = project / "render" / "artifacts" / entry["fingerprint"] / f"output-{entry['sha256']}{suffix}"
    if not path.is_file():
        raise PipelineError(f"renderer artifact is missing: {path.name}")
    return path


def warnings_for(manifest: SceneManifest) -> list[str]:
    """Transitions that overlap spoken words rather than silence."""
    warnings = []
    slack = manifest.trim.padding if manifest.trim.enabled else 0.0
    for scene in manifest.scenes[:-1]:
        transition = scene.transition
        if transition is None or transition.type == "cut":
            continue
        if transition.duration > scene.pause_after + slack + OVERLAP_WARNING_SECONDS:
            warnings.append(
                f"scene {scene.id}: the {transition.duration:g}s transition overlaps the end of its "
                "narration; add pause_after of at least the transition length for a clean overlap"
            )
    return warnings


def assemble(project: Path, manifest: SceneManifest, durations: list[float], status: dict,
             progress: Callable[[str], None], output_name: str | None = None) -> dict:
    """Run every post stage on top of the renderer's scene artifacts and publish
    out/<output_name or project_id>.mp4. Returns the output path and which stages
    were reused."""
    settings = manifest.settings
    ctx = _Context(project, project / ".scene_cache" / "post", settings.fps, settings.width,
                   settings.height, progress)
    frames = scene_frames(manifest, durations)
    heads, tails, problems = transition_frames(manifest, frames)
    if problems:
        raise PipelineError("\n".join(problems))
    scenes = []
    for index, scene in enumerate(manifest.scenes):
        video = _artifact(project, status["stages"][f"{scene.id}:scene"], ".mp4")
        audio = _artifact(project, status["stages"][f"{scene.id}:audio"], ".wav")
        lines = _overlays(manifest, scene, durations[index])
        scenes.append(_Scene(
            id=scene.id, frames=frames[index], head=heads[index], tail=tails[index],
            pad_seconds=scene.pause_after + 2.0, video=video, audio=audio,
            video_sha=status["stages"][f"{scene.id}:scene"]["sha256"],
            audio_sha=status["stages"][f"{scene.id}:audio"]["sha256"],
            lines=lines, lines_key=scene_text.overlay_key(lines),
        ))

    video_segments: list[Path] = []
    audio_segments: list[Path] = []
    for index, scene in enumerate(scenes):
        picture = _video_body(ctx, scene)
        if picture is not None:
            video_segments.append(picture)
            audio_segments.append(_audio_body(ctx, scene))
        if index < len(scenes) - 1 and scene.tail > 0:
            kind = TRANSITION_TYPES[manifest.scenes[index].transition.type]
            video_segments.append(_video_junction(ctx, scene, scenes[index + 1], kind))
            audio_segments.append(_audio_junction(ctx, scene, scenes[index + 1]))
        progress(f"  {scene.id}: segments ready")

    video, video_key = _concat(ctx, video_segments, "video", ".mp4", "join:picture",
                               ["-an", "-movflags", "+faststart"])
    audio, audio_key = _concat(ctx, audio_segments, "audio", ".wav", "join:narration", [])
    count = total_frames(frames, tails)
    final = _final(ctx, manifest, video, video_key, audio, audio_key, count)
    report = None
    normalised = None
    if manifest.loudness is not None:
        progress("  loudness: measuring and normalising the final audio")
        normalised, report = _loudness_stage(ctx, manifest, final, _key("final", video_key, audio_key, count), count)

    out_dir = project / "out"
    out_dir.mkdir(exist_ok=True)
    name = output_name or manifest.project_id
    published = out_dir / f"{name}.mp4"
    staged = out_dir / f".{name}.partial.mp4"
    try:
        shutil.copyfile(final, staged)
        os.replace(staged, published)
    finally:
        staged.unlink(missing_ok=True)
    warnings = warnings_for(manifest)
    result = {
        "output": str(published),
        "stages": dict(ctx.stages),
        "frames": count,
        "seconds": count / settings.fps,
        "warnings": warnings,
    }
    if report is not None:
        loud_media = out_dir / f"{name}.loudnorm.mp4"
        loud_report = out_dir / f"{name}.loudnorm.json"
        loud_staged = out_dir / f".{name}.loudnorm.partial.mp4"
        try:
            shutil.copyfile(normalised, loud_staged)
            os.replace(loud_staged, loud_media)
        finally:
            loud_staged.unlink(missing_ok=True)
        loud_report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        warnings.extend(loudness_warnings(report))
        result["loudness"] = {**report, "file": str(loud_media), "report": str(loud_report)}
    return result
