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


# -- D1: shared text block and claim card -------------------------------------

def _card_overlays(card, width=1080, height=1920, defaults=None):
    return st.scene_overlays("", 3.0, {"enabled": False}, (card,), defaults or {}, width, height)


def _claim(label="TMZ", text="Fight with another inmate", style=None, start=0.3, end=2.5):
    return st.TextCard(text, style or {}, start, end, "claim", label)


def _lines(items):
    return [i for i in items if isinstance(i, st.DrawLine)]


def _boxes(items):
    return [i for i in items if isinstance(i, st.DrawBox)]


def test_plain_cards_keep_the_original_items_rows_and_filters():
    card = st.TextCard("Hello there, a plain card", {"position": "top"}, 0.3, 2.0)
    items = _card_overlays(card, 540, 960)
    style = st.resolve_style("card", 540, 960, {}, {}, card.style)
    assert items == st._draw_lines(card.text, style, 0.3, 2.0, 540, 960)
    assert all(i.x is None for i in items)
    rows = st.overlay_key(items)[0]
    assert all(len(row) == 12 for row in rows)
    chain = st.drawtext_chain(items, "p").filters
    assert "x=(w-text_w)/2" in chain and "drawbox" not in chain


def test_the_new_style_keys_do_not_change_a_plain_cards_key():
    card = st.TextCard("Hello there", {}, 0.0, 1.0)
    first = st.overlay_key(_card_overlays(card, 540, 960))
    with_defaults = st.overlay_key(_card_overlays(card, 540, 960, {"align": "center", "panel": False,
                                                                     "accent_color": "#FF0000"}))
    assert first == with_defaults


@pytest.mark.parametrize("raw,message", [
    ({"kind": "poster", "text": "x", "start": 0, "end": 1}, "kind: must be one of plain, claim"),
    ({"kind": "claim", "text": "x", "start": 0, "end": 1}, "a claim card needs a one-line label"),
    ({"kind": "claim", "label": "A\nB", "text": "x", "start": 0, "end": 1}, "one-line label"),
    ({"kind": "claim", "label": "L" * 41, "text": "x", "start": 0, "end": 1}, "1 to 40 characters"),
    ({"kind": "claim", "label": "L", "text": "x" * 141, "start": 0, "end": 1}, "text: must be 1 to 140"),
    ({"label": "L", "text": "x", "start": 0, "end": 1}, "label: only used by claim cards"),
    ({"kind": "claim", "label": "L", "text": "x", "start": 0, "end": 1, "value": "3"}, "unknown field: value"),
])
def test_card_kind_fields_are_validated(raw, message):
    errors = []
    st.parse_text_cards([raw], "scene s1", errors)
    assert any(message in e for e in errors), errors


def test_claim_card_parses_and_normalises_its_label():
    errors = []
    (card,) = st.parse_text_cards([{"kind": "claim", "label": "  Page   Six ", "text": "Hit his head",
                                    "start": 1, "end": 3}], "scene s1", errors)
    assert errors == [] and card.kind == "claim" and card.label == "Page Six"


@pytest.mark.parametrize("raw,message", [
    ({"align": "right"}, "align: must be one of center, left"),
    ({"panel": "yes"}, "panel: must be true or false"),
    ({"panel_opacity": 2}, "panel_opacity: must be a number from 0 to 1"),
    ({"panel_color": "red"}, "panel_color: must be a color like #RRGGBB"),
    ({"accent_color": "#12"}, "accent_color: must be a color like #RRGGBB"),
])
def test_new_card_style_keys_are_validated(raw, message):
    errors = []
    st.parse_style(raw, "style", errors)
    assert any(message in e for e in errors), errors


def test_captions_reject_the_card_only_style_keys():
    errors = []
    st.parse_caption_spec({"style": {"panel": True, "align": "left", "color": "#FFFFFF"}}, "captions", errors)
    assert len([e for e in errors if "only text cards use this" in e]) == 2


@pytest.mark.parametrize("size", [(1080, 1920), (540, 960), (1920, 1080)])
@pytest.mark.parametrize("position", ["top", "center", "bottom"])
def test_claim_card_stays_inside_the_safe_margins(size, position):
    width, height = size
    items = _card_overlays(_claim(text="Both say he went to a hospital on October 10 after a fight",
                                  style={"position": position}), width, height)
    (panel,) = _boxes(items)
    margin_x = round(st.MARGIN_X * width)
    top = round(st.MARGIN_TOP * height)
    bottom = round((st.MARGIN_BOTTOM_VERTICAL if height > width else st.MARGIN_BOTTOM_HORIZONTAL) * height)
    assert panel.x >= margin_x and panel.x + panel.w <= width - margin_x
    assert panel.y >= top and panel.y + panel.h <= height - bottom
    style = st.resolve_style("card", width, height, st.KIND_DEFAULTS["claim"])
    for line in _lines(items):
        font = st._font(st.FONT_DIR / line.font, line.size)
        assert line.x >= panel.x and line.x + font.getlength(line.text) <= panel.x + panel.w
        assert line.y >= panel.y and line.y + sum(font.getmetrics()) <= panel.y + panel.h
    assert style["panel"] is True


def test_claim_card_has_a_small_accent_label_over_a_larger_claim():
    items = _card_overlays(_claim(label="Page Six", text="Hit his head on a bunk"))
    assert _boxes(items) and isinstance(items[0], st.DrawBox)  # the panel is drawn first
    label, *claim = _lines(items)
    assert label.text == "PAGE SIX" and label.color == st.DEFAULT_ACCENT
    assert claim and all(c.color == "#FFFFFF" for c in claim)
    assert label.size == pytest.approx(0.55 * claim[0].size, abs=1)
    assert label.y < claim[0].y
    assert len({i.x for i in _lines(items)}) == 1  # left aligned: one shared left edge


def test_claim_card_defaults_and_overrides():
    plain_defaults = {"box": True, "color": "#EEEEEE", "accent_color": "#00AAFF"}
    items = _card_overlays(_claim(), defaults=plain_defaults)
    label = _lines(items)[0]
    assert label.color == "#00AAFF"                      # project default from settings
    assert all(i.box is False for i in _lines(items))    # the panel replaces per-line boxes
    items = _card_overlays(_claim(style={"accent_color": "#FF0000"}), defaults=plain_defaults)
    assert _lines(items)[0].color == "#FF0000"           # the card's own value wins
    items = _card_overlays(_claim(style={"panel_opacity": 0.9, "panel_color": "#102030"}))
    assert (_boxes(items)[0].color, _boxes(items)[0].opacity) == ("#102030", 0.9)


def test_claim_panel_is_dark_enough_to_read_over_a_photo_by_default():
    (panel,) = _boxes(_card_overlays(_claim()))
    assert panel.opacity >= 0.6 and panel.color == "#000000"


def test_alignment_is_a_per_card_setting():
    centred = _card_overlays(_claim(style={"align": "center"}))
    assert all(i.x is None for i in _lines(centred))
    (panel,) = _boxes(centred)
    assert abs((panel.x + panel.w / 2) - 540) <= 1
    left_plain = _card_overlays(st.TextCard("Plain but left", {"align": "left"}, 0, 1))
    assert all(i.x is not None for i in _lines(left_plain)) and not _boxes(left_plain)
    panelled = _card_overlays(st.TextCard("Plain with panel", {"panel": True}, 0, 1))
    assert len(_boxes(panelled)) == 1 and all(i.x is None for i in _lines(panelled))


def test_a_long_claim_shrinks_instead_of_overflowing():
    text = "word " * 27  # 135 characters
    items = _card_overlays(_claim(text=text.strip(), style={"size": 160}), 540, 960)
    (panel,) = _boxes(items)
    assert max(i.size for i in _lines(items)) < 160
    assert panel.y >= round(st.MARGIN_TOP * 960)
    assert panel.y + panel.h <= 960 - round(st.MARGIN_BOTTOM_VERTICAL * 960)


def test_a_claim_that_cannot_fit_is_rejected():
    with pytest.raises(st.TextLayoutError):
        _card_overlays(_claim(text="word " * 27), 120, 120)


def test_overlay_key_and_filters_for_a_claim_card():
    base = st.overlay_key(_card_overlays(_claim()))
    assert base != st.overlay_key(_card_overlays(_claim(label="Other")))
    assert base != st.overlay_key(_card_overlays(_claim(style={"accent_color": "#00FF00"})))
    assert base != st.overlay_key(_card_overlays(_claim(style={"panel_opacity": 0.5})))
    assert base == st.overlay_key(_card_overlays(_claim()))
    chain = st.drawtext_chain(_card_overlays(_claim()), "c").filters
    first, *rest = chain.split(",drawtext=")
    assert first.startswith("drawbox=") and ":t=fill:" in first and "@0.72" in first
    assert "enable='gte(t,0.300)*lt(t,2.500)'" in first
    assert all(part.count("x=(w-text_w)/2") == 0 for part in rest)


def test_a_claim_card_colour_override_does_not_leak_into_plain_cards():
    plain = st.TextCard("Plain", {}, 0, 1)
    items = _card_overlays(plain, defaults={"accent_color": "#00AAFF"})
    assert all(i.color == "#FFFFFF" for i in _lines(items))
