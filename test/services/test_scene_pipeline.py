"""Scene pipeline tests. Real FFmpeg for media; narration synthesis is faked."""

import json
import os
import re
import resource
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.services import scene_pipeline as sp
from app.services.video_project import ProjectError, VideoProject


def _ff(arguments):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-threads", "1", *arguments],
        check=True,
    )


def _duration(path):
    return float(
        subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            text=True,
        )
    )


def _png(path, pattern="testsrc", size="96x72"):
    _ff(["-f", "lavfi", "-i", f"{pattern}=s={size}:d=1", "-frames:v", "1", str(path)])


def _tone(path, seconds, frequency=440):
    _ff(["-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000:duration={seconds}",
         "-f", "wav", str(path)])


def _video(path, seconds, color="red"):
    _ff(["-f", "lavfi", "-i", f"color=c={color}:s=96x72:r=10:d={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)])


class FakeTTS:
    """Stands in for Edge TTS; duration depends on the text so edits are visible."""

    def __init__(self):
        self.calls = []

    def __call__(self, text, voice_name, rate, output):
        self.calls.append(text)
        _tone(output, round(0.5 + len(text) * 0.01, 2))
        return True


def _forbidden(*_args):
    raise AssertionError("narration synthesis must not run")


SETTINGS = {"width": 96, "height": 160, "fps": 10}


@pytest.fixture
def media(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("native FFmpeg/FFprobe required")
    project = tmp_path / "project"
    (project / "assets").mkdir(parents=True)
    (project / "audio").mkdir()
    _png(project / "assets" / "a.png")
    _png(project / "assets" / "b.png", pattern="smptebars")
    _video(project / "assets" / "short.mp4", 0.5)
    _video(project / "assets" / "long.mp4", 3, "blue")
    _tone(project / "audio" / "one.wav", 1.3)
    _tone(project / "audio" / "two.wav", 0.9, 330)
    return project


def _write(project, scenes, **extra):
    manifest = {"schema_version": 1, "project_id": "ep", "settings": dict(SETTINGS),
                "voice": {"name": "en-US-AriaNeural", "rate": 1.0}, "scenes": scenes}
    manifest.update(extra)
    (project / "scenes.json").write_text(json.dumps(manifest), encoding="utf-8")


def _scene(scene_id, asset="a.png", narration="Hello there", **extra):
    return {"id": scene_id, "asset": asset, "narration": narration, **extra}


def _errors(project):
    with pytest.raises(sp.ManifestError) as caught:
        sp.load_manifest(project)
    return str(caught.value)


# -- schema ---------------------------------------------------------------

def test_valid_manifest_applies_defaults(media):
    _write(media, [_scene("s1")], settings={})
    manifest = sp.load_manifest(media)
    assert (manifest.settings.width, manifest.settings.height, manifest.settings.fps) == (1080, 1920, 30)
    assert manifest.scenes[0].motion == "zoom_in"
    assert manifest.voice_rate == 1.0


@pytest.mark.parametrize("field", ["full_narration_audio"])
def test_reserved_top_level_fields_rejected(media, field):
    _write(media, [_scene("s1")], **{field: "x.mp3"})
    assert "reserved for a later phase" in _errors(media)


@pytest.mark.parametrize("field", list(sp.RESERVED_SCENE_FIELDS))
def test_reserved_scene_fields_rejected(media, field):
    _write(media, [_scene("s1", **{field: {"x": 1}})])
    assert field in _errors(media)


def test_null_reserved_fields_are_allowed(media):
    _write(media, [_scene("s1", captions=None, transition=None)], bgm=None)
    assert sp.load_manifest(media).scenes[0].id == "s1"


@pytest.mark.parametrize("motion", list(sp.RESERVED_MOTIONS))
def test_reserved_motions_rejected(media, motion):
    _write(media, [_scene("s1", motion=motion)])
    assert "reserved" in _errors(media)


def test_unknown_fields_rejected(media):
    _write(media, [_scene("s1", surprise=1)], extra_top=1)
    message = _errors(media)
    assert "surprise" in message and "extra_top" in message


@pytest.mark.parametrize("name", ["../a.png", "sub/a.png", "/etc/passwd.png", "a\\b.png", ".hidden.png", ""])
def test_filenames_must_be_plain(media, name):
    _write(media, [_scene("s1", asset=name)])
    assert "s1" in _errors(media)


def test_symlinked_asset_rejected(media):
    outside = media.parent / "outside.png"
    _png(outside)
    (media / "assets" / "link.png").symlink_to(outside)
    _write(media, [_scene("s1", asset="link.png")])
    assert "symbolic" in _errors(media)


def test_all_problems_reported_together(media):
    _write(media, [_scene("s1", asset="missing.png"), _scene("s2", asset="a.gif"),
                   _scene("s2", narration=""), _scene("bad id")])
    message = _errors(media)
    for fragment in ("missing.png", "unsupported extension", "duplicate scene id", "needs narration", "bad id"):
        assert fragment in message or "1-64" in message


def test_scene_needs_narration_or_audio(media):
    _write(media, [_scene("s1", narration="  ")])
    assert "needs narration" in _errors(media)


def test_voice_required_only_when_tts_needed(media):
    _write(media, [_scene("s1", audio="one.wav")], voice=None)
    assert sp.load_manifest(media).voice_name is None
    _write(media, [_scene("s1")], voice=None)
    assert "voice.name" in _errors(media)


@pytest.mark.parametrize("count", [0, sp.MAX_SCENES + 1])
def test_scene_count_limits(media, count):
    _write(media, [_scene(f"s{i}") for i in range(count)])
    assert "scenes" in _errors(media)


def test_media_probe_reports_unreadable_asset(media):
    (media / "assets" / "broken.png").write_bytes(b"not an image")
    _write(media, [_scene("s1", asset="broken.png")])
    with pytest.raises(sp.ManifestError, match="s1"):
        sp.inspect_media(sp.load_manifest(media), media)


def test_oversized_image_rejected(media, monkeypatch):
    monkeypatch.setattr(sp, "MAX_IMAGE_PIXELS", 100)
    _write(media, [_scene("s1")])
    with pytest.raises(sp.ManifestError, match="larger than"):
        sp.inspect_media(sp.load_manifest(media), media)


# -- narration ------------------------------------------------------------

def test_premade_audio_skips_tts(media):
    _write(media, [_scene("s1", audio="one.wav")])
    result = sp.build(media, synthesize=_forbidden, progress=lambda _m: None)
    assert result["narration_generated"] == 0


def test_tts_runs_once_then_is_cached(media):
    fake = FakeTTS()
    _write(media, [_scene("s1"), _scene("s2", asset="b.png", narration="Another line")])
    sp.build(media, synthesize=fake, progress=lambda _m: None)
    assert len(fake.calls) == 2
    sp.build(media, synthesize=_forbidden, progress=lambda _m: None)


def test_tts_cache_key_depends_on_text_voice_and_rate(media):
    base = sp.tts_cache_path(media, "v", 1.0, "text")
    assert base == sp.tts_cache_path(media, "v", 1.0, "text")
    assert len({base, sp.tts_cache_path(media, "v", 1.0, "other"),
                sp.tts_cache_path(media, "w", 1.0, "text"),
                sp.tts_cache_path(media, "v", 1.1, "text")}) == 4


def test_tts_failure_names_the_scene(media):
    _write(media, [_scene("s1"), _scene("s2", narration="Second")])
    with pytest.raises(sp.PipelineError, match="scene s1"):
        sp.build(media, synthesize=lambda *_a: False, progress=lambda _m: None)


def test_needs_network_follows_cache_state(media):
    _write(media, [_scene("s1"), _scene("s2", audio="two.wav")])
    assert sp.needs_network(media) is True
    sp.build(media, synthesize=FakeTTS(), progress=lambda _m: None)
    assert sp.needs_network(media) is False
    _write(media, [_scene("s1", audio="one.wav")])
    assert sp.needs_network(media) is False


# -- fit ------------------------------------------------------------------

def test_fit_cover_is_default_and_contain_blur_is_accepted(media):
    _write(media, [_scene("s1"), _scene("s2", fit="cover"), _scene("s3", fit="contain_blur")])
    assert [s.fit for s in sp.load_manifest(media).scenes] == ["cover", "cover", "contain_blur"]
    _write(media, [_scene("s1", fit="stretch")])
    assert "fit" in _errors(media)


# -- trailing silence -----------------------------------------------------

def _tone_then_silence(path, tone, silence):
    _ff(["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={tone}",
         "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=mono:d={silence}",
         "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1", "-f", "wav", str(path)])


def _prepared(media, **settings):
    _tone_then_silence(media / "audio" / "tail.wav", 1.0, 1.5)
    _write(media, [_scene("s1", audio="tail.wav")], settings={**SETTINGS, **settings})
    manifest = sp.load_manifest(media)
    return manifest, sp.ensure_scene_audio(media, manifest, manifest.scenes[0], _forbidden)


def test_trailing_silence_is_trimmed_with_default_padding(media):
    _, (_, _, duration) = _prepared(media)
    assert abs(duration - 1.15) < 0.1


@pytest.mark.parametrize("padding,expected", [(0, 1.0), (0.5, 1.5)])
def test_trim_padding_is_respected(media, padding, expected):
    _, (_, _, duration) = _prepared(media, trim_padding=padding)
    assert abs(duration - expected) < 0.1


def test_trim_off_leaves_audio_unchanged(media):
    _, (_, path, duration) = _prepared(media, trim_silence=False)
    assert path == media / "audio" / "tail.wav"
    assert abs(duration - 2.5) < 0.02


def test_generated_narration_is_trimmed_too(media):
    def tail_tts(text, voice_name, rate, output):
        _tone_then_silence(output, 0.8, 1.2)
        return True

    _write(media, [_scene("s1")])
    manifest = sp.load_manifest(media)
    _, _, duration = sp.ensure_scene_audio(media, manifest, manifest.scenes[0], tail_tts)
    assert abs(duration - 0.95) < 0.1


def test_trim_settings_are_part_of_the_cache_key(media):
    _tone_then_silence(media / "audio" / "tail.wav", 1.0, 1.0)
    source = media / "audio" / "tail.wav"
    first, _ = sp.ensure_trimmed_audio(media, source, 0.1)
    second, _ = sp.ensure_trimmed_audio(media, source, 0.2)
    assert first != second
    assert sp.ensure_trimmed_audio(media, source, 0.1)[0] == first


def test_silent_audio_is_an_error(media):
    _ff(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=1", "-f", "wav", str(media / "audio" / "quiet.wav")])
    with pytest.raises(sp.PipelineError, match="silent"):
        sp.ensure_trimmed_audio(media, media / "audio" / "quiet.wav", 0.15)


def test_trim_settings_are_validated(media):
    _write(media, [_scene("s1")], settings={**SETTINGS, "trim_silence": "yes", "trim_padding": 9})
    message = _errors(media)
    assert "trim_silence" in message and "trim_padding" in message


# -- visuals --------------------------------------------------------------

def _psnr(first, second):
    done = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(first), "-i", str(second),
                           "-lavfi", "psnr", "-f", "null", "-"], capture_output=True, text=True)
    return float(re.search(r"average:(\S+)", done.stderr).group(1))


def _end_frames(clip, folder):
    _ff(["-i", str(clip), "-frames:v", "1", str(folder / "first.png")])
    _ff(["-sseof", "-0.15", "-i", str(clip), "-update", "1", "-frames:v", "1", str(folder / "last.png")])
    return folder / "first.png", folder / "last.png"


@pytest.mark.parametrize("duration", [2.37, 0.2, 1.0])
def test_image_clip_covers_fractional_duration(media, tmp_path, duration):
    out = tmp_path / "clip.mp4"
    sp.render_image_clip(media / "assets" / "a.png", out, duration=duration, width=96, height=160, fps=10)
    length = _duration(out)
    assert duration <= length <= duration + 0.5


@pytest.mark.parametrize("engine", ["zoompan", "scale_crop"])
def test_zoom_moves_and_static_does_not(media, tmp_path, engine):
    zoom, still = tmp_path / "zoom.mp4", tmp_path / "still.mp4"
    image = media / "assets" / "a.png"
    sp.render_image_clip(image, zoom, duration=3, width=96, height=160, fps=10, engine=engine)
    sp.render_image_clip(image, still, duration=3, width=96, height=160, fps=10, motion="static")
    (tmp_path / "z").mkdir()
    (tmp_path / "s").mkdir()
    zoom_psnr = _psnr(*_end_frames(zoom, tmp_path / "z"))
    still_psnr = _psnr(*_end_frames(still, tmp_path / "s"))
    assert still_psnr > zoom_psnr + 5


@pytest.mark.parametrize("asset,expected", [("short.mp4", 1.3), ("long.mp4", 1.3)])
def test_video_asset_is_looped_or_trimmed_to_audio(media, asset, expected):
    _write(media, [_scene("s1", asset=asset, audio="one.wav")])
    result = sp.build(media, synthesize=_forbidden, progress=lambda _m: None)
    assert abs(_duration(result["output"]) - expected) < 0.3


# -- end to end and caching ---------------------------------------------

def test_build_matches_total_audio_duration(media):
    _write(media, [_scene("s1", audio="one.wav"), _scene("s2", asset="short.mp4", audio="two.wav"),
                   _scene("s3", asset="b.png")])
    fake = FakeTTS()
    result = sp.build(media, synthesize=fake, progress=lambda _m: None)
    expected = 1.3 + 0.9 + (0.5 + len("Hello there") * 0.01)
    assert abs(_duration(result["output"]) - expected) < 0.5
    assert Path(result["output"]) == media / "out" / "ep.mp4"


def test_second_build_reuses_every_stage(media):
    _write(media, [_scene("s1"), _scene("s2", asset="b.png", narration="Other")])
    sp.build(media, synthesize=FakeTTS(), progress=lambda _m: None)
    again = sp.build(media, synthesize=_forbidden, progress=lambda _m: None)
    assert all(again["stages"].values())
    assert again["image_clips_rendered"] == 0


def test_changing_one_scene_rebuilds_only_that_scene(media):
    scenes = [_scene("s1"), _scene("s2", asset="b.png", narration="Second"), _scene("s3", audio="two.wav")]
    _write(media, scenes)
    sp.build(media, synthesize=FakeTTS(), progress=lambda _m: None)
    scenes[1]["narration"] = "Second line, now much longer than before"
    _write(media, scenes)
    result = sp.build(media, synthesize=FakeTTS(), progress=lambda _m: None)
    rebuilt = {key for key, reused in result["stages"].items() if not reused}
    assert rebuilt == {"s2:audio", "s2:timing", "s2:scene", "assembly", "export"}


def test_cli_validate_and_exit_codes(media, capsys):
    _write(media, [_scene("s1", asset="missing.png")])
    assert sp.main(["--project", str(media), "validate"]) == 1
    assert "missing.png" in capsys.readouterr().err
    _write(media, [_scene("s1")])
    assert sp.main(["--project", str(media), "validate"]) == 0
    assert sp.main(["--project", str(media), "needs-network"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "yes"


def test_video_project_accepts_more_than_100_scenes(media, tmp_path):
    footage, audio = media / "assets" / "short.mp4", media / "audio" / "two.wav"

    def request(count):
        return {"project_id": "big", "settings": dict(SETTINGS),
                "scenes": [{"id": f"s{i}", "footage": str(footage), "audio": str(audio)} for i in range(count)]}

    project = VideoProject(tmp_path / "big")
    assert len(project.create_revision(request(101))["scenes"]) == 101
    with pytest.raises(ProjectError, match="1000"):
        project.create_revision(request(1001))


# -- scale (opt in: SCENE_PIPELINE_SCALE_TEST=1, run with -s) -----------

def _cgroup_peak():
    for name in ("/sys/fs/cgroup/memory.peak", "/sys/fs/cgroup/memory/memory.max_usage_in_bytes"):
        try:
            return int(Path(name).read_text())
        except (OSError, ValueError):
            continue
    return None


@pytest.mark.skipif(not os.environ.get("SCENE_PIPELINE_SCALE_TEST"), reason="opt-in scale test")
def test_scale_is_fast_and_memory_flat(media, tmp_path):
    def project_of(count, name):
        project = tmp_path / name
        shutil.copytree(media, project)
        scenes = [_scene(f"s{i:04d}", asset=("a.png", "b.png", "short.mp4", "long.mp4")[i % 4],
                         audio=("one.wav", "two.wav")[i % 2]) for i in range(count)]
        _write(project, scenes, settings={"width": 270, "height": 480, "fps": 15})
        return project

    marks = {}
    for count in (100, 300):
        project = project_of(count, f"p{count}")
        started = time.monotonic()
        first = sp.build(project, synthesize=_forbidden, progress=lambda _m: None)
        built = time.monotonic() - started
        started = time.monotonic()
        again = sp.build(project, synthesize=_forbidden, progress=lambda _m: None)
        reused = time.monotonic() - started
        assert all(again["stages"].values())
        scenes = json.loads((project / "scenes.json").read_text())
        scenes["scenes"][count // 2]["audio"] = "two.wav" if scenes["scenes"][count // 2]["audio"] == "one.wav" else "one.wav"
        (project / "scenes.json").write_text(json.dumps(scenes))
        started = time.monotonic()
        changed = sp.build(project, synthesize=_forbidden, progress=lambda _m: None)
        one_change = time.monotonic() - started
        assert sum(not r for r in changed["stages"].values()) <= 5
        marks[count] = (built, reused, one_change, resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                        resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
        print(f"\nscale {count}: build {built:.1f}s, no-change {reused:.1f}s, one-scene change "
              f"{one_change:.1f}s, python peak {marks[count][3] // 1024} MB, "
              f"largest child peak {marks[count][4] // 1024} MB, cgroup peak {_cgroup_peak()}", file=sys.stderr)
        assert first["scenes"] == count
    assert marks[300][3] <= marks[100][3] * 1.25 + 50 * 1024
    assert marks[300][4] <= marks[100][4] * 1.25 + 50 * 1024
