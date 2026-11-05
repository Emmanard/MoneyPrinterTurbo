"""Captions, text cards, several visuals per scene, transitions, pauses, music and the
other new looks. Real FFmpeg; narration comes from pre-made tone files so no network
or TTS is needed. Thresholds on picture and level comparisons may need tuning once
run against the container's ffmpeg."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from app.services import scene_pipeline as sp
from app.services import scene_post

SETTINGS = {"width": 270, "height": 480, "fps": 10}
SAY = "Hello there my friends, welcome back to the show"


def _ff(arguments):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-threads", "1", *arguments],
        check=True,
    )


def _probe(path, entry, select=None):
    command = ["ffprobe", "-v", "error"]
    if select:
        command += ["-select_streams", select]
    command += ["-show_entries", entry, "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    return float(subprocess.check_output(command, text=True).split()[0])


def _duration(path):
    return _probe(path, "format=duration")


def _stream_duration(path, select):
    return _probe(path, "stream=duration", select)


def _png(path, pattern="testsrc", size="270x480"):
    _ff(["-f", "lavfi", "-i", f"{pattern}=s={size}:d=1", "-frames:v", "1", str(path)])


def _tone(path, seconds, frequency=440, gain=1):
    _ff(["-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000:duration={seconds}",
         "-af", f"volume={gain}", "-f", "wav", str(path)])


def _frame(video, seconds, dest):
    _ff(["-ss", str(seconds), "-i", str(video), "-frames:v", "1", str(dest)])
    return dest


def _psnr(first, second):
    done = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(first), "-i", str(second),
                           "-lavfi", "psnr", "-f", "null", "-"], capture_output=True, text=True)
    return float(re.search(r"average:(\S+)", done.stderr).group(1))


def _level(path, start, length):
    done = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-ss", str(start), "-t", str(length), "-i", str(path),
         "-af", "highpass=f=1000,volumedetect", "-f", "null", "-"], capture_output=True, text=True)
    return float(re.search(r"mean_volume: (-?[\d.]+) dB", done.stderr).group(1))


def _forbidden(*_args):
    raise AssertionError("narration synthesis must not run")


@pytest.fixture
def project(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("native FFmpeg/FFprobe required")
    root = tmp_path / "project's files"
    (root / "assets").mkdir(parents=True)
    (root / "audio").mkdir()
    _png(root / "assets" / "a.png")
    _png(root / "assets" / "b.png", "smptebars")
    _png(root / "assets" / "wide.png", "testsrc", "320x160")
    _ff(["-f", "lavfi", "-i", "color=c=blue:s=270x480:r=10:d=2", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", str(root / "assets" / "clip.mp4")])
    _tone(root / "audio" / "one.wav", 1.37, 440)
    _tone(root / "audio" / "two.wav", 0.93, 330)
    _tone(root / "audio" / "three.wav", 1.3, 550)
    _tone(root / "audio" / "voice200.wav", 1.3, 200)
    _tone(root / "audio" / "music_short.wav", 0.7, 1500, 4)
    _tone(root / "audio" / "music_long.wav", 8, 1500, 4)
    return root


def _write(root, scenes, **extra):
    manifest = {"schema_version": 1, "project_id": "ep", "settings": dict(SETTINGS), "scenes": scenes}
    manifest.update(extra)
    (root / "scenes.json").write_text(json.dumps(manifest), encoding="utf-8")


def _scene(scene_id, audio="one.wav", asset="a.png", **extra):
    return {"id": scene_id, "asset": asset, "audio": audio, "narration": SAY, **extra}


def _build(root):
    return sp.build(root, synthesize=_forbidden, progress=lambda _m: None)


def _errors(root):
    with pytest.raises(sp.ManifestError) as caught:
        sp.load_manifest(root)
    return str(caught.value)


def _rebuilt(result):
    return {key for key, reused in result["post_stages"].items() if not reused}


# -- the original path is untouched ---------------------------------------

def test_original_manifest_does_not_use_post_stages(project):
    _write(project, [_scene("s1"), _scene("s2", "two.wav", "b.png")])
    manifest = sp.load_manifest(project)
    assert sp.uses_post(manifest) is False
    result = _build(project)
    assert "post_stages" not in result and "stages" in result
    assert not (project / ".scene_cache" / "post").exists()


def test_original_filter_strings_are_unchanged():
    assert sp.build_zoom_filter(270, 480, 10, 23, "zoom_in", "zoompan", 2) == (
        "scale=540:960:force_original_aspect_ratio=increase:flags=lanczos,crop=540:960,"
        "zoompan=z='1+0.069000*on/23':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=23:s=270x480:fps=10,"
        "setsar=1,format=yuv420p"
    )
    # scale_crop zoom_in changed on purpose: its crop is now centred from the known
    # sizes (the old '(iw-ow)/2' stuck to the top-left corner). zoompan is untouched.
    assert sp.build_zoom_filter(270, 480, 10, 23, "zoom_in", "scale_crop", 2) == (
        "scale=540:960:force_original_aspect_ratio=increase:flags=lanczos,crop=540:960,"
        "scale=w='iw*(1+0.069000*t/2.300000)':h='ih*(1+0.069000*t/2.300000)':eval=frame:flags=bicubic,"
        "crop=540:960:'540*(0.069000*t/2.300000)/2':'960*(0.069000*t/2.300000)/2',"
        "scale=270:480:flags=lanczos,setsar=1,format=yuv420p"
    )
    assert sp.build_zoom_filter(270, 480, 10, 23, "static", "zoompan", 2) == (
        "scale=270:480:force_original_aspect_ratio=increase:flags=lanczos,crop=270:480,"
        "setsar=1,format=yuv420p"
    )


@pytest.mark.parametrize("extra,scene_extra", [
    ({"bgm": {"file": "music_short.wav"}}, {}),
    ({}, {"pause_after": 0.5}),
    ({}, {"transition": {"type": "crossfade"}}),
    ({}, {"text_cards": [{"text": "Hi", "start": 0, "end": 1}]}),
    ({}, {"captions": {"enabled": True}}),
    ({"settings": {**SETTINGS, "captions": {"enabled": True}}}, {}),
])
def test_each_new_feature_switches_on_the_post_stages(project, extra, scene_extra):
    _write(project, [_scene("s1", **scene_extra), _scene("s2", "two.wav")], **extra)
    assert sp.uses_post(sp.load_manifest(project)) is True


def test_cut_transition_alone_does_not_need_post_stages(project):
    _write(project, [_scene("s1", transition={"type": "cut"}), _scene("s2", "two.wav")])
    assert sp.uses_post(sp.load_manifest(project)) is False


# -- validation -----------------------------------------------------------

def test_default_caption_style_in_settings_and_scene_override(project):
    defaults = {"enabled": True, "mode": "words", "style": {"size": 40, "position": "top"}}
    _write(project, [_scene("s1"), _scene("s2", "two.wav", captions={"style": {"size": 60}}),
                     _scene("s3", "three.wav", captions={"enabled": False})],
           settings={**SETTINGS, "captions": defaults})
    manifest = sp.load_manifest(project)
    from app.services import scene_text
    effective = [scene_text.merge_caption(manifest.captions, s.captions) for s in manifest.scenes]
    assert [e["enabled"] for e in effective] == [True, True, False]
    assert effective[1]["style"] == {"size": 60, "position": "top"}


def test_captions_without_narration_are_rejected(project):
    scene = {"id": "s1", "asset": "a.png", "audio": "one.wav", "captions": {"enabled": True}}
    _write(project, [scene])
    assert "captions need narration" in _errors(project)


def test_new_scene_fields_are_validated_together(project):
    _write(project, [
        _scene("s1", pause_after=99, captions={"mode": "karaoke"},
               text_cards=[{"text": "x", "start": 2, "end": 1}],
               transition={"type": "spin", "duration": 9}),
        _scene("s2", transition={"type": "crossfade"}),
    ], bgm={"file": "missing.wav", "volume": 5, "ducking": {"amount": 40, "oops": 1}})
    message = _errors(project)
    for fragment in ("pause_after", "captions.mode", "text_cards[0].end", "transition.type",
                     "last scene", "bgm.file", "bgm.volume", "ducking.amount", "ducking: unknown field"):
        assert fragment in message


def test_transition_duration_range_and_last_scene(project):
    _write(project, [_scene("s1", transition={"type": "crossfade", "duration": 9}), _scene("s2", "two.wav")])
    assert "transition.duration" in _errors(project)
    _write(project, [_scene("s1"), _scene("s2", "two.wav", transition={"type": "dissolve"})])
    assert "last scene" in _errors(project)
    _write(project, [_scene("s1"), _scene("s2", "two.wav", transition={"type": "cut"})])
    assert sp.load_manifest(project).scenes[1].transition.type == "cut"


def test_asset_and_assets_are_mutually_exclusive(project):
    _write(project, [_scene("s1", assets=[{"file": "a.png"}])])
    assert "either asset or assets" in _errors(project)
    scene = {"id": "s1", "narration": SAY, "audio": "one.wav"}
    _write(project, [scene])
    assert "asset" in _errors(project)


def test_assets_entries_are_validated(project):
    scene = {"id": "s1", "narration": SAY, "audio": "one.wav", "assets": [
        {"file": "a.png", "start": 2}, {"file": "gone.png"}, {"file": "b.png", "duration": -1, "x": 1},
        {"file": "b.png", "motion": "spin"}]}
    _write(project, [scene])
    message = _errors(project)
    for fragment in ("only applies to video", "gone.png", "duration", "unknown field: x", "motion"):
        assert fragment in message


def test_full_narration_audio_is_still_reserved(project):
    _write(project, [_scene("s1")], full_narration_audio="all.mp3")
    assert "reserved for a later phase" in _errors(project)


def test_settings_bgm_volume_points_to_bgm(project):
    _write(project, [_scene("s1")], settings={**SETTINGS, "bgm_volume": 0.2})
    assert "bgm.volume" in _errors(project)


def test_ducking_defaults_and_none(project):
    _write(project, [_scene("s1")], bgm={"file": "music_short.wav", "ducking": {}})
    ducking = sp.load_manifest(project).bgm.ducking
    assert (ducking.amount, ducking.attack, ducking.release) == (12.0, 150.0, 800.0)
    _write(project, [_scene("s1")], bgm={"file": "music_short.wav"})
    assert sp.load_manifest(project).bgm.ducking is None


# -- captions and text cards in the picture -------------------------------

def test_captions_are_burned_into_the_picture(project):
    _write(project, [_scene("s1")])
    plain = _frame(_build(project)["output"], 0.6, project / "plain.png")
    _write(project, [_scene("s1")], settings={**SETTINGS, "captions": {"enabled": True, "mode": "words"}})
    result = _build(project)
    captioned = _frame(result["output"], 0.6, project / "captioned.png")
    assert _psnr(plain, captioned) < 35
    assert abs(_duration(result["output"]) - result["seconds"]) < 0.15


def test_text_card_shows_only_between_start_and_end(project):
    _write(project, [_scene("s1")])
    plain = project / "plain.mp4"
    shutil.copyfile(_build(project)["output"], plain)
    _write(project, [_scene("s1", text_cards=[{"text": "A quote", "start": 0.4, "end": 0.9}])])
    carded = _build(project)["output"]
    before = _psnr(_frame(plain, 0.2, project / "p1.png"), _frame(carded, 0.2, project / "c1.png"))
    during = _psnr(_frame(plain, 0.65, project / "p2.png"), _frame(carded, 0.65, project / "c2.png"))
    after = _psnr(_frame(plain, 1.2, project / "p3.png"), _frame(carded, 1.2, project / "c3.png"))
    assert during < 32
    assert before > 36 and after > 36


def test_text_card_past_the_end_of_the_scene_fails_before_rendering(project):
    _write(project, [_scene("s1", text_cards=[{"text": "Late", "start": 0, "end": 30}])])
    with pytest.raises(sp.PipelineError, match="text_cards"):
        _build(project)
    assert not (project / "render").exists()


def test_captions_follow_the_pause_after_rule(project):
    _write(project, [_scene("s1", pause_after=1.0, captions={"enabled": True, "mode": "words"})])
    result = _build(project)
    assert result["frames"] == round((1.37 + 1.0) * 10)
    during_pause = _frame(result["output"], 2.0, project / "pause.png")
    plain = project / "plain_pause.png"
    _write(project, [_scene("s1", pause_after=1.0)])
    _frame(_build(project)["output"], 2.0, plain)
    assert _psnr(plain, during_pause) > 36


def test_changing_one_caption_rebuilds_only_that_scene(project):
    scenes = [_scene("s1", captions={"enabled": True}), _scene("s2", "two.wav", captions={"enabled": True}),
              _scene("s3", "three.wav", captions={"enabled": True})]
    _write(project, scenes)
    first = _build(project)
    assert not any(first["post_stages"].values())
    again = _build(project)
    assert all(again["post_stages"].values()) and all(again["stages"].values())
    scenes[1] = _scene("s2", "two.wav", captions={"enabled": True, "style": {"color": "#FFFF00"}})
    _write(project, scenes)
    changed = _build(project)
    assert _rebuilt(changed) == {"s2:picture", "join:picture", "final"}
    assert all(changed["stages"].values())


# -- several visuals in one scene -----------------------------------------

def test_asset_durations_that_add_up():
    refs = (sp.AssetRef("a.png", duration=0.5), sp.AssetRef("b.png", duration=0.8))
    assert sp.plan_asset_durations(refs, 1.3) == [0.5, 0.8]


def test_small_mismatch_is_absorbed_by_the_last_asset():
    refs = (sp.AssetRef("a.png", duration=0.5), sp.AssetRef("b.png", duration=0.7))
    seconds = sp.plan_asset_durations(refs, 1.3)
    assert seconds[0] == 0.5 and abs(seconds[1] - 0.8) < 1e-9


def test_large_mismatch_is_rejected():
    refs = (sp.AssetRef("a.png", duration=0.5), sp.AssetRef("b.png", duration=0.5))
    with pytest.raises(sp.PipelineError, match="add up"):
        sp.plan_asset_durations(refs, 1.3)


def test_assets_without_a_duration_share_the_rest():
    refs = (sp.AssetRef("a.png"), sp.AssetRef("b.png", duration=0.3), sp.AssetRef("c.png"))
    seconds = sp.plan_asset_durations(refs, 1.3)
    assert abs(seconds[0] - 0.5) < 1e-9 and abs(seconds[2] - 0.5) < 1e-9 and seconds[1] == 0.3


def test_no_time_left_for_open_assets_is_rejected():
    refs = (sp.AssetRef("a.png", duration=1.3), sp.AssetRef("b.png"))
    with pytest.raises(sp.PipelineError, match="leaving"):
        sp.plan_asset_durations(refs, 1.3)


@pytest.mark.parametrize("seconds,total", [([0.5, 0.8], 14), ([0.33, 0.33, 0.64], 13), ([1.3], 16)])
def test_frame_allocation_adds_up(seconds, total):
    frames = sp.allocate_frames(seconds, total, 10)
    assert sum(frames) == total and all(count >= 1 for count in frames)


def test_multi_asset_scene_adds_up_to_the_audio(project):
    assets = [{"file": "a.png", "duration": 0.5}, {"file": "clip.mp4"}, {"file": "b.png", "duration": 0.3}]
    scene = {"id": "s1", "narration": SAY, "audio": "three.wav", "assets": assets}
    _write(project, [scene])
    manifest = sp.load_manifest(project)
    infos = sp.inspect_media(manifest, project)
    footage, rendered = sp.ensure_scene_visual(project, manifest, manifest.scenes[0], infos, 1.3)
    assert rendered == 4 and 1.3 <= _duration(footage) <= 1.3 + 0.5
    result = _build(project)
    assert abs(_duration(result["output"]) - 1.3) < 0.25


def test_mismatched_assets_fail_before_rendering(project):
    scene = {"id": "s1", "narration": SAY, "audio": "three.wav",
             "assets": [{"file": "a.png", "duration": 2}, {"file": "b.png", "duration": 2}]}
    _write(project, [scene])
    with pytest.raises(sp.PipelineError, match="add up"):
        _build(project)
    assert not (project / "render").exists()


def test_video_start_beyond_its_length_is_rejected(project):
    scene = {"id": "s1", "narration": SAY, "audio": "three.wav",
             "assets": [{"file": "clip.mp4", "start": 5}, {"file": "a.png"}]}
    _write(project, [scene])
    with pytest.raises(sp.ManifestError, match="start"):
        sp.inspect_media(sp.load_manifest(project), project)


def test_a_video_with_a_start_does_not_loop(project):
    scene = {"id": "s1", "narration": SAY, "audio": "three.wav",
             "assets": [{"file": "clip.mp4", "start": 1.5, "duration": 1.3}]}
    _write(project, [scene])
    with pytest.raises(sp.PipelineError, match="only loops"):
        _build(project)


def test_plan_reports_multi_asset_scenes(project, capsys):
    scene = {"id": "s1", "narration": SAY, "audio": "three.wav", "assets": [{"file": "a.png"}, {"file": "b.png"}]}
    _write(project, [scene])
    assert sp.main(["--project", str(project), "plan"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["visual"] == "multi"


# -- looks ----------------------------------------------------------------

@pytest.mark.parametrize("engine", ["zoompan", "scale_crop"])
@pytest.mark.parametrize("motion", ["zoom_out", "pan_left", "pan_right", "pan_up", "pan_down"])
def test_new_motions_move_the_picture(project, tmp_path, engine, motion):
    image = project / "assets" / "a.png"
    moving, still = tmp_path / "moving.mp4", tmp_path / "still.mp4"
    sp.render_image_clip(image, moving, duration=2, width=270, height=480, fps=10, motion=motion, engine=engine)
    sp.render_image_clip(image, still, duration=2, width=270, height=480, fps=10, motion="static")

    def ends(clip, name):
        first = _frame(clip, 0, tmp_path / f"{name}_first.png")
        _ff(["-sseof", "-0.15", "-i", str(clip), "-update", "1", "-frames:v", "1", str(tmp_path / f"{name}_last.png")])
        return _psnr(first, tmp_path / f"{name}_last.png")

    assert ends(still, "still") > ends(moving, "moving") + 3


def _fit_zoom(first_png, last_png):
    """Scale and focal point (as fractions of the frame) that best map the first
    frame onto the last. A coarse search on small grayscale copies."""
    import numpy as np

    size = (135, 240)
    with Image.open(first_png) as a, Image.open(last_png) as b:
        first = a.convert("L").resize(size, Image.BILINEAR)
        target = np.asarray(b.convert("L").resize(size, Image.BILINEAR), dtype=np.float32)
    best = None
    for scale in np.arange(1.0, 1.26, 0.01):
        inverse = 1.0 / scale
        for fx in np.arange(0.0, 1.01, 0.1):
            for fy in np.arange(0.0, 1.01, 0.1):
                data = (inverse, 0, fx * size[0] * (1 - inverse), 0, inverse, fy * size[1] * (1 - inverse))
                warped = np.asarray(first.transform(size, Image.AFFINE, data, Image.BILINEAR), dtype=np.float32)
                error = float(np.mean((warped - target) ** 2))
                if best is None or error < best[0]:
                    best = (error, float(scale), float(fx), float(fy))
    return best[1:]


def _aligned_psnr(first_png, second_png, reach=2):
    """PSNR of the interior of two frames, taking the best shift within ``reach`` px."""
    import math

    import numpy as np

    with Image.open(first_png) as a, Image.open(second_png) as b:
        first = np.asarray(a.convert("L"), dtype=np.float32)
        second = np.asarray(b.convert("L"), dtype=np.float32)
    margin = 10
    best = min(
        float(np.mean((first[margin:-margin, margin:-margin]
                       - np.roll(np.roll(second, dy, 0), dx, 1)[margin:-margin, margin:-margin]) ** 2))
        for dx in range(-reach, reach + 1) for dy in range(-reach, reach + 1)
    )
    return 99.0 if best == 0 else 10 * math.log10(255 ** 2 / best)


def _end_frames_of(clip, folder, name):
    first = _frame(clip, 0, folder / f"{name}_first.png")
    last = folder / f"{name}_last.png"
    # Frames are 0.1 s apart at 10 fps, so seek further back than one frame.
    _ff(["-sseof", "-0.15", "-i", str(clip), "-update", "1", "-frames:v", "1", str(last)])
    return first, last


@pytest.mark.parametrize("motion", ["zoom_in", "zoom_out"])
def test_both_engines_end_on_the_same_last_frame(project, tmp_path, motion):
    image = project / "assets" / "a.png"
    ends = {}
    for engine in ("zoompan", "scale_crop"):
        clip = tmp_path / f"{engine}.mp4"
        sp.render_image_clip(image, clip, duration=4, width=270, height=480, fps=10,
                             motion=motion, engine=engine)
        ends[engine] = _end_frames_of(clip, tmp_path, engine)
    # The engines round the crop window differently, so they can differ by about one
    # pixel; compare after allowing a shift of up to 2 px. The old top-left anchoring
    # was off by tens of pixels and fails this by a wide margin.
    for index in (0, 1):
        assert _aligned_psnr(ends["zoompan"][index], ends["scale_crop"][index]) > 30


@pytest.mark.parametrize("engine", ["zoompan", "scale_crop"])
@pytest.mark.parametrize("motion", ["zoom_in", "zoom_out"])
def test_zoom_is_centred_on_the_frame(project, tmp_path, engine, motion):
    clip = tmp_path / "zoom.mp4"
    sp.render_image_clip(project / "assets" / "a.png", clip, duration=4, width=270, height=480,
                         fps=10, motion=motion, engine=engine)
    first, last = _end_frames_of(clip, tmp_path, "zoom")
    # Zooming in maps first onto last; zooming out maps last onto first.
    scale, fx, fy = _fit_zoom(*((first, last) if motion == "zoom_in" else (last, first)))
    assert 1.03 <= scale <= 1.25
    assert abs(fx - 0.5) <= 0.1 and abs(fy - 0.5) <= 0.1


def test_contain_blur_fills_the_bars_with_a_blurred_copy(project):
    _write(project, [_scene("s1", asset="wide.png", fit="contain_blur")])
    frame = _frame(_build(project)["output"], 0.5, project / "contain.png")
    with Image.open(frame) as image:
        top = image.convert("L").crop((0, 0, 270, 40))
        assert sum(top.getdata()) / (270 * 40) > 25


def _phone_photo(path):
    """A phone-style photo: stored landscape (2000x1500) with EXIF orientation 6, so it
    is an upright 1500x2000 portrait when displayed (red top, blue bottom, white border)."""
    from PIL import ImageDraw

    upright = Image.new("RGB", (1500, 2000), (128, 128, 128))
    draw = ImageDraw.Draw(upright)
    draw.rectangle((0, 0, 1499, 499), fill=(220, 0, 0))
    draw.rectangle((0, 1500, 1499, 1999), fill=(0, 0, 220))
    draw.ellipse((350, 750, 1150, 1250), fill=(0, 0, 0))
    draw.rectangle((0, 0, 1499, 1999), outline=(255, 255, 255), width=14)
    exif = Image.Exif()
    exif[0x0112] = 6
    upright.rotate(90, expand=True).save(path, quality=95, exif=exif.tobytes())


def _rotated_video(path):
    """A 400x300 video whose display matrix turns it 90 degrees, as phones write it.
    ffmpeg 5.1 ignores the old rotate=90 metadata, so the track header is patched."""
    import struct

    plain = path.with_suffix(".plain.mp4")
    _ff(["-f", "lavfi", "-i", "testsrc=s=400x300:r=10:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(plain)])
    data = bytearray(plain.read_bytes())
    start = data.index(b"tkhd") - 4
    version = data[start + 8]
    offset = start + 8 + 4 + (20 if version == 0 else 32) + 8 + 8
    data[offset:offset + 36] = struct.pack(">9i", 0, 0x10000, 0, -0x10000, 0, 0, 0, 0, 0x40000000)
    path.write_bytes(bytes(data))


def test_rotated_photo_probes_as_upright(project):
    _phone_photo(project / "assets" / "phone.jpg")
    with Image.open(project / "assets" / "phone.jpg") as stored:
        assert stored.size == (2000, 1500)
    info = sp.probe_visual(project / "assets" / "phone.jpg")
    assert (info.width, info.height) == (1500, 2000)


def test_rotated_phone_video_probes_the_same_way(project):
    _rotated_video(project / "assets" / "phone.mp4")
    info = sp.probe_visual(project / "assets" / "phone.mp4")
    assert (info.kind, info.width, info.height) == ("video", 300, 400)


def test_unrotated_pictures_still_probe_as_stored(project):
    assert (sp.probe_visual(project / "assets" / "a.png").width, sp.probe_visual(project / "assets" / "a.png").height) == (270, 480)
    wide = sp.probe_visual(project / "assets" / "wide.png")
    assert (wide.width, wide.height) == (320, 160)
    clip = sp.probe_visual(project / "assets" / "clip.mp4")
    assert (clip.width, clip.height) == (270, 480)


def _runs(values):
    found, start, previous = [], None, None
    for value in values:
        if start is None:
            start = previous = value
        elif value == previous + 1:
            previous = value
        else:
            found.append((start, previous))
            start = previous = value
    if start is not None:
        found.append((start, previous))
    return found


def test_contain_blur_picture_box_of_a_rotated_photo_is_1080_by_1440(project):
    _phone_photo(project / "assets" / "phone.jpg")
    _write(project, [_scene("s1", asset="phone.jpg", fit="contain_blur", motion="static")],
           settings={"width": 1080, "height": 1920, "fps": 10})
    frame = _frame(_build(project)["output"], 0.5, project / "phone_contain.png")
    with Image.open(frame) as image:
        image = image.convert("RGB")
        assert image.size == (1080, 1920)
        column = [image.getpixel((540, y)) for y in range(1920)]
        row = [image.getpixel((x, 960)) for x in range(1080)]
    # The picture's sharp white border marks its box; the blurred background never gets that bright.
    rows = _runs([y for y, pixel in enumerate(column) if min(pixel) >= 245])
    columns = _runs([x for x, pixel in enumerate(row) if min(pixel) >= 245])
    assert len(rows) == 2, rows
    assert 238 <= rows[0][0] <= 246 and 1674 <= rows[1][1] <= 1682   # rows 240 to 1680
    assert columns[0][0] <= 4 and columns[-1][1] >= 1075             # full width
    assert column[200][0] > column[200][2]                           # blurred red above
    assert column[1720][2] > column[1720][0]                         # blurred blue below


def test_contain_blur_works_for_videos_too(project):
    _write(project, [_scene("s1", asset="clip.mp4", fit="contain_blur")])
    assert abs(_duration(_build(project)["output"]) - 1.4) < 0.2


# -- transitions, pauses and sync -----------------------------------------

def _sync(path):
    return abs(_stream_duration(path, "v:0") - _stream_duration(path, "a:0"))


def test_transition_shortens_the_timeline_by_the_overlap(project):
    scenes = [_scene("s1", transition={"type": "crossfade", "duration": 0.25}),
              _scene("s2", "two.wav", asset="b.png"), _scene("s3", "three.wav")]
    _write(project, scenes)
    result = _build(project)
    # 14 + 9 + 13 frames at 10 fps, minus a 2-frame overlap (0.25 s rounds to 2 frames).
    assert result["frames"] == 34
    assert abs(_duration(result["output"]) - 3.4) < 0.15
    assert _sync(result["output"]) < 0.1


@pytest.mark.parametrize("kind", [k for k in sp.TRANSITION_TYPES if k != "cut"])
def test_every_transition_type_keeps_length_and_sync(project, kind):
    _write(project, [_scene("s1", transition={"type": kind, "duration": 0.3}), _scene("s2", "two.wav", asset="b.png")])
    result = _build(project)
    assert result["frames"] == 14 + 9 - 3
    assert abs(_duration(result["output"]) - 2.0) < 0.15
    assert _sync(result["output"]) < 0.1


@pytest.mark.parametrize("duration", [0.2, 0.33, 0.5, 0.71])
def test_fractional_transition_durations(project, duration):
    _write(project, [_scene("s1", transition={"type": "crossfade", "duration": duration}),
                     _scene("s2", "two.wav", asset="b.png")])
    result = _build(project)
    shared = round(duration * 10)
    assert result["frames"] == 14 + 9 - shared
    assert abs(_duration(result["output"]) - result["frames"] / 10) < 0.15
    assert _sync(result["output"]) < 0.1


@pytest.mark.parametrize("duration,accepted", [(0.1, False), (0.14, False), (0.15, True), (0.2, True)])
def test_transition_under_two_frames_is_rejected_before_rendering(project, duration, accepted):
    _write(project, [_scene("s1", transition={"type": "crossfade", "duration": duration}),
                     _scene("s2", "two.wav", asset="b.png")])
    if accepted:
        assert sp.load_manifest(project).scenes[0].transition.duration == duration
        return
    with pytest.raises(sp.ManifestError) as caught:
        _build(project)
    message = str(caught.value)
    assert "under 2 frames at 10 fps" in message and "minimum" in message and "0.2s" in message
    assert not (project / "render").exists()
    assert not (project / ".scene_cache").exists()


def test_minimum_transition_follows_the_frame_rate(project):
    _write(project, [_scene("s1", transition={"type": "crossfade", "duration": 0.1}),
                     _scene("s2", "two.wav", asset="b.png")], settings={**SETTINGS, "fps": 30})
    assert sp.load_manifest(project).scenes[0].transition.duration == 0.1
    _write(project, [_scene("s1", transition={"type": "crossfade", "duration": 0.1}),
                     _scene("s2", "two.wav", asset="b.png")], settings={**SETTINGS, "fps": 24})
    assert sp.load_manifest(project).scenes[0].transition.duration == 0.1
    _write(project, [_scene("s1", transition={"type": "crossfade", "duration": 0.1}),
                     _scene("s2", "two.wav", asset="b.png")], settings={**SETTINGS, "fps": 12})
    assert "minimum" in _errors(project)


def test_picture_and_narration_stay_in_sync_over_many_transitions(project):
    scenes = [_scene(f"s{i}", ("one.wav", "two.wav", "three.wav")[i % 3],
                     transition={"type": "crossfade", "duration": 0.2} if i < 7 else None) for i in range(8)]
    for scene in scenes:
        if scene.get("transition") is None:
            scene.pop("transition", None)
    _write(project, scenes)
    result = _build(project)
    assert abs(_duration(result["output"]) - result["frames"] / 10) < 0.2
    assert _sync(result["output"]) < 0.15


def test_transition_longer_than_the_scene_allows_is_rejected(project):
    _write(project, [_scene("s1", transition={"type": "crossfade", "duration": 0.6}),
                     _scene("s2", "two.wav", transition={"type": "crossfade", "duration": 0.6}),
                     _scene("s3", "three.wav")])
    with pytest.raises(sp.PipelineError, match="transitions need"):
        _build(project)
    assert not (project / "render").exists()


def test_pause_after_extends_the_scene(project):
    _write(project, [_scene("s1", pause_after=0.5), _scene("s2", "two.wav", asset="b.png")])
    result = _build(project)
    assert result["frames"] == round((1.37 + 0.5) * 10) + 9
    assert abs(_duration(result["output"]) - result["frames"] / 10) < 0.15
    assert _sync(result["output"]) < 0.1


def test_transition_over_speech_warns_and_a_pause_silences_the_warning(project):
    _write(project, [_scene("s1", transition={"type": "crossfade", "duration": 0.5}),
                     _scene("s2", "two.wav", asset="b.png")])
    assert "overlaps the end of its narration" in _build(project)["warnings"][0]
    _write(project, [_scene("s1", transition={"type": "crossfade", "duration": 0.5}, pause_after=0.5),
                     _scene("s2", "two.wav", asset="b.png")])
    assert _build(project)["warnings"] == []


# -- background music ------------------------------------------------------

@pytest.mark.parametrize("music,note", [("music_short.wav", "loops"), ("music_long.wav", "is trimmed")])
def test_music_length_matches_the_video(project, music, note):
    _write(project, [_scene("s1"), _scene("s2", "two.wav", asset="b.png")], bgm={"file": music, "volume": 0.5})
    result = _build(project)
    assert abs(_duration(result["output"]) - 2.3) < 0.15, note
    assert abs(_stream_duration(result["output"], "a:0") - 2.3) < 0.15


def test_music_is_ducked_under_the_narration(project):
    ducking = {"amount": 12, "attack": 20, "release": 200}
    scene = _scene("s1", "voice200.wav", pause_after=2.0)
    _write(project, [scene], bgm={"file": "music_long.wav", "volume": 1.0, "ducking": ducking})
    ducked = Path(_build(project)["output"])
    shutil.copyfile(ducked, project / "ducked.mp4")
    _write(project, [scene], bgm={"file": "music_long.wav", "volume": 1.0})
    flat = _build(project)["output"]
    spoken, silent = (0.5, 0.7), (2.5, 0.7)
    gap = _level(project / "ducked.mp4", *silent) - _level(project / "ducked.mp4", *spoken)
    assert gap > 6
    assert abs(_level(flat, *silent) - _level(flat, *spoken)) < 3


def test_duck_ratio_grows_with_the_amount():
    assert scene_post.duck_ratio(0) == 1.0
    assert scene_post.duck_ratio(6) < scene_post.duck_ratio(12) < scene_post.duck_ratio(18) <= 20


def test_music_file_must_exist_and_have_audio(project):
    _write(project, [_scene("s1")], bgm={"file": "music_short.wav"})
    (project / "audio" / "music_short.wav").write_bytes(b"not audio")
    with pytest.raises(sp.ManifestError, match="bgm"):
        sp.inspect_media(sp.load_manifest(project), project)


# -- caching across a project ----------------------------------------------

def test_changing_one_scene_rebuilds_it_its_junctions_and_the_later_stages(project):
    scenes = [_scene("s1", transition={"type": "crossfade", "duration": 0.2}),
              _scene("s2", "two.wav", asset="b.png", transition={"type": "crossfade", "duration": 0.2}),
              _scene("s3", "three.wav")]
    _write(project, scenes, bgm={"file": "music_long.wav", "volume": 0.3, "ducking": {}})
    first = _build(project)
    assert not any(first["post_stages"].values())
    again = _build(project)
    assert all(again["post_stages"].values())

    scenes[1]["text_cards"] = [{"text": "Changed", "start": 0.2, "end": 0.6}]
    _write(project, scenes, bgm={"file": "music_long.wav", "volume": 0.3, "ducking": {}})
    picture_only = _build(project)
    assert _rebuilt(picture_only) == {"s2:picture", "s1>s2:picture", "s2>s3:picture", "join:picture", "final"}
    assert all(picture_only["stages"].values())

    scenes[0]["audio"] = "three.wav"
    _write(project, scenes, bgm={"file": "music_long.wav", "volume": 0.3, "ducking": {}})
    audio_change = _build(project)
    rebuilt = _rebuilt(audio_change)
    assert {"s1:narration", "s1:picture", "s1>s2:narration", "s1>s2:picture", "join:picture",
            "join:narration", "final"} <= rebuilt
    assert "s3:picture" not in rebuilt and "s3:narration" not in rebuilt


def test_changing_the_music_rebuilds_only_the_final_mux(project):
    scenes = [_scene("s1"), _scene("s2", "two.wav", asset="b.png")]
    _write(project, scenes, bgm={"file": "music_long.wav", "volume": 0.3})
    _build(project)
    _write(project, scenes, bgm={"file": "music_long.wav", "volume": 0.6})
    assert _rebuilt(_build(project)) == {"final"}


# -- stage A: per-asset fit, detailed plan, output name --------------------

def _two_wide(first_fit=None, scene_fit=None):
    first = {"file": "wide.png", "duration": 0.6}
    if first_fit:
        first["fit"] = first_fit
    scene = {"id": "s1", "narration": SAY, "audio": "three.wav",
             "assets": [first, {"file": "wide.png"}]}
    if scene_fit:
        scene["fit"] = scene_fit
    return scene


def test_asset_fit_must_be_a_known_fit(project):
    scene = _two_wide()
    scene["assets"][0]["fit"] = "stretch"
    _write(project, [scene])
    assert "assets[0].fit: must be one of cover, contain_blur" in _errors(project)


def test_asset_fit_overrides_the_scene_fit_for_that_asset_only(project, tmp_path):
    _write(project, [_two_wide(first_fit="contain_blur", scene_fit="cover")])
    mixed_out = _build(project)["output"]
    mixed_early = _frame(mixed_out, 0.3, tmp_path / "mixed_early.png")
    mixed_late = _frame(mixed_out, 1.0, tmp_path / "mixed_late.png")
    shutil.rmtree(project / "out")
    shutil.rmtree(project / "render_v1", ignore_errors=True)
    _write(project, [_two_wide(scene_fit="contain_blur")])
    blur_out = _build(project)["output"]
    blur_early = _frame(blur_out, 0.3, tmp_path / "blur_early.png")
    blur_late = _frame(blur_out, 1.0, tmp_path / "blur_late.png")
    # first asset: contain_blur in both builds; second asset: cover vs contain_blur
    assert _psnr(mixed_early, blur_early) > 40
    assert _psnr(mixed_late, blur_late) < 30


def test_plan_lists_durations_and_cards_against_the_scene_length(project, capsys):
    scene = _two_wide(first_fit="contain_blur")
    scene["text_cards"] = [{"text": "Inside", "start": 0.1, "end": 0.9}]
    _write(project, [scene])
    assert sp.main(["--project", str(project), "plan"]) == 0
    entry = json.loads(capsys.readouterr().out)[0]
    assert entry["visual"] == "multi" and entry["trimmed_seconds"] == pytest.approx(1.3, abs=0.05)
    assert entry["frames"] == 13 and entry["length_seconds"] == pytest.approx(1.3)
    first, second = entry["assets"]
    assert first["fit"] == "contain_blur" and second["fit"] == "cover"
    assert first["seconds"] == pytest.approx(0.6) and first["starts"] == 0
    assert second["seconds"] == pytest.approx(0.7, abs=0.05)
    assert entry["cards"][0]["seconds_before_scene_end"] == pytest.approx(0.4, abs=0.05)
    assert entry["problems"] == []


def test_plan_reports_problems_and_exits_with_one(project, capsys):
    scene = _scene("s1", text_cards=[{"text": "Late", "start": 0, "end": 30}])
    two = _scene("s2", "two.wav", text_cards=[
        {"text": "One", "start": 0.1, "end": 0.6}, {"text": "Two", "start": 0.4, "end": 0.8}])
    _write(project, [scene, two])
    assert sp.main(["--project", str(project), "plan", "--text"]) == 1
    text = capsys.readouterr().out
    assert "PROBLEM: text card 1 ends at 30s but the scene lasts" in text
    assert "text cards 1 and 2 are on screen together" in text
    assert "2 problem(s)" in text


def test_plan_reports_assets_that_do_not_fit_the_narration(project, capsys):
    scene = {"id": "s1", "narration": SAY, "audio": "one.wav",
             "assets": [{"file": "a.png", "duration": 5}, {"file": "b.png"}]}
    _write(project, [scene])
    assert sp.main(["--project", str(project), "plan"]) == 1
    assert json.loads(capsys.readouterr().out)[0]["problems"]


def test_plan_writes_nothing_into_the_project(project, capsys):
    _write(project, [_scene("s1")])
    before = sorted(str(p.relative_to(project)) for p in project.rglob("*"))
    sp.main(["--project", str(project), "plan"])
    capsys.readouterr()
    assert sorted(str(p.relative_to(project)) for p in project.rglob("*")) == before


def test_plan_with_pending_narration_checks_what_it_can(project, capsys):
    scene = {"id": "s1", "asset": "a.png", "narration": SAY}
    _write(project, [scene, _scene("s2", "two.wav", "b.png")], voice={"name": "en-US-AriaNeural"})
    assert sp.main(["--project", str(project), "plan", "--text"]) == 0
    text = capsys.readouterr().out
    assert "pending" in text and "narration is not generated yet" in text


def test_plan_sums_transitions_into_the_total(project, capsys):
    _write(project, [_scene("s1", transition={"type": "crossfade", "duration": 0.3}), _scene("s2", "two.wav", "b.png")])
    sp.main(["--project", str(project), "plan"])
    entry = json.loads(capsys.readouterr().out)[0]
    assert entry["transition"]["frames"] == 3
    built = _build(project)
    assert _duration(built["output"]) == pytest.approx(
        (14 + 9 - 3) / 10, abs=0.15)


def test_output_name_keeps_the_earlier_file(project):
    _write(project, [_scene("s1")])
    first = _build(project)["output"]
    again = sp.build(project, synthesize=_forbidden, progress=lambda _m: None, output_name="take2")
    assert Path(again["output"]).name == "take2.mp4" and Path(first).name == "ep.mp4"
    assert Path(first).is_file() and Path(again["output"]).is_file()


def test_output_name_refuses_to_replace_without_overwrite(project):
    _write(project, [_scene("s1")])
    sp.build(project, synthesize=_forbidden, progress=lambda _m: None, output_name="take2")
    before = (project / "out" / "take2.mp4").stat().st_mtime_ns
    with pytest.raises(sp.PipelineError, match="already exists"):
        sp.build(project, synthesize=_forbidden, progress=lambda _m: None, output_name="take2")
    assert (project / "out" / "take2.mp4").stat().st_mtime_ns == before
    sp.build(project, synthesize=_forbidden, progress=lambda _m: None, output_name="take2", overwrite=True)


@pytest.mark.parametrize("name", ["", "a/b", "..", ".hidden", "x y", "a.mp4", "n" * 65])
def test_output_name_must_be_a_plain_name(project, name):
    _write(project, [_scene("s1")])
    with pytest.raises(sp.PipelineError):
        sp.build(project, synthesize=_forbidden, progress=lambda _m: None, output_name=name)


def test_output_name_also_works_on_the_post_path(project):
    _write(project, [_scene("s1", transition={"type": "crossfade", "duration": 0.3}), _scene("s2", "two.wav", "b.png")],
           bgm={"file": "music_long.wav", "volume": 0.3})
    result = sp.build(project, synthesize=_forbidden, progress=lambda _m: None, output_name="alt")
    assert Path(result["output"]).name == "alt.mp4" and (project / "out" / "alt.mp4").is_file()
    assert not (project / "out" / "ep.mp4").exists()


# -- stage B: loudness ------------------------------------------------------

def _loud(root, target=-20, peak=-2, **loudness):
    scenes = [_scene("s1"), _scene("s2", "two.wav", "b.png")]
    settings = {**SETTINGS, "loudness": {"target_lufs": target, "true_peak_db": peak, **loudness}}
    manifest = {"schema_version": 1, "project_id": "ep", "settings": settings, "scenes": scenes}
    (root / "scenes.json").write_text(json.dumps(manifest), encoding="utf-8")


def _video_md5(path):
    done = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path), "-map", "0:v:0",
                           "-c", "copy", "-f", "md5", "-"], capture_output=True, text=True)
    return done.stdout.strip()


@pytest.mark.parametrize("value,message", [
    ("loud", "settings.loudness: must be an object"),
    ({"target_lufs": "high"}, "settings.loudness.target_lufs: must be a number from -50 to -5"),
    ({"target_lufs": -2}, "settings.loudness.target_lufs"),
    ({"true_peak_db": 1}, "settings.loudness.true_peak_db: must be a number from -9 to 0"),
    ({"true_peek_db": -1}, "settings.loudness: unknown field: true_peek_db"),
])
def test_loudness_setting_is_validated(project, value, message):
    manifest = {"schema_version": 1, "project_id": "ep", "settings": {**SETTINGS, "loudness": value},
                "scenes": [_scene("s1")]}
    (project / "scenes.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert message in _errors(project)


def test_loudness_defaults_apply_when_only_one_value_is_given(project):
    manifest = {"schema_version": 1, "project_id": "ep", "settings": {**SETTINGS, "loudness": {}},
                "scenes": [_scene("s1")]}
    (project / "scenes.json").write_text(json.dumps(manifest), encoding="utf-8")
    loudness = sp.load_manifest(project).loudness
    assert (loudness.target_lufs, loudness.true_peak_db) == (-14.0, -1.5)


def test_loudness_is_off_unless_set(project):
    _write(project, [_scene("s1")], bgm={"file": "music_long.wav", "volume": 0.3})
    assert sp.load_manifest(project).loudness is None
    result = _build(project)
    assert "loudness" not in result and "loudness" not in result["post_stages"]
    assert not list((project / "out").glob("*.loudnorm.*"))


def test_loudness_writes_a_second_file_with_the_report(project):
    _loud(project)
    result = _build(project)
    report = result["loudness"]
    normal, loud = Path(result["output"]), Path(report["file"])
    assert normal.name == "ep.mp4" and loud.name == "ep.loudnorm.mp4" and loud.is_file()
    assert json.loads(Path(report["report"]).read_text()) == {
        k: v for k, v in report.items() if k not in ("file", "report")}
    assert report["mode"] in ("linear", "dynamic")
    assert report["ffmpeg_label"] in ("linear", "dynamic", "")
    for side in ("input", "output", "predicted_output"):
        assert set(report[side]) == {"lufs", "true_peak_db", "lra"}
    assert report["output"]["lufs"] == pytest.approx(-20, abs=1.5)
    # the original build output is not replaced by the normalised one
    assert _video_md5(normal) == _video_md5(loud)
    assert _duration(loud) == pytest.approx(_duration(normal), abs=0.1)


def test_loudness_mode_is_linear_when_the_gain_fits_and_dynamic_when_it_does_not(project):
    _loud(project, target=-24, peak=-2)
    assert _build(project)["loudness"]["mode"] == "linear"
    _loud(project, target=-8, peak=-9)
    report = _build(project)["loudness"]
    assert report["mode"] == "dynamic"
    # loudnorm could not reach the target here; the report and a warning say so
    assert abs(report["output"]["lufs"] - report["target_lufs"]) > 1
    assert any("more than" in text for text in scene_post.loudness_warnings(report))


def test_loudness_stage_is_cached_and_follows_its_settings(project):
    _loud(project)
    first = _build(project)
    assert first["post_stages"]["loudness"] is False
    again = _build(project)
    assert again["post_stages"]["loudness"] is True
    assert again["loudness"]["output"] == first["loudness"]["output"]
    _loud(project, target=-22)
    changed = _build(project)
    assert _rebuilt(changed) == {"loudness"}
    assert changed["loudness"]["target_lufs"] == -22


def test_loudnorm_mode_is_decided_from_gain_and_peak_not_from_ffmpeg_label():
    target = sp.Loudness(-14.0, -1.5)
    quiet = {"lufs": -20.0, "true_peak_db": -12.0, "lra": 3.0}
    # gain of +6 dB keeps the peak at -6, under the limit, and the output shows +6
    assert scene_post.loudnorm_mode(quiet, {"lufs": -14.1, "true_peak_db": -6.2, "lra": 3.0}, target) == "linear"
    # same gain but the output did not move by that gain: loudnorm fell back
    assert scene_post.loudnorm_mode(quiet, {"lufs": -22.0, "true_peak_db": -14.0, "lra": 1.0}, target) == "dynamic"
    # peaky input: +6 dB would put the peak at +1.0 dBTP, over the limit
    peaky = {"lufs": -20.0, "true_peak_db": -5.0, "lra": 3.0}
    assert scene_post.loudnorm_mode(peaky, {"lufs": -14.0, "true_peak_db": 1.0, "lra": 3.0}, target) == "dynamic"


def test_loudness_warnings_name_what_is_off():
    report = {"mode": "dynamic", "target_lufs": -14.0, "true_peak_db": -1.5,
              "output": {"lufs": -17.2, "true_peak_db": -0.4, "lra": 5.0}}
    text = " ".join(scene_post.loudness_warnings(report))
    assert "-17.2 LUFS" in text and "dynamic" in text and "-0.4 dBTP" in text
    report["output"] = {"lufs": -14.1, "true_peak_db": -1.6, "lra": 5.0}
    assert scene_post.loudness_warnings(report) == []


def test_loudnorm_output_that_has_no_json_is_an_error():
    with pytest.raises(sp.PipelineError, match="no measurements"):
        scene_post._loudnorm_json("nothing useful here", "pass")
    with pytest.raises(sp.PipelineError, match="silent"):
        scene_post._number({"input_i": "-inf"}, "input_i", "pass")


def test_loudness_with_output_name_and_overwrite_check(project):
    _loud(project)
    sp.build(project, synthesize=_forbidden, progress=lambda _m: None, output_name="take3")
    assert (project / "out" / "take3.loudnorm.mp4").is_file()
    assert (project / "out" / "take3.loudnorm.json").is_file()
    (project / "out" / "take3.mp4").unlink()
    with pytest.raises(sp.PipelineError, match="take3.loudnorm.mp4 already exists"):
        sp.build(project, synthesize=_forbidden, progress=lambda _m: None, output_name="take3")


def test_plan_shows_the_loudness_target(project, capsys):
    _loud(project, target=-16, peak=-1)
    assert sp.main(["--project", str(project), "plan", "--text"]) == 0
    assert "normalise to -16 LUFS, true peak -1 dBTP" in capsys.readouterr().out


def test_loudnorm_gets_a_true_peak_target_below_the_requested_limit():
    loudness = sp.Loudness(-14.0, -1.5)
    assert scene_post.loudnorm_peak(loudness) == pytest.approx(-1.8)
    assert scene_post._target_filter(loudness) == "I=-14:TP=-1.8:LRA=11"


def test_linear_mode_needs_room_under_the_lowered_target():
    loudness = sp.Loudness(-14.0, -1.5)
    before = {"lufs": -20.0, "true_peak_db": -7.7, "lra": 3.0}
    # +6 dB puts the peak at -1.7: under -1.5 but over the -1.8 handed to loudnorm
    assert scene_post.loudnorm_mode(before, {"lufs": -14.0, "true_peak_db": -1.7, "lra": 3.0}, loudness) == "dynamic"


def test_written_file_stays_under_the_requested_true_peak(project):
    _loud(project, target=-16, peak=-3)
    report = _build(project)["loudness"]
    assert report["output"]["true_peak_db"] <= report["true_peak_db"] + 0.05


def test_peak_headroom_never_asks_loudnorm_for_less_than_minus_nine():
    assert scene_post.loudnorm_peak(sp.Loudness(-14.0, -9.0)) == -9.0
    assert scene_post.loudnorm_peak(sp.Loudness(-14.0, -8.8)) == -9.0
    assert scene_post.loudnorm_peak(sp.Loudness(-14.0, 0.0)) == pytest.approx(-0.3)


# -- stage C: random music from a folder -------------------------------------

def _music_folder(root, names=("c_calm.wav", "a_bright.wav", "b_mellow.wav", "d_low.wav"), folder="music"):
    (root / "audio" / folder).mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(names):  # created out of order on purpose
        _tone(root / "audio" / folder / name, 3, 400 + 150 * index, 4)
    return root / "audio" / folder


def _bgm_manifest(root, bgm, scenes=None):
    scenes = scenes or [_scene("s1"), _scene("s2", "two.wav", "b.png")]
    _write(root, scenes, bgm=bgm)


def _chosen(root):
    return sp.load_manifest(root).bgm.chosen


@pytest.mark.parametrize("bgm,message", [
    ({"file": "music_long.wav", "folder": "music"}, "bgm: set either file or folder, not both"),
    ({"volume": 0.3}, "bgm: set file (one track) or folder (pick one at random)"),
    ({"file": "music_long.wav", "seed": 3}, "bgm.seed: only used with bgm.folder"),
    ({"folder": "music", "seed": -1}, "bgm.seed: must be a whole number from 0 up"),
    ({"folder": "music", "seed": 1.5}, "bgm.seed: must be a whole number from 0 up"),
    ({"folder": "music", "seed": True}, "bgm.seed: must be a whole number from 0 up"),
    ({"folder": "music", "seed": "7"}, "bgm.seed: must be a whole number from 0 up"),
    ({"folder": "../music"}, "bgm.folder: must be a plain folder name"),
    ({"folder": "a/b"}, "bgm.folder: must be a plain folder name"),
    ({"folder": ".hidden"}, "bgm.folder: must be a plain folder name"),
    ({"folder": ""}, "bgm.folder: must be a plain folder name"),
    ({"folder": 3}, "bgm.folder: must be a plain folder name"),
    ({"folder": "nothere"}, "bgm.folder: 'nothere' is not a folder in audio/"),
])
def test_music_folder_setting_is_validated(project, bgm, message):
    _music_folder(project)
    _bgm_manifest(project, bgm)
    assert message in _errors(project)


def test_music_folder_without_audio_files_is_rejected(project):
    folder = _music_folder(project, names=())
    (folder / "notes.txt").write_text("not music")
    (folder / ".hidden.wav").write_bytes(b"")
    _bgm_manifest(project, {"folder": "music"})
    assert "no audio files in audio/music/" in _errors(project)


def test_a_file_must_be_directly_inside_the_folder(project):
    folder = _music_folder(project, names=())
    (folder / "nested").mkdir()
    _tone(folder / "nested" / "deep.wav", 2)
    _bgm_manifest(project, {"folder": "music"})
    assert "no audio files in audio/music/" in _errors(project)


def test_nothing_is_picked_from_outside_the_folder(project, tmp_path):
    folder = _music_folder(project)
    outside = tmp_path / "elsewhere.wav"
    _tone(outside, 2)
    (folder / "zz_link.wav").symlink_to(outside)
    _bgm_manifest(project, {"folder": "music"})
    assert "symbolic links are not allowed: music/zz_link.wav" in _errors(project)
    (folder / "zz_link.wav").unlink()
    (project / "audio" / "linked").symlink_to(folder)
    _bgm_manifest(project, {"folder": "linked"})
    assert "bgm.folder: symbolic links are not allowed" in _errors(project)


def test_top_level_audio_files_are_never_candidates(project):
    folder = _music_folder(project, names=("only.wav",))
    for seed in range(8):
        _bgm_manifest(project, {"folder": "music", "seed": seed})
        bgm = sp.load_manifest(project).bgm
        assert bgm.file == "music/only.wav" and bgm.candidates == 1
    assert folder.parent.joinpath("music_long.wav").is_file()


def test_the_same_seed_picks_the_same_file_whatever_the_creation_order(project):
    _music_folder(project)
    _bgm_manifest(project, {"folder": "music", "seed": 5})
    first = _chosen(project)
    shutil.rmtree(project / "audio" / "music")
    _music_folder(project, names=("d_low.wav", "b_mellow.wav", "a_bright.wav", "c_calm.wav"))
    # the tones differ by name now, but the choice depends only on seed and sorted names
    assert _chosen(project) == first
    names = sorted(p.name for p in (project / "audio" / "music").iterdir())
    assert first == names[sp.music_index(5, 4)]


def test_different_seeds_reach_different_files_and_stay_in_range(project):
    _music_folder(project)
    seen = set()
    for seed in range(30):
        index = sp.music_index(seed, 4)
        assert 0 <= index < 4 and index == sp.music_index(seed, 4)
        seen.add(index)
    assert len(seen) >= 3


def test_build_records_the_pick_in_the_result_and_next_to_the_output(project):
    _music_folder(project)
    _bgm_manifest(project, {"folder": "music", "seed": 11, "volume": 0.3})
    result = _build(project)
    music = result["music"]
    assert music["folder"] == "music" and music["seed"] == 11 and music["candidates"] == 4
    assert music["chosen"] == _chosen(project) and music["file"] == f"music/{music['chosen']}"
    record = json.loads((project / "out" / "ep.music.json").read_text())
    assert record == {k: v for k, v in music.items() if k != "record"}
    assert Path(music["record"]) == project / "out" / "ep.music.json"


def test_rebuild_with_the_same_seed_is_repeatable_and_reuses_every_stage(project):
    _music_folder(project)
    _bgm_manifest(project, {"folder": "music", "seed": 2, "volume": 0.3})
    first = _build(project)
    again = _build(project)
    assert again["music"] == first["music"] and _rebuilt(again) == set()


def test_a_different_pick_rebuilds_only_the_final_mix(project):
    _music_folder(project)
    one = next(s for s in range(50) if sp.music_index(s, 4) == 0)
    two = next(s for s in range(50) if sp.music_index(s, 4) == 2)
    _bgm_manifest(project, {"folder": "music", "seed": one, "volume": 0.3})
    first = _build(project)
    _bgm_manifest(project, {"folder": "music", "seed": two, "volume": 0.3})
    second = _build(project)
    assert first["music"]["chosen"] != second["music"]["chosen"]
    assert _rebuilt(second) == {"final"}


def test_single_file_music_has_no_pick_record(project):
    _write(project, [_scene("s1")], bgm={"file": "music_long.wav", "volume": 0.3})
    result = _build(project)
    assert "music" not in result and not list((project / "out").glob("*.music.json"))


def test_plan_shows_which_file_the_seed_picked(project, capsys):
    _music_folder(project)
    _bgm_manifest(project, {"folder": "music", "seed": 9})
    chosen = _chosen(project)
    assert sp.main(["--project", str(project), "plan", "--text"]) == 0
    assert f"music: {chosen} picked from audio/music/ with seed 9 (from manifest), 4 files" in capsys.readouterr().out
    assert sp.main(["--project", str(project), "plan", "--summary"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["music"] == {"folder": "music", "seed": 9, "seed_source": "manifest",
                               "candidates": 4, "chosen": chosen}
    assert capsys.readouterr().out == ""


def test_music_record_is_not_replaced_by_a_named_build_without_overwrite(project):
    _music_folder(project)
    _bgm_manifest(project, {"folder": "music", "seed": 1, "volume": 0.3})
    sp.build(project, synthesize=_forbidden, progress=lambda _m: None, output_name="take4")
    (project / "out" / "take4.mp4").unlink()
    with pytest.raises(sp.PipelineError, match="take4.music.json already exists"):
        sp.build(project, synthesize=_forbidden, progress=lambda _m: None, output_name="take4")


def test_loudness_line_appears_once_in_the_text_plan(project, capsys):
    _loud(project, target=-16, peak=-1)
    sp.main(["--project", str(project), "plan", "--text"])
    assert capsys.readouterr().out.count("loudness: normalise to") == 1


def _named_project(root, project_id, bgm):
    scenes = [_scene("s1")]
    manifest = {"schema_version": 1, "project_id": project_id, "settings": dict(SETTINGS),
                "scenes": scenes, "bgm": bgm}
    (root / "scenes.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_default_seed_comes_from_the_project_id(project):
    import hashlib

    _music_folder(project)
    expected = int.from_bytes(hashlib.sha256(b"scene-music-seed-v1:episode-7").digest()[:8], "big") % 2**63
    assert sp.default_music_seed("episode-7") == expected
    assert sp.default_music_seed("episode-7") == sp.default_music_seed("episode-7")
    _named_project(project, "episode-7", {"folder": "music"})
    bgm = sp.load_manifest(project).bgm
    assert bgm.seed == expected and bgm.seed_explicit is False
    names = sorted(p.name for p in (project / "audio" / "music").iterdir())
    assert bgm.chosen == names[sp.music_index(expected, 4)]


def test_different_projects_pick_differently_by_default(project):
    _music_folder(project)
    picks = set()
    for number in range(12):
        _named_project(project, f"show-{number}", {"folder": "music"})
        picks.add(_chosen(project))
    assert len(picks) >= 3


def test_an_explicit_seed_overrides_the_project_id_default(project):
    _music_folder(project)
    _named_project(project, "episode-7", {"folder": "music", "seed": 0})
    bgm = sp.load_manifest(project).bgm
    assert bgm.seed == 0 and bgm.seed_explicit is True
    _named_project(project, "other-name", {"folder": "music", "seed": 0})
    assert _chosen(project) == bgm.chosen


def test_music_record_says_where_the_seed_came_from(project):
    _music_folder(project)
    _named_project(project, "episode-7", {"folder": "music", "volume": 0.3})
    result = _build(project)
    assert result["music"]["seed_source"] == "project_id"
    assert result["music"]["seed"] == sp.default_music_seed("episode-7")
    record = json.loads((project / "out" / "episode-7.music.json").read_text())
    assert record["seed_source"] == "project_id"
    _named_project(project, "episode-7", {"folder": "music", "volume": 0.3, "seed": 4})
    assert _build(project)["music"]["seed_source"] == "manifest"


# -- D1: claim card in real frames -------------------------------------------

def _luma(image, box):
    return sum(image.convert("L").crop(box).getdata()) / ((box[2] - box[0]) * (box[3] - box[1]))


def _claim_scene(**card_style):
    card = {"kind": "claim", "label": "Page Six", "text": "Hit his head on a bunk", "start": 0.2,
            "end": 1.0, "style": {"position": "center", **card_style}}
    return _scene("s1", audio="three.wav", motion="static", text_cards=[card])


def _claim_style(manifest, card):
    text = scene_post.scene_text
    return text.resolve_style("card", 270, 480, manifest.text_card_style, text.KIND_DEFAULTS[card.kind],
                              card.style)


def _card_items(manifest):
    card = manifest.scenes[0].text_cards[0]
    return scene_post.scene_text.card_items(card, _claim_style(manifest, card), 270, 480)


def _card_rect(project):
    return next(i for i in _card_items(sp.load_manifest(project))
                if isinstance(i, scene_post.scene_text.DrawBox))


def test_claim_card_darkens_a_panel_only_while_it_is_shown(project):
    _write(project, [_claim_scene()])
    panel = _card_rect(project)
    out = _build(project)["output"]
    inside = Image.open(_frame(out, 0.6, project / "in.png"))
    outside = Image.open(_frame(out, 1.2, project / "out.png"))
    box = (panel.x, panel.y, panel.x + panel.w, panel.y + panel.h)
    assert _luma(inside, box) < 0.6 * _luma(outside, box)


def test_claim_card_does_not_touch_the_side_margins(project):
    _write(project, [_claim_scene(position="top")])
    out = _build(project)["output"]
    inside = Image.open(_frame(out, 0.6, project / "in.png")).convert("L")
    outside = Image.open(_frame(out, 1.2, project / "out.png")).convert("L")
    margin = round(scene_post.scene_text.MARGIN_X * 270)
    for strip in ((0, 0, margin - 1, 480), (270 - margin + 1, 0, 270, 480)):
        diff = sum(abs(a - b) for a, b in zip(inside.crop(strip).getdata(), outside.crop(strip).getdata()))
        assert diff / ((strip[2] - strip[0]) * 480) < 3


def test_claim_label_is_drawn_in_the_accent_colour(project):
    _write(project, [_claim_scene(accent_color="#FF2020")])
    panel = _card_rect(project)
    inside = Image.open(_frame(_build(project)["output"], 0.6, project / "in.png")).convert("RGB")
    crop = inside.crop((panel.x, panel.y, panel.x + panel.w, panel.y + panel.h))
    reddish = [p for p in crop.getdata() if p[0] > 180 and p[1] < 90 and p[2] < 90]
    assert len(reddish) >= 8


def test_claim_card_is_gone_after_its_window(project):
    _write(project, [_claim_scene()])
    out = _build(project)["output"]
    late = _frame(out, 1.2, project / "late.png")
    _write(project, [_scene("s1", audio="three.wav", motion="static")])
    bare = _frame(_build(project)["output"], 1.2, project / "bare.png")
    assert _psnr(late, bare) > 35


def test_plan_describes_a_claim_card(project, capsys):
    _write(project, [_claim_scene()])
    assert sp.main(["--project", str(project), "plan", "--text"]) == 0
    text = capsys.readouterr().out
    assert "claim" in text and "Page Six: Hit his head on a bunk" in text
    assert sp.main(["--project", str(project), "plan"]) == 0
    card = json.loads(capsys.readouterr().out)[0]["cards"][0]
    assert card["kind"] == "claim" and card["label"] == "Page Six" and card["panel"] is True
    assert card["align"] == "left" and card["inside_scene"] is True


def test_claim_card_past_the_scene_end_is_rejected_before_rendering(project):
    scene = _claim_scene()
    scene["text_cards"][0]["end"] = 30
    _write(project, [scene])
    with pytest.raises(sp.PipelineError, match="text_cards"):
        _build(project)
    assert not (project / "render").exists()


def test_settings_accent_color_is_the_project_default(project):
    manifest = {"schema_version": 1, "project_id": "ep", "scenes": [_claim_scene()],
                "settings": {**SETTINGS, "text_card_style": {"accent_color": "#00AAFF"}}}
    (project / "scenes.json").write_text(json.dumps(manifest), encoding="utf-8")
    loaded = sp.load_manifest(project)
    assert loaded.text_card_style == {"accent_color": "#00AAFF"}
    label = next(i for i in _card_items(loaded) if isinstance(i, scene_post.scene_text.DrawLine))
    assert label.color == "#00AAFF"


# -- D2: fact card, quote card and accent bar in real frames -----------------------

def _card_scene(card):
    card = {"start": 0.2, "end": 1.0, **card}
    card["style"] = {"position": "center", **card.get("style", {})}
    return _scene("s1", audio="three.wav", motion="static", text_cards=[card])


def _boxes_of(project):
    return [i for i in _card_items(sp.load_manifest(project)) if isinstance(i, scene_post.scene_text.DrawBox)]


def _share(image, box, near, tolerance=40):
    crop = image.convert("RGB").crop(box)
    pixels = list(crop.getdata())
    hits = [p for p in pixels if all(abs(p[i] - near[i]) <= tolerance for i in range(3))]
    return len(hits) / len(pixels)


FACT = {"kind": "fact", "label": "Last listed", "value": "Oct 26", "text": "Allenwood Low"}
QUOTE = {"kind": "quote", "text": "No additional information.", "source": "Bureau of Prisons"}


@pytest.mark.parametrize("card,side", [(FACT, "top"), (QUOTE, "left")])
def test_accent_bar_is_a_solid_strip_of_the_accent_colour(project, card, side):
    _write(project, [_card_scene({**card, "style": {"accent_color": "#FF2020"}})])
    panel, bar = _boxes_of(project)
    out = _build(project)["output"]
    inside = Image.open(_frame(out, 0.6, project / "in.png"))
    strip = (bar.x + 1, bar.y + 1, bar.x + bar.w - 1, bar.y + bar.h - 1)
    assert _share(inside, strip, (255, 32, 32)) > 0.9
    outside = Image.open(_frame(out, 1.2, project / "out.png"))
    assert _share(outside, strip, (255, 32, 32)) < 0.1
    assert bar.h == 10 if side == "top" else bar.w == 10


@pytest.mark.parametrize("card", [FACT, QUOTE])
def test_fact_and_quote_cards_darken_their_panel_and_keep_the_margins(project, card):
    _write(project, [_card_scene(card)])
    panel = _boxes_of(project)[0]
    out = _build(project)["output"]
    inside = Image.open(_frame(out, 0.6, project / "in.png"))
    outside = Image.open(_frame(out, 1.2, project / "out.png"))
    box = (panel.x, panel.y, panel.x + panel.w, panel.y + panel.h)
    assert _luma(inside, box) < 0.6 * _luma(outside, box)
    margin = round(scene_post.scene_text.MARGIN_X * 270)
    left, right = inside.convert("L"), outside.convert("L")
    for strip in ((0, 0, margin - 1, 480), (270 - margin + 1, 0, 270, 480)):
        diff = sum(abs(a - b) for a, b in zip(left.crop(strip).getdata(), right.crop(strip).getdata()))
        assert diff / ((strip[2] - strip[0]) * 480) < 3


def test_fact_value_is_drawn_in_white_and_the_label_in_the_accent(project):
    _write(project, [_card_scene({**FACT, "style": {"accent_color": "#FF2020"}})])
    panel = _boxes_of(project)[0]
    inside = Image.open(_frame(_build(project)["output"], 0.6, project / "in.png"))
    box = (panel.x, panel.y, panel.x + panel.w, panel.y + panel.h)
    assert _share(inside, box, (255, 32, 32), 50) > 0.01      # label (and bar)
    assert _share(inside, box, (255, 255, 255), 30) > 0.02    # value and caption


def test_plan_describes_fact_and_quote_cards(project, capsys):
    _write(project, [_scene("s1", audio="three.wav", motion="static", text_cards=[
        {**FACT, "start": 0.1, "end": 0.6}, {**QUOTE, "start": 0.7, "end": 1.2}])])
    assert sp.main(["--project", str(project), "plan", "--text"]) == 0
    text = capsys.readouterr().out
    assert "Last listed Oct 26 Allenwood Low" in text and "No additional information. -- Bureau of Prisons" in text
    assert sp.main(["--project", str(project), "plan"]) == 0
    fact, quote = json.loads(capsys.readouterr().out)[0]["cards"]
    assert fact["kind"] == "fact" and fact["value"] == "Oct 26" and fact["align"] == "center"
    assert quote["kind"] == "quote" and quote["source"] == "Bureau of Prisons" and quote["align"] == "left"


def test_cards_past_the_scene_end_are_still_rejected_for_every_kind(project):
    for card in (FACT, QUOTE):
        _write(project, [_card_scene({**card, "end": 30})])
        with pytest.raises(sp.PipelineError, match="text_cards"):
            _build(project)
    assert not (project / "render").exists()


def test_a_card_with_a_bar_rebuilds_only_its_scene(project):
    scenes = [_card_scene({**QUOTE}), _scene("s2", "two.wav", "b.png")]
    _write(project, scenes)
    _build(project)
    scenes[0]["text_cards"][0]["style"]["accent_width"] = 20
    _write(project, scenes)
    rebuilt = _rebuilt(_build(project))
    assert "s1:picture" in rebuilt and "s2:picture" not in rebuilt


# -- E1: all xfade transitions --------------------------------------------------

def _ffmpeg_xfade_names():
    done = subprocess.run(["ffmpeg", "-hide_banner", "-h", "filter=xfade"], capture_output=True, text=True)
    return set(re.findall(r"^\s+(\w+)\s+\d+\s", done.stdout, re.M))


def test_every_transition_maps_to_a_real_xfade_name():
    if not shutil.which("ffmpeg"):
        pytest.skip("native FFmpeg required")
    real = _ffmpeg_xfade_names()
    assert len(real) >= 46
    mapped = {k: v for k, v in scene_post.TRANSITION_TYPES.items() if v is not None}
    assert [k for k, v in mapped.items() if v not in real] == []
    assert set(scene_post.TRANSITION_TYPES) == set(sp.TRANSITION_TYPES)
    assert len(mapped) == 46 and len(set(mapped.values())) == 46
    assert real - set(mapped.values()) - {"custom"} == set()  # every ffmpeg transition is available


def test_the_nine_original_transitions_keep_their_xfade_names():
    original = {"crossfade": "fade", "fade_black": "fadeblack", "fade_white": "fadewhite",
                "dissolve": "dissolve", "wipe_left": "wipeleft", "wipe_right": "wiperight",
                "slide_left": "slideleft", "slide_right": "slideright"}
    assert {k: scene_post.TRANSITION_TYPES[k] for k in original} == original
    assert scene_post.TRANSITION_TYPES["cut"] is None


def test_an_unknown_transition_type_lists_the_valid_names(project):
    _write(project, [_scene("s1", transition={"type": "spin", "duration": 0.3}), _scene("s2", "two.wav", asset="b.png")])
    message = _errors(project)
    assert "transition.type: must be one of cut, crossfade" in message and "fade_slow" in message


# -- E2: music fades and per-scene volume --------------------------------------------

def _window_level(path, start, length):
    done = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-ss", str(start), "-t", str(length), "-i", str(path),
         "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True)
    return float(re.search(r"mean_volume: (-?[\d.]+) dB", done.stderr).group(1))


@pytest.mark.parametrize("raw,message", [
    ({"fade_in": -1}, "bgm.fade_in: must be a number of seconds from 0 to 10"),
    ({"fade_out": 11}, "bgm.fade_out: must be a number of seconds from 0 to 10"),
    ({"fade_in": "slow"}, "bgm.fade_in: must be a number of seconds from 0 to 10"),
])
def test_music_fades_are_validated(project, raw, message):
    _write(project, [_scene("s1")], bgm={"file": "music_long.wav", **raw})
    assert message in _errors(project)


@pytest.mark.parametrize("value", [-31, 13, "loud", True])
def test_scene_volume_db_is_validated(project, value):
    _write(project, [_scene("s1", volume_db=value)])
    assert "volume_db: must be a number of dB from -30 to 12" in _errors(project)


def test_fades_and_gain_are_off_by_default(project):
    _write(project, [_scene("s1")], bgm={"file": "music_long.wav", "volume": 0.3, "ducking": {}})
    manifest = sp.load_manifest(project)
    assert manifest.bgm.fade_in == 0 and manifest.bgm.fade_out == 0
    assert manifest.scenes[0].volume_db == 0
    graph = scene_post.bgm_graph(0.3, manifest.bgm.ducking)
    assert "afade" not in graph and scene_post.music_fades(0, 0, 5.0) == ""
    assert scene_post._gain(0) == ""
    assert sp.uses_post(sp.load_manifest(project))  # bgm alone already uses the post stages


def test_music_fade_filters():
    assert scene_post.music_fades(1.0, 1.5, 10.0) == ",afade=t=in:st=0:d=1.000,afade=t=out:st=8.500:d=1.500"
    assert scene_post.music_fades(0, 2.0, 1.0) == ",afade=t=out:st=0.000:d=2.000"
    graph = scene_post.bgm_graph(0.3, None, 1.0, 0.5, 4.0)
    assert "volume=0.3000,afade=t=in" in graph and graph.index("afade") < graph.index("amix")


def test_music_fades_shape_the_start_and_the_end(project):
    scenes = [_scene("s1"), _scene("s2", "two.wav", "b.png")]
    _write(project, scenes, bgm={"file": "music_long.wav", "volume": 0.5})
    plain = _build(project)["output"]
    plain_start, plain_end = _window_level(plain, 0, 0.25), _window_level(plain, 2.0, 0.25)
    _write(project, scenes, bgm={"file": "music_long.wav", "volume": 0.5, "fade_in": 1.0, "fade_out": 1.0})
    faded = _build(project)["output"]
    assert _window_level(faded, 0, 0.25) < plain_start - 6
    assert _window_level(faded, 2.0, 0.25) < plain_end - 6
    mid = _window_level(faded, 1.0, 0.2)
    assert mid > _window_level(faded, 0, 0.25) + 3


def test_fades_longer_than_the_video_are_rejected_before_rendering(project):
    _write(project, [_scene("s1")], bgm={"file": "music_long.wav", "fade_in": 1.0, "fade_out": 1.0})
    with pytest.raises(sp.PipelineError, match="longer than the video"):
        _build(project)
    assert not (project / "render").exists()


def test_changing_a_fade_rebuilds_only_the_final_mix(project):
    scenes = [_scene("s1"), _scene("s2", "two.wav", "b.png")]
    _write(project, scenes, bgm={"file": "music_long.wav", "volume": 0.3, "fade_out": 0.5})
    _build(project)
    _write(project, scenes, bgm={"file": "music_long.wav", "volume": 0.3, "fade_out": 0.8})
    assert _rebuilt(_build(project)) == {"final"}


def test_scene_volume_changes_that_scenes_narration_level(project):
    scenes = [_scene("s1"), _scene("s2", "two.wav", "b.png")]
    _write(project, scenes)
    base = _build(project)["output"]
    quiet = _window_level(base, 0.2, 0.5)
    other = _window_level(base, 1.6, 0.3)
    scenes[0]["volume_db"] = -12
    _write(project, scenes)
    result = _build(project)
    assert _window_level(result["output"], 0.2, 0.5) == pytest.approx(quiet - 12, abs=1.0)
    assert _window_level(result["output"], 1.6, 0.3) == pytest.approx(other, abs=1.0)


def test_scene_volume_rebuilds_only_that_scene_and_its_junction(project):
    scenes = [_scene("s1", transition={"type": "crossfade", "duration": 0.3}),
              _scene("s2", "two.wav", "b.png"), _scene("s3", "three.wav", "a.png")]
    _write(project, scenes)
    _build(project)
    scenes[2]["volume_db"] = 6
    _write(project, scenes)
    rebuilt = _rebuilt(_build(project))
    assert "s3:narration" in rebuilt and "s1:narration" not in rebuilt and "s1>s2:narration" not in rebuilt
    assert not any(label.endswith("picture") for label in rebuilt)


def test_scene_volume_alone_uses_the_post_stages_and_zero_does_not(project):
    _write(project, [_scene("s1", volume_db=3)])
    assert sp.uses_post(sp.load_manifest(project))
    _write(project, [_scene("s1", volume_db=0)])
    assert not sp.uses_post(sp.load_manifest(project))


def test_plan_shows_fades_and_gain(project, capsys):
    _write(project, [_scene("s1", volume_db=-3)], bgm={"file": "music_long.wav", "fade_in": 0.3, "fade_out": 0.4})
    assert sp.main(["--project", str(project), "plan", "--text"]) == 0
    text = capsys.readouterr().out
    assert "music fades: in 0.3 s, out 0.4 s" in text and "narration gain: -3 dB" in text
    _write(project, [_scene("s1")], bgm={"file": "music_long.wav", "fade_in": 1.0, "fade_out": 1.0})
    assert sp.main(["--project", str(project), "plan", "--text"]) == 1
    assert "longer than the video" in capsys.readouterr().out


# -- E3: look (grade, vignette) and the flash on a transition -------------------

from app.services import scene_look  # noqa: E402


@pytest.mark.parametrize("raw,message", [
    ("warm", "look: must be an object"),
    ({"glow": {}}, "look: unknown field: glow"),
    ({"vignette": 0.3}, "vignette: must be an object"),
    ({"vignette": {"amount": 2}}, "vignette.amount: must be a number from 0 to 1"),
    ({"vignette": {"strength": 0.2}}, "vignette: unknown field: strength"),
    ({"grade": {}}, "grade: give a preset or at least one"),
    ({"grade": {"preset": "neon"}}, "grade.preset: must be one of warm, cool, faded, high_contrast"),
    ({"grade": {"contrast": 3}}, "grade.contrast: must be a number from 0.5 to 2"),
    ({"grade": {"saturation": -1}}, "grade.saturation: must be a number from 0 to 3"),
    ({"grade": {"brightness": 0.9}}, "grade.brightness: must be a number from -0.5 to 0.5"),
    ({"grade": {"hue": 1}}, "grade: unknown field: hue"),
])
def test_look_is_validated(raw, message):
    errors = []
    scene_look.parse_look(raw, "look", errors)
    assert any(message in e for e in errors), errors


def test_look_is_empty_by_default_and_adds_nothing():
    assert scene_look.parse_look(None, "look", []) == {}
    assert scene_look.look_filters({}) == "" and scene_look.look_key({}) is None
    assert scene_look.look_filters({"vignette": {"amount": 0.0}}) == ""
    assert scene_look.look_filters({"grade": {"contrast": 1.0, "saturation": 1.0, "brightness": 0.0}}) == ""


def test_look_filters_for_each_part():
    assert scene_look.look_filters({"grade": {"contrast": 1.2}}) == "eq=contrast=1.2:saturation=1:brightness=0"
    warm = scene_look.look_filters({"grade": {"preset": "warm"}})
    assert warm.startswith("eq=contrast=1.04:saturation=1.08") and ",colorbalance=rs=0.06" in warm
    override = scene_look.look_filters({"grade": {"preset": "faded", "saturation": 1.0}})
    assert "saturation=1:" in override and "contrast=0.88" in override
    weak = scene_look.look_filters({"vignette": {"amount": 0.2}})
    strong = scene_look.look_filters({"vignette": {"amount": 0.9}})
    angle = lambda text: float(text.split("angle=")[1])  # noqa: E731
    assert 0 < angle(weak) < angle(strong) < 1.5708
    both = scene_look.look_filters({"grade": {"preset": "cool"}, "vignette": {"amount": 0.5}})
    assert both.index("eq=") < both.index("colorbalance") < both.index("vignette")


def test_a_scene_look_overrides_the_project_look_field_by_field():
    default = {"vignette": {"amount": 0.4}, "grade": {"preset": "warm", "contrast": 1.1}}
    scene = {"grade": {"contrast": 1.3}}
    merged = scene_look.merge_look(default, scene)
    assert merged == {"vignette": {"amount": 0.4}, "grade": {"preset": "warm", "contrast": 1.3}}
    assert scene_look.merge_look(None, None) == {}


@pytest.mark.parametrize("flash,message", [
    ("white", "flash: must be an object"),
    ({"duration": 0.01}, "flash.duration: must be a number from 0.03 to 0.3"),
    ({"duration": 0.6}, "flash.duration: must be a number from 0.03 to 0.3"),
    ({"duration": 0.3}, "flash.duration: must be shorter than the transition"),
    ({"duration": 0.1, "strength": 0}, "flash.strength: must be a number above 0 and at most 1"),
    ({"duration": 0.1, "color": "white"}, "flash.color: must be a color like #RRGGBB"),
    ({"duration": 0.1, "speed": 1}, "flash: unknown field: speed"),
])
def test_flash_is_validated(project, flash, message):
    _write(project, [_scene("s1", transition={"type": "crossfade", "duration": 0.3, "flash": flash}),
                     _scene("s2", "two.wav", "b.png")])
    assert message in _errors(project)


def test_a_cut_has_nothing_to_flash(project):
    _write(project, [_scene("s1", transition={"type": "cut", "flash": {"duration": 0.1}}),
                     _scene("s2", "two.wav", "b.png")])
    assert "a cut has no overlap to flash over" in _errors(project)


def test_look_in_settings_and_scene_parse_and_switch_on_the_post_stages(project):
    manifest = {"schema_version": 1, "project_id": "ep", "scenes": [_scene("s1")],
                "settings": {**SETTINGS, "look": {"vignette": {"amount": 0.3}}}}
    (project / "scenes.json").write_text(json.dumps(manifest), encoding="utf-8")
    loaded = sp.load_manifest(project)
    assert loaded.look == {"vignette": {"amount": 0.3}} and sp.uses_post(loaded)
    _write(project, [_scene("s1", look={"grade": {"preset": "warm"}})])
    assert sp.uses_post(sp.load_manifest(project))
    _write(project, [_scene("s1", look={})])
    assert not sp.uses_post(sp.load_manifest(project))


def _flat(project, name="flat.png", color="0x808080"):
    _ff(["-f", "lavfi", "-i", f"color=c={color}:s=270x480", "-frames:v", "1", str(project / "assets" / name)])
    return name


def _mean_rgb(image, box):
    crop = image.convert("RGB").crop(box)
    pixels = list(crop.getdata())
    return tuple(sum(p[i] for p in pixels) / len(pixels) for i in range(3))


def test_vignette_darkens_the_corners_only_when_asked(project):
    flat = _flat(project)
    _write(project, [_scene("s1", asset=flat, motion="static")])
    plain = Image.open(_frame(_build(project)["output"], 0.5, project / "plain.png"))
    _write(project, [_scene("s1", asset=flat, motion="static", look={"vignette": {"amount": 1.0}})])
    shaded = Image.open(_frame(_build(project)["output"], 0.5, project / "shaded.png"))
    corner, centre = (0, 0, 30, 30), (120, 210, 150, 270)
    assert abs(_luma(plain, corner) - _luma(plain, centre)) < 3
    assert _luma(shaded, corner) < _luma(shaded, centre) - 40
    assert abs(_luma(shaded, centre) - _luma(plain, centre)) < 12


def test_warm_grade_shifts_colour_toward_red_and_cool_toward_blue(project):
    flat = _flat(project)
    centre = (100, 200, 170, 280)
    results = {}
    for name, look in (("none", {}), ("warm", {"grade": {"preset": "warm"}}), ("cool", {"grade": {"preset": "cool"}})):
        _write(project, [_scene("s1", asset=flat, motion="static", look=look)])
        results[name] = _mean_rgb(Image.open(_frame(_build(project)["output"], 0.5, project / f"{name}.png")), centre)
    base_r, _, base_b = results["none"]
    assert results["warm"][0] - results["warm"][2] > (base_r - base_b) + 8
    assert results["cool"][2] - results["cool"][0] > (base_b - base_r) + 8


def test_contrast_and_brightness_change_the_picture(project):
    flat = _flat(project, color="0x606060")
    centre = (100, 200, 170, 280)
    _write(project, [_scene("s1", asset=flat, motion="static")])
    base = _luma(Image.open(_frame(_build(project)["output"], 0.5, project / "a.png")), centre)
    _write(project, [_scene("s1", asset=flat, motion="static", look={"grade": {"brightness": 0.2}})])
    bright = _luma(Image.open(_frame(_build(project)["output"], 0.5, project / "b.png")), centre)
    assert bright > base + 25


def test_a_look_leaves_the_timeline_length_alone(project):
    _write(project, [_scene("s1"), _scene("s2", "two.wav", "b.png")])
    plain = _build(project)
    _write(project, [_scene("s1", look={"grade": {"preset": "faded"}, "vignette": {"amount": 0.6}}),
                     _scene("s2", "two.wav", "b.png")])
    styled = _build(project)
    assert styled["frames"] == 14 + 9
    assert abs(_duration(styled["output"]) - _duration(plain["output"])) < 0.05


def test_a_look_change_rebuilds_only_that_scene_and_its_junction(project):
    scenes = [_scene("s1", transition={"type": "crossfade", "duration": 0.3}),
              _scene("s2", "two.wav", "b.png"), _scene("s3", "three.wav", "a.png")]
    _write(project, scenes)
    _build(project)
    _write(project, [dict(scenes[0], look={"vignette": {"amount": 0.5}}), scenes[1], scenes[2]])
    rebuilt = _rebuilt(_build(project))
    assert "s1:picture" in rebuilt and "s1>s2:picture" in rebuilt
    assert "s3:picture" not in rebuilt and "s2:picture" not in rebuilt and "s1:narration" not in rebuilt


def test_an_empty_look_keeps_the_cache_keys_of_a_manifest_without_one(project):
    scenes = [_scene("s1", transition={"type": "crossfade", "duration": 0.3}), _scene("s2", "two.wav", "b.png")]
    _write(project, scenes)
    _build(project)
    scenes[0]["look"] = {}
    scenes[0]["transition"]["flash"] = None
    _write(project, scenes)
    assert _rebuilt(_build(project)) == set()


def test_flash_brightens_the_middle_of_the_transition_only(project):
    scenes = [_scene("s1", asset="b.png", motion="static", transition={"type": "crossfade", "duration": 0.5}),
              _scene("s2", "two.wav", "a.png", motion="static")]
    _write(project, scenes)
    plain = _build(project)["output"]
    plain_mid = _luma(Image.open(_frame(plain, 1.1, project / "p_mid.png")), (0, 0, 270, 480))
    plain_early = _frame(plain, 0.4, project / "p_early.png")
    scenes[0]["transition"]["flash"] = {"duration": 0.2, "strength": 1.0, "color": "#FFFFFF"}
    _write(project, scenes)
    flashed = _build(project)
    out = flashed["output"]
    mid = _luma(Image.open(_frame(out, 1.1, project / "f_mid.png")), (0, 0, 270, 480))
    assert mid > plain_mid + 25
    assert _psnr(plain_early, _frame(out, 0.4, project / "f_early.png")) > 35   # before the transition
    assert flashed["frames"] == 14 + 9 - 5                                   # length unchanged


def test_flash_colour_is_used(project):
    scenes = [_scene("s1", asset="b.png", motion="static",
                     transition={"type": "crossfade", "duration": 0.5,
                                 "flash": {"duration": 0.2, "strength": 1.0, "color": "#FF0000"}}),
              _scene("s2", "two.wav", "a.png", motion="static")]
    _write(project, scenes)
    r, g, b = _mean_rgb(Image.open(_frame(_build(project)["output"], 1.1, project / "red.png")), (0, 0, 270, 480))
    assert r > g + 40 and r > b + 40


def test_a_flash_change_rebuilds_only_its_junction_and_the_joins(project):
    scenes = [_scene("s1", transition={"type": "crossfade", "duration": 0.4,
                                       "flash": {"duration": 0.1, "strength": 0.5}}),
              _scene("s2", "two.wav", "b.png")]
    _write(project, scenes)
    _build(project)
    scenes[0]["transition"]["flash"]["strength"] = 0.9
    _write(project, scenes)
    rebuilt = _rebuilt(_build(project))
    assert "s1>s2:picture" in rebuilt and "s1:picture" not in rebuilt and "s2:picture" not in rebuilt
    assert not any(label.endswith("narration") for label in rebuilt - {"join:narration"})


def test_plan_shows_look_and_flash(project, capsys):
    _write(project, [_scene("s1", look={"grade": {"preset": "warm"}, "vignette": {"amount": 0.4}},
                            transition={"type": "crossfade", "duration": 0.4, "flash": {"duration": 0.1}}),
                     _scene("s2", "two.wav", "b.png")])
    assert sp.main(["--project", str(project), "plan", "--text"]) == 0
    text = capsys.readouterr().out
    assert "look: eq=contrast=1.04" in text and "flash: 0.1 s, strength 0.8, #FFFFFF" in text


# -- E4: card animation in real frames --------------------------------------------------

def _anim_scene(card, animation, **extra):
    card = {"start": 0.2, "end": 1.2, **card, "animation": animation}
    card["style"] = {"position": "center", **card.get("style", {})}
    return _scene("s1", audio="three.wav", asset=_flat_name, motion="static", text_cards=[card], **extra)


_flat_name = "flat.png"


def _text_rows(project):
    """Bounding box (x0, y0, x1, y1) of the drawn text lines of the first card."""
    manifest = sp.load_manifest(project)
    card = manifest.scenes[0].text_cards[0]
    style = _claim_style(manifest, card)
    animation = scene_post.scene_text.resolve_animation(card.animation, manifest.text_card_animation)
    items = scene_post.scene_text.card_items(card, style, 270, 480, animation)
    lines = [i for i in items if isinstance(i, scene_post.scene_text.DrawLine)]
    return [(i.y, i.size, i.text) for i in lines]


def _bright(image, box, level=150):
    crop = image.convert("L").crop(box)
    return sum(1 for p in crop.getdata() if p > level)


def _frames(project, out, times):
    return {t: Image.open(_frame(out, t, project / f"fr_{t}.png")) for t in times}


def test_fade_in_shows_the_text_gradually_over_a_static_panel(project):
    _flat(project, "flat.png", "0x303030")
    claim = {"kind": "claim", "label": "Page Six", "text": "Hit his head"}
    _write(project, [_anim_scene(claim, {"in": "fade", "duration": 0.6})])
    rows = _text_rows(project)
    panel = _card_rect(project)
    out = _build(project)["output"]
    frames = _frames(project, out, [0.2, 0.4, 0.9])
    box = (panel.x, panel.y, panel.x + panel.w, panel.y + panel.h)
    # the panel is already there when the card starts (it cuts in) but the text is not
    start, mid, end = (_bright(frames[t], box, 60) for t in (0.2, 0.4, 0.9))
    assert _luma(frames[0.2], box) < 0.6 * 0x30     # dark panel at the first frame
    assert start <= 5 and start < mid < end


def test_fade_out_removes_the_text_before_the_card_ends(project):
    _flat(project, "flat.png", "0x303030")
    claim = {"kind": "claim", "label": "Page Six", "text": "Hit his head"}
    _write(project, [_anim_scene(claim, {"out": "fade", "duration": 0.6})])
    panel = _card_rect(project)
    out = _build(project)["output"]
    frames = _frames(project, out, [0.4, 1.1])
    box = (panel.x, panel.y, panel.x + panel.w, panel.y + panel.h)
    assert _bright(frames[1.1], box) < _bright(frames[0.4], box) * 0.5


def test_slide_up_moves_the_text_up_into_place(project):
    _flat(project, "flat.png", "0x303030")
    claim = {"kind": "claim", "label": "Page Six", "text": "Hit his head", "style": {"align": "center"}}
    _write(project, [_anim_scene(claim, {"in": "slide_up", "duration": 0.8})])
    panel = _card_rect(project)
    out = _build(project)["output"]
    frames = _frames(project, out, [0.4, 1.1])
    box = (panel.x, panel.y, panel.x + panel.w, panel.y + panel.h)

    def centroid(image):
        crop = image.convert("L").crop(box)
        w = crop.size[0]
        ys = [i // w for i, p in enumerate(crop.getdata()) if p > 45]
        return sum(ys) / len(ys) if ys else None

    early, final = centroid(frames[0.4]), centroid(frames[1.1])
    assert early is not None and final is not None and early > final + 1.5


def test_stagger_lets_the_label_land_before_the_claim(project):
    _flat(project, "flat.png", "0x303030")
    claim = {"kind": "claim", "label": "Page Six", "text": "Hit his head"}
    _write(project, [_anim_scene(claim, {"in": "fade", "duration": 0.2, "stagger": 0.5})])
    rows = _text_rows(project)
    out = _build(project)["output"]
    frame = _frames(project, out, [0.5])[0.5]
    label_y, label_size, _ = rows[0]
    claim_y, claim_size, _ = rows[1]
    panel = _card_rect(project)
    label_box = (panel.x, label_y, panel.x + panel.w, label_y + label_size)
    claim_box = (panel.x, claim_y, panel.x + panel.w, claim_y + claim_size)
    assert _bright(frame, label_box, 100) > 5          # the label is already there (accent colour is bright)
    assert _bright(frame, claim_box, 150) <= 2         # the claim has not started
    late = _frames(project, out, [1.0])[1.0]
    assert _bright(late, claim_box, 150) > 5


def test_probe_box_behaviour_of_a_plain_card_with_a_fade(project):
    _flat(project, "flat.png", "0x909090")
    _write(project, [_anim_scene({"text": "Plain card"}, {"in": "fade", "duration": 0.6})])
    rows = _text_rows(project)
    out = _build(project)["output"]
    frames = _frames(project, out, [0.2, 1.0])
    y, size, _ = rows[0]
    box = (40, y, 230, y + size)
    print("BOXPROBE start", _luma(frames[0.2], box), "end", _luma(frames[1.0], box), "bg", 0x90)


def test_an_animation_that_does_not_fit_is_rejected_before_rendering(project):
    _flat(project, "flat.png")
    _write(project, [_anim_scene({"text": "Short"}, {"in": "fade", "out": "fade", "duration": 0.7})])
    with pytest.raises(sp.PipelineError, match="animation needs"):
        _build(project)
    assert not (project / "render").exists()


def test_animation_default_in_settings_applies_and_a_card_can_turn_it_off(project):
    _flat(project, "flat.png")
    card = {"text": "Plain card", "start": 0.2, "end": 1.2}
    scenes = [_scene("s1", audio="three.wav", asset="flat.png", motion="static", text_cards=[card])]
    manifest = {"schema_version": 1, "project_id": "ep", "scenes": scenes,
                "settings": {**SETTINGS, "text_card_animation": {"in": "fade", "duration": 0.3}}}
    (project / "scenes.json").write_text(json.dumps(manifest), encoding="utf-8")
    loaded = sp.load_manifest(project)
    assert loaded.text_card_animation == {"in": "fade", "duration": 0.3}
    items = scene_post._overlays(loaded, loaded.scenes[0], 1.3)
    assert all(i.anim == ("fade", "none", 0.3, 0.0) for i in items)
    scenes[0]["text_cards"][0]["animation"] = {"in": "none"}
    (project / "scenes.json").write_text(json.dumps(manifest), encoding="utf-8")
    loaded = sp.load_manifest(project)
    assert all(i.anim is None for i in scene_post._overlays(loaded, loaded.scenes[0], 1.3))


def test_changing_an_animation_rebuilds_only_that_scene(project):
    _flat(project, "flat.png")
    scenes = [_anim_scene({"text": "Plain card"}, {"in": "fade", "duration": 0.3}), _scene("s2", "two.wav", "b.png")]
    _write(project, scenes)
    _build(project)
    scenes[0]["text_cards"][0]["animation"] = {"in": "slide_up", "duration": 0.3}
    _write(project, scenes)
    rebuilt = _rebuilt(_build(project))
    assert "s1:picture" in rebuilt and "s2:picture" not in rebuilt


def test_plan_shows_the_animation(project, capsys):
    _flat(project, "flat.png")
    _write(project, [_anim_scene({"kind": "claim", "label": "L", "text": "Text"}, {"in": "slide_up", "stagger": 0.2})])
    assert sp.main(["--project", str(project), "plan", "--text"]) == 0
    assert "animation in=slide_up out=none 0.3s stagger 0.2s" in capsys.readouterr().out
