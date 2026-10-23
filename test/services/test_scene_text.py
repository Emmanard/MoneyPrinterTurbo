"""Caption timing, text wrapping and style validation. Pure Python; needs Pillow and
the fonts in resource/fonts, but no ffmpeg."""

import pytest

from app.services import scene_text as st

LONG_NARRATION = (
    "The tour announcement landed on Monday, and fans immediately started asking whether "
    "the new stadium dates would sell out before the weekend, because last time it took "
    "less than an hour."
)


# -- caption timing -------------------------------------------------------

@pytest.mark.parametrize("duration", [0.4, 2.37, 14.9, 61.003])
@pytest.mark.parametrize("mode", list(st.CAPTION_MODES))
def test_caption_timing_covers_the_whole_scene(duration, mode):
    cues = st.caption_cues(LONG_NARRATION, duration, mode)
    assert cues[0].start == 0
    assert cues[-1].end == duration
    for before, after in zip(cues, cues[1:]):
        assert before.end == after.start
    assert all(cue.end >= cue.start for cue in cues)


@pytest.mark.parametrize("duration", [0.4, 2.37, 14.9])
def test_no_caption_outlasts_the_scene(duration):
    assert max(cue.end for cue in st.caption_cues(LONG_NARRATION, duration, "chunks")) <= duration


def test_longer_chunks_get_more_time():
    cues = st.caption_cues("Hi there. Extraordinarily incomprehensible circumstances", 6.0, "chunks")
    assert len(cues) >= 2
    short, long = cues[0], cues[-1]
    assert (long.end - long.start) > (short.end - short.start)


def test_modes_limit_words_per_caption():
    assert all(len(c.text.split()) == 1 for c in st.caption_cues(LONG_NARRATION, 10, "words"))
    assert all(len(c.text.split()) <= 3 for c in st.caption_cues(LONG_NARRATION, 10, "chunks"))
    assert all(len(c.text.split()) <= 7 for c in st.caption_cues(LONG_NARRATION, 10, "lines"))


def test_sentence_ends_break_captions():
    texts = [c.text for c in st.caption_cues("One. Two three four five. Six", 5, "chunks")]
    assert texts[0] == "One."
    assert all(not t.startswith("Two") or t.endswith(("four", "five.")) or len(t.split()) <= 3 for t in texts)


def test_pause_tags_are_not_captioned():
    cues = st.caption_cues("Hello [pause: 1s] world (silence 2s) again", 3, "words")
    assert [c.text for c in cues] == ["Hello", "world", "again"]


def test_empty_text_has_no_captions():
    assert st.caption_cues("   [pause: 1s]  ", 3, "chunks") == []


def test_too_many_captions_are_rejected():
    words = " ".join(["word"] * (st.MAX_CUES_PER_SCENE + 1))
    with pytest.raises(st.TextLayoutError, match="captions"):
        st.caption_cues(words, 100, "words")


# -- wrapping -------------------------------------------------------------

@pytest.mark.parametrize("width,height", [(1080, 1920), (1920, 1080), (540, 960)])
def test_long_text_wraps_inside_the_frame(width, height):
    style = st.resolve_style("card", width, height)
    lines, size, ys = st.layout_block(LONG_NARRATION * 2, style, width, height)
    font = st._font(st.FONT_DIR / style["font"], size)
    margin = round(st.MARGIN_X * width)
    assert len(lines) > 1
    assert all(font.getlength(line) <= width - 2 * margin for line in lines)
    ascent, descent = font.getmetrics()
    assert ys[0] >= round(st.MARGIN_TOP * height) - 1
    assert ys[-1] + ascent + descent <= height


def test_bottom_text_stays_above_the_vertical_safe_margin():
    style = st.resolve_style("caption", 1080, 1920)
    lines, size, ys = st.layout_block("Short line", style, 1080, 1920)
    font = st._font(st.FONT_DIR / style["font"], size)
    ascent, descent = font.getmetrics()
    assert ys[-1] + ascent + descent <= 1920 - round(st.MARGIN_BOTTOM_VERTICAL * 1920) + 1


def test_oversized_text_shrinks_to_fit():
    style = st.resolve_style("card", 540, 960, {"size": 200})
    lines, size, ys = st.layout_block(LONG_NARRATION, style, 540, 960)
    assert size < 200
    font = st._font(st.FONT_DIR / style["font"], size)
    assert all(font.getlength(line) <= 540 - 2 * round(st.MARGIN_X * 540) for line in lines)


def test_unfittable_text_is_an_error():
    style = st.resolve_style("card", 120, 120)
    with pytest.raises(st.TextLayoutError, match="does not fit"):
        st.layout_block("word " * 90, style, 120, 120)


def test_one_long_word_is_split_by_character():
    style = st.resolve_style("card", 540, 960)
    lines, size, _ = st.layout_block("A" * 120, style, 540, 960)
    font = st._font(st.FONT_DIR / style["font"], size)
    assert len(lines) > 1
    assert all(font.getlength(line) <= 540 - 2 * round(st.MARGIN_X * 540) for line in lines)


def test_explicit_line_breaks_are_kept():
    style = st.resolve_style("card", 540, 960)
    lines, _, _ = st.layout_block("“A quote.”\n- Someone, 2026", style, 540, 960)
    assert lines == ["“A quote.”", "- Someone, 2026"]


# -- overlays and filters -------------------------------------------------

def test_text_card_respects_its_window_in_the_filter():
    card = st.TextCard("Hello", {}, 1.0, 2.5)
    items = st.scene_overlays("", 3.0, {"enabled": False}, (card,), {}, 540, 960)
    chain = st.drawtext_chain(items, "t")
    assert "gte(t,1.000)*lt(t,2.500)" in chain.filters
    assert chain.files == {"tline0.txt": "Hello"}


def test_filters_use_relative_names_only():
    items = st.scene_overlays("Hello world again", 3.0, {"enabled": True, "mode": "words", "style": {}},
                              (), {}, 540, 960)
    chain = st.drawtext_chain(items, "a")
    assert "/" not in chain.filters.replace("(w-text_w)/2", "")
    assert all(alias.startswith("afont") for alias in chain.fonts)


def test_captions_are_timed_over_the_audio_not_the_pause():
    items = st.scene_overlays("one two three four", 2.0, {"enabled": True, "mode": "words", "style": {}},
                              (), {}, 540, 960)
    assert max(item.end for item in items) <= 2.0


def test_overlay_key_changes_with_the_text_and_style():
    base = st.scene_overlays("one two", 2.0, {"enabled": True, "mode": "words", "style": {}}, (), {}, 540, 960)
    other = st.scene_overlays("one three", 2.0, {"enabled": True, "mode": "words", "style": {}}, (), {}, 540, 960)
    colored = st.scene_overlays("one two", 2.0,
                                {"enabled": True, "mode": "words", "style": {"color": "#FF0000"}}, (), {}, 540, 960)
    keys = [st.overlay_key(items) for items in (base, other, colored)]
    assert keys[0] != keys[1] and keys[0] != keys[2]


def test_disabled_captions_produce_no_text():
    assert st.scene_overlays("hello", 2.0, {"enabled": False}, (), {}, 540, 960) == []


def test_scene_style_overrides_the_settings_default():
    merged = st.merge_caption({"enabled": True, "mode": "words", "style": {"size": 40, "color": "#FFFF00"}},
                              {"style": {"size": 60}})
    assert merged == {"enabled": True, "mode": "words", "style": {"size": 60, "color": "#FFFF00"}}
    assert st.merge_caption({"enabled": True}, {"enabled": False})["enabled"] is False


# -- validation -----------------------------------------------------------

def test_style_validation_reports_every_problem():
    errors = []
    st.parse_style({"font": "nope.ttf", "size": 2, "color": "red", "position": "left",
                    "outline_width": 99, "box": "yes", "box_opacity": 3, "shadow": 1}, "style", errors)
    joined = "\n".join(errors)
    for field in ("font", "size", "color", "position", "outline_width", "box", "box_opacity", "shadow"):
        assert field in joined


def test_valid_style_is_kept():
    errors = []
    style = st.parse_style({"font": st.DEFAULT_CAPTION_FONT, "size": 64, "color": "#FFFFFF",
                            "position": "top", "box": True}, "style", errors)
    assert errors == [] and style["size"] == 64 and style["position"] == "top"


def test_caption_spec_validation():
    errors = []
    st.parse_caption_spec({"enabled": "yes", "mode": "karaoke", "extra": 1}, "captions", errors)
    assert len(errors) == 3


def test_text_card_validation():
    errors = []
    cards = st.parse_text_cards([{"text": "", "start": -1, "end": 0}, {"text": "ok", "start": 1, "end": 2, "x": 1},
                                 "bad"], "scene s1", errors)
    joined = "\n".join(errors)
    assert "text" in joined and "start" in joined and "end" in joined and "unknown field: x" in joined
    assert "must be an object" in joined
    assert len(cards) == 2


def test_text_card_must_end_inside_the_scene():
    errors = []
    cards = (st.TextCard("a", {}, 0.0, 2.0), st.TextCard("b", {}, 1.0, 4.0))
    st.check_card_times(cards, 3.0, "scene s1", errors)
    assert len(errors) == 1 and "text_cards[1]" in errors[0]
