"""Read-only plan for a scene project.

Reports where each scene's audio comes from, how long each scene and each visual
lasts, where text cards fall against the scene, and everything a build would
reject (asset durations that do not add up, cards that end after their scene,
text that cannot fit, transitions longer than their scenes).

Nothing is written into the project: trimmed audio is measured in a temporary
directory unless an earlier build has already cached it. A scene whose narration
has not been generated yet (text-to-speech) has no length, so everything that
depends on its length is reported as pending; a build checks it once the
narration exists and before any rendering.
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

from app.services import scene_pipeline as sp
from app.services import scene_look, scene_post, scene_text

PENDING_NOTE = (
    "narration is not generated yet: length, per-asset durations, card windows and "
    "caption cues are checked after it exists, before rendering"
)
UNKNOWN_FRAMES = 10**9


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def audio_lengths(project: Path, manifest: sp.SceneManifest, path: Path) -> tuple[float, float]:
    """(raw, trimmed) length of a scene's audio in seconds, without writing into the project."""
    raw = sp.probe_audio(path)
    if not manifest.trim.enabled:
        return raw, raw
    cached = sp.trimmed_audio_path(project, path, manifest.trim.padding)
    if cached.is_file() and cached.stat().st_size > 0:
        return raw, sp.probe_audio(cached)
    with tempfile.TemporaryDirectory(prefix="plan-") as folder:
        trimmed = Path(folder) / "trimmed.wav"
        sp.trim_trailing_silence(path, trimmed, manifest.trim.padding)
        return raw, sp.probe_audio(trimmed)


def _asset_rows(manifest: sp.SceneManifest, scene: sp.Scene, infos: dict, duration: float | None,
                problems: list[str]) -> list[dict]:
    fps = manifest.settings.fps
    refs = scene.assets or (sp.AssetRef(scene.asset),)
    seconds = counts = None
    if duration is not None:
        try:
            seconds = sp.plan_asset_durations(refs, duration)
            total = max(2, math.ceil(duration * fps) + sp.CLIP_MARGIN_FRAMES)
            counts = sp.allocate_frames(seconds, total, fps)
        except sp.PipelineError as exc:
            problems.append(str(exc))
    rows = []
    cursor = 0.0
    for index, ref in enumerate(refs):
        info = infos[ref.file]
        row = {
            "file": ref.file,
            "kind": info.kind,
            "pixels": f"{info.width}x{info.height}",
            "fit": ref.fit or scene.fit,
            "motion": (ref.motion or scene.motion) if info.kind == "image" else None,
            "duration_given": ref.duration,
        }
        if seconds is not None and counts is not None:
            row.update(starts=_round(cursor), ends=_round(cursor + seconds[index]),
                       seconds=_round(seconds[index]), frames=counts[index])
            cursor += seconds[index]
            if info.kind == "video" and ref.start > 0 and ref.start + counts[index] / fps > info.duration + 0.05:
                problems.append(
                    f"{ref.file} has {max(info.duration - ref.start, 0):.2f}s after its start of "
                    f"{ref.start:g}s but {counts[index] / fps:.2f}s are needed; a video only loops "
                    "when it starts at 0"
                )
        rows.append(row)
    return rows


def _card_rows(manifest: sp.SceneManifest, scene: sp.Scene, length: float | None,
               problems: list[str]) -> list[dict]:
    width, height = manifest.settings.width, manifest.settings.height
    rows = []
    styles = []
    for index, card in enumerate(scene.text_cards, start=1):
        style = scene_text.resolve_style("card", width, height, manifest.text_card_style,
                                         scene_text.KIND_DEFAULTS[card.kind], card.style)
        styles.append(style)
        row = {"index": index, "kind": card.kind, "text": card.text, "position": style["position"],
               "start": card.start, "end": card.end}
        for field in ("label", "value", "source"):
            if getattr(card, field):
                row[field] = getattr(card, field)
        try:
            drawn = [i for i in scene_text.card_items(card, style, width, height)
                     if isinstance(i, scene_text.DrawLine)]
            size = max(i.size for i in drawn)
            row.update(lines=len(drawn), size=size, align=style["align"], panel=style["panel"],
                       shrunk_from=style["size"] if size < style["size"] else None)
        except scene_text.TextLayoutError as exc:
            problems.append(f"text card {index}: {exc}")
        if length is not None:
            row["seconds_before_scene_end"] = _round(length - card.end)
            row["inside_scene"] = card.end <= length + 1e-6
            if not row["inside_scene"]:
                problems.append(
                    f"text card {index} ends at {card.end:g}s but the scene lasts {length:.2f}s"
                )
        rows.append(row)
    for later in range(len(rows)):
        for earlier in range(later):
            a, b = scene.text_cards[earlier], scene.text_cards[later]
            if styles[earlier]["position"] == styles[later]["position"] and a.start < b.end and b.start < a.end:
                problems.append(
                    f"text cards {earlier + 1} and {later + 1} are on screen together at the same "
                    f"position ({styles[later]['position']})"
                )
    return rows


def build_plan(project: Path) -> dict:
    """Return {"scenes": [...], "summary": {...}} for a project, writing nothing into it."""
    manifest = sp.load_manifest(project)
    infos = sp.inspect_media(manifest, project)
    fps = manifest.settings.fps
    entries: list[dict] = []
    trimmed_lengths: list[float | None] = []
    for scene in manifest.scenes:
        source, path = sp.audio_source(project, manifest, scene)
        entry = {
            "scene": scene.id,
            "audio": source,
            "visual": "multi" if scene.assets else infos[scene.asset].kind,
            "audio_file": path.name,
            "problems": [],
            "warnings": [],
            "notes": [],
        }
        raw = trimmed = None
        if source == "tts":
            entry["notes"].append(PENDING_NOTE)
        else:
            try:
                raw, trimmed = audio_lengths(project, manifest, path)
            except sp.PipelineError as exc:
                entry["problems"].append(str(exc))
        entry.update(audio_seconds=_round(raw), trimmed_seconds=_round(trimmed),
                     pause_after=scene.pause_after, volume_db=scene.volume_db)
        trimmed_lengths.append(trimmed)
        entries.append(entry)

    frames = [
        None if d is None else max(1, round((d + scene.pause_after) * fps))
        for d, scene in zip(trimmed_lengths, manifest.scenes)
    ]
    heads, tails, transition_problems = scene_post.transition_frames(
        manifest, [UNKNOWN_FRAMES if f is None else f for f in frames])
    warnings = scene_post.warnings_for(manifest)

    cursor: float | None = 0.0
    for index, (scene, entry) in enumerate(zip(manifest.scenes, entries)):
        length = None if frames[index] is None else frames[index] / fps
        entry.update(frames=frames[index], length_seconds=_round(length), starts_at=_round(cursor))
        entry["transition"] = (
            {"type": scene.transition.type, "frames": tails[index], "seconds": _round(tails[index] / fps)}
            if tails[index] else None
        )
        look = scene_look.look_filters(scene_look.merge_look(manifest.look, scene.look))
        entry["look"] = look or None
        if entry["transition"] and scene.transition.flash:
            entry["transition"]["flash"] = scene.transition.flash
        entry["assets"] = _asset_rows(manifest, scene, infos, trimmed_lengths[index], entry["problems"])
        entry["cards"] = _card_rows(manifest, scene, length, entry["problems"])
        caption = scene_text.merge_caption(manifest.captions, scene.captions)
        cues = None
        if caption["enabled"] and trimmed_lengths[index] is not None:
            try:
                cues = len(scene_text.caption_cues(scene.narration, trimmed_lengths[index], caption["mode"]))
            except scene_text.TextLayoutError as exc:
                entry["problems"].append(str(exc))
        entry["captions"] = {"enabled": caption["enabled"], "mode": caption["mode"],
                             "position": caption["style"].get("position"), "cues": cues}
        prefix = f"scene {scene.id}:"
        entry["problems"] += [text for text in transition_problems if text.startswith(prefix)]
        entry["warnings"] += [text for text in warnings if text.startswith(prefix)]
        if cursor is not None:
            cursor = None if frames[index] is None else cursor + (frames[index] - tails[index]) / fps

    known = [f for f in frames if f is not None]
    all_known = len(known) == len(frames)
    bgm = manifest.bgm
    if bgm is not None and (bgm.fade_in or bgm.fade_out) and all_known and entries:
        seconds = (sum(known) - sum(tails)) / fps
        if bgm.fade_in + bgm.fade_out > seconds + 1e-6:
            entries[0]["problems"].append(
                f"bgm: fade_in {bgm.fade_in:g}s plus fade_out {bgm.fade_out:g}s is longer than the "
                f"video ({seconds:.2f}s)")
    summary = {
        "project_id": manifest.project_id,
        "settings": f"{manifest.settings.width}x{manifest.settings.height} @ {fps} fps",
        "scenes": len(entries),
        "pending_scenes": [e["scene"] for e in entries if e["audio"] == "tts"],
        "total_frames": sum(known) - sum(tails) if all_known else None,
        "total_seconds": _round((sum(known) - sum(tails)) / fps) if all_known else None,
        "transition_frames": sum(tails),
        "music_fades": None if bgm is None or not (bgm.fade_in or bgm.fade_out) else
        {"fade_in": bgm.fade_in, "fade_out": bgm.fade_out},
        "music": None if manifest.bgm is None or not manifest.bgm.folder else {
            "folder": manifest.bgm.folder, "seed": manifest.bgm.seed,
            "seed_source": "manifest" if manifest.bgm.seed_explicit else "project_id",
            "candidates": manifest.bgm.candidates, "chosen": manifest.bgm.chosen},
        "loudness": None if manifest.loudness is None else (
            f"{manifest.loudness.target_lufs:g} LUFS, true peak {manifest.loudness.true_peak_db:g} dBTP"),
        "problems": sum(len(e["problems"]) for e in entries),
        "warnings": sum(len(e["warnings"]) for e in entries),
    }
    return {"scenes": entries, "summary": summary}


def format_text(plan: dict) -> str:
    """A readable version of the plan."""
    summary = plan["summary"]
    lines = [f"Project {summary['project_id']}: {summary['settings']}, {summary['scenes']} scenes"]
    lines.append("")
    lines.append("scene | audio | trimmed s | pause | frames | length s | starts s | transition out")
    for e in plan["scenes"]:
        transition = f"{e['transition']['type']} {e['transition']['seconds']} s" if e["transition"] else "none"
        lines.append(" | ".join(str(x) for x in (
            e["scene"], e["audio"], "pending" if e["trimmed_seconds"] is None else f"{e['trimmed_seconds']:.3f}",
            e["pause_after"], "-" if e["frames"] is None else e["frames"],
            "-" if e["length_seconds"] is None else f"{e['length_seconds']:.3f}",
            "-" if e["starts_at"] is None else f"{e['starts_at']:.3f}", transition)))
    if summary["total_frames"] is not None:
        lines.append(f"total {summary['total_frames']} frames = {summary['total_seconds']:.3f} s "
                     f"(scenes joined end to end, {summary['transition_frames']} transition frames overlap)")
    else:
        lines.append(f"total: pending (narration still to generate for {', '.join(summary['pending_scenes'])})")
    for e in plan["scenes"]:
        lines.append("")
        length = "pending" if e["length_seconds"] is None else f"{e['length_seconds']:.3f} s"
        lines.append(f"{e['scene']}  (scene length {length})")
        for n, a in enumerate(e["assets"], start=1):
            span = (f"{a['starts']:.2f} -> {a['ends']:.2f} s = {a['seconds']:.2f} s, {a['frames']} frames"
                    if "seconds" in a else "time on the timeline: pending")
            given = "given" if a["duration_given"] is not None else "shares the rest"
            lines.append(f"  asset {n}: {a['file']} [{a['pixels']}] fit={a['fit']} motion={a['motion']}  "
                         f"{span} ({given})")
        for c in e["cards"]:
            window = f"{c['start']:.2f} -> {c['end']:.2f} s"
            if "seconds_before_scene_end" in c:
                window += f", {c['seconds_before_scene_end']:.2f} s before the scene ends"
            detail = f"{c['position']}, {c.get('lines', '?')} line(s), size {c.get('size', '?')}"
            if c.get("shrunk_from"):
                detail += f" (shrunk from {c['shrunk_from']})"
            shown = c["text"].splitlines()[0][:48] if c["text"] else ""
            kind = c.get("kind", "plain")
            if kind == "claim":
                shown = f"{c['label']}: {shown}"
            elif kind == "fact":
                shown = " ".join(x for x in (c.get("label", ""), c["value"], shown) if x)
            elif kind == "quote" and c.get("source"):
                shown = f"{shown} -- {c['source']}"
            if kind != "plain":
                detail += f", {kind}"
            lines.append(f"  card {c['index']}: {window} [{detail}] {shown}")
        if e.get("look"):
            lines.append(f"  look: {e['look']}")
        if e.get("transition") and e["transition"].get("flash"):
            flash = e["transition"]["flash"]
            lines.append(f"  flash: {flash['duration']:g} s, strength {flash['strength']:g}, {flash['color']}")
        if e.get("volume_db"):
            lines.append(f"  narration gain: {e['volume_db']:+g} dB")
        cap = e["captions"]
        if cap["enabled"]:
            cues = "pending" if cap["cues"] is None else cap["cues"]
            lines.append(f"  captions: {cap['mode']}, position {cap['position']}, {cues} cues")
        for text in e["notes"]:
            lines.append(f"  note: {text}")
        for text in e["warnings"]:
            lines.append(f"  WARNING: {text}")
        for text in e["problems"]:
            lines.append(f"  PROBLEM: {text}")
    lines.append("")
    if summary.get("music_fades"):
        fades = summary["music_fades"]
        lines.append(f"music fades: in {fades['fade_in']:g} s, out {fades['fade_out']:g} s")
    if summary["music"]:
        music = summary["music"]
        lines.append(f"music: {music['chosen']} picked from audio/{music['folder']}/ with seed "
                     f"{music['seed']} (from {music['seed_source']}), {music['candidates']} files; "
                     "recorded in out/<name>.music.json")
    if summary["loudness"]:
        lines.append(f"loudness: normalise to {summary['loudness']} "
                     "(written as out/<name>.loudnorm.mp4 and out/<name>.loudnorm.json)")
    lines.append(f"{summary['problems']} problem(s), {summary['warnings']} warning(s)")
    return "\n".join(lines)
