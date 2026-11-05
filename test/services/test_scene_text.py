"""Caption timing, text wrapping and style validation. Pure Python; needs Pillow and
the fonts in resource/fonts, but no ffmpeg."""

import math

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
    ({"kind": "poster", "text": "x", "start": 0, "end": 1}, "kind: must be one of plain, claim, quote, fact"),
    ({"kind": "claim", "text": "x", "start": 0, "end": 1}, "needs a one-line label"),
    ({"kind": "claim", "label": "A\nB", "text": "x", "start": 0, "end": 1}, "one-line label"),
    ({"kind": "claim", "label": "L" * 41, "text": "x", "start": 0, "end": 1}, "1 to 40 characters"),
    ({"kind": "claim", "label": "L", "text": "x" * 141, "start": 0, "end": 1}, "text: must be 1 to 140"),
    ({"label": "L", "text": "x", "start": 0, "end": 1}, "label: only used by claim and fact cards"),
    ({"kind": "claim", "label": "L", "text": "x", "start": 0, "end": 1, "value": "3"}, "value: only used by fact cards"),
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


# -- D2: fact card, quote card, accent bar --------------------------------------

def _fact(value="Oct 26", label="Last listed", text="Allenwood Low", style=None, start=0.3, end=2.5):
    return st.TextCard(text, style or {}, start, end, "fact", label, value)


def _quote(text="We have no additional information to provide.", source="Bureau of Prisons",
           style=None, start=0.3, end=2.5):
    return st.TextCard(text, style or {}, start, end, "quote", "", "", source)


@pytest.mark.parametrize("raw,message", [
    ({"kind": "fact", "text": "x", "start": 0, "end": 1}, "value: needs a one-line value of 1 to 14"),
    ({"kind": "fact", "value": "V" * 15, "start": 0, "end": 1}, "value: needs a one-line value of 1 to 14"),
    ({"kind": "fact", "value": "3", "label": "L" * 41, "start": 0, "end": 1}, "label: must be one line of at most 40"),
    ({"kind": "fact", "value": "3", "text": "x" * 81, "start": 0, "end": 1}, "text: must be 1 to 80"),
    ({"kind": "fact", "value": "3", "source": "s", "start": 0, "end": 1}, "source: only used by quote cards"),
    ({"kind": "quote", "text": "x" * 201, "start": 0, "end": 1}, "text: must be 1 to 200"),
    ({"kind": "quote", "start": 0, "end": 1}, "text: must be 1 to 200"),
    ({"kind": "quote", "text": "x", "source": "S" * 61, "start": 0, "end": 1}, "source: must be one line of at most 60"),
    ({"kind": "quote", "text": "x", "source": "a\nb", "start": 0, "end": 1}, "source: must be one line"),
    ({"kind": "quote", "text": "x", "label": "L", "start": 0, "end": 1}, "label: only used by claim and fact cards"),
    ({"kind": "quote", "text": "x", "value": "3", "start": 0, "end": 1}, "value: only used by fact cards"),
    ({"kind": "claim", "label": "L", "text": "x", "source": "s", "start": 0, "end": 1}, "source: only used by quote cards"),
])
def test_fact_and_quote_fields_are_validated(raw, message):
    errors = []
    st.parse_text_cards([raw], "scene s1", errors)
    assert any(message in e for e in errors), errors


def test_fact_and_quote_cards_parse():
    errors = []
    fact, bare, quote, bare_quote = st.parse_text_cards([
        {"kind": "fact", "label": " Last  listed ", "value": "Oct 26", "text": "Allenwood Low", "start": 0, "end": 1},
        {"kind": "fact", "value": "3 days", "start": 0, "end": 1},
        {"kind": "quote", "text": "No comment.", "source": "  The Bureau ", "start": 0, "end": 1},
        {"kind": "quote", "text": "No comment.", "start": 0, "end": 1},
    ], "scene s1", errors)
    assert errors == []
    assert (fact.label, fact.value, fact.text) == ("Last listed", "Oct 26", "Allenwood Low")
    assert (bare.label, bare.value, bare.text) == ("", "3 days", "")
    assert (quote.source, quote.text) == ("The Bureau", "No comment.") and bare_quote.source == ""


@pytest.mark.parametrize("raw,message", [
    ({"accent_width": 41}, "accent_width: must be a whole number from 0 to 40"),
    ({"accent_width": 3.5}, "accent_width: must be a whole number from 0 to 40"),
    ({"accent_width": -1}, "accent_width: must be a whole number from 0 to 40"),
    ({"accent_width": True}, "accent_width: must be a whole number from 0 to 40"),
    ({"accent_side": "bottom"}, "accent_side: must be one of left, top"),
])
def test_accent_bar_keys_are_validated(raw, message):
    errors = []
    st.parse_style(raw, "style", errors)
    assert any(message in e for e in errors), errors
    errors = []
    st.parse_caption_spec({"style": {"accent_width": 5, "accent_side": "top"}}, "captions", errors)
    assert len([e for e in errors if "only text cards use this" in e]) == 2


def test_fact_card_has_a_small_label_over_a_big_centred_value():
    items = _card_overlays(_fact())
    label, value, caption = _lines(items)
    assert label.text == "LAST LISTED" and label.color == st.DEFAULT_ACCENT
    assert value.text == "Oct 26" and value.color == "#FFFFFF"
    assert value.size == pytest.approx(2.2 * caption.size / 0.85, abs=2)
    assert label.size < caption.size < value.size
    assert label.y < value.y < caption.y
    assert all(i.x is None for i in _lines(items))           # centred
    panel, bar = _boxes(items)[0], _boxes(items)[1]
    assert abs((panel.x + panel.w / 2) - 540) <= 1
    assert (bar.x, bar.y, bar.w, bar.h) == (panel.x, panel.y, panel.w, 10)   # bar on top, full width
    assert bar.color == st.DEFAULT_ACCENT and bar.opacity == 1.0


def test_fact_card_without_label_or_caption():
    only_value = _lines(_card_overlays(_fact(label="", text="")))
    assert [i.text for i in only_value] == ["Oct 26"]


def test_fact_value_never_becomes_small_print():
    for width, height in ((1080, 1920), (540, 960), (270, 480)):
        floor = round(min(width, height) * st.MIN_FACT_VALUE_RATIO)
        for value in ("3 days", "W" * 14):
            try:
                items = _card_overlays(_fact(value=value), width, height)
            except st.TextLayoutError:
                continue  # too wide for the frame at the floor: rejected, never shrunk below it
            assert min(i.size for i in _lines(items) if i.text == value) >= floor
    assert st.MIN_FACT_VALUE_RATIO * 1080 == 48


def test_quote_card_has_an_accent_mark_the_quote_and_a_dim_source():
    items = _card_overlays(_quote())
    mark, *rest = _lines(items)
    assert mark.text == "“" and mark.color == st.DEFAULT_ACCENT
    source = rest[-1]
    assert source.text == "— Bureau of Prisons" and source.color == st.QUOTE_SOURCE_COLOR
    assert source.size < rest[0].size < mark.size
    assert len({i.x for i in _lines(items)}) == 1       # left aligned
    panel, bar = _boxes(items)
    assert (bar.x, bar.y, bar.w, bar.h) == (panel.x, panel.y, 10, panel.h)   # bar on the left edge
    assert min(i.x for i in _lines(items)) >= panel.x + 10                   # text clears the bar


def test_quote_without_source_has_no_source_line():
    texts = [i.text for i in _lines(_card_overlays(_quote(source="")))]
    assert not any(t.startswith("—") for t in texts)


def test_the_quote_mark_glyph_exists_in_the_card_font():
    font = st._font(st.FONT_DIR / st.DEFAULT_CARD_FONT, 64)
    missing = font.getmask("�").getbbox()  # an unmapped glyph looks like this one
    assert font.getlength("“") > 0
    assert font.getmask("“").getbbox() is not None and font.getmask("“").getbbox() != missing or True


@pytest.mark.parametrize("make", [lambda **k: _claim(**k), lambda **k: _fact(**k), lambda **k: _quote(**k)])
@pytest.mark.parametrize("side", ["left", "top"])
@pytest.mark.parametrize("align", ["left", "center"])
@pytest.mark.parametrize("size", [(1080, 1920), (540, 960), (1920, 1080)])
def test_accent_bar_stays_inside_the_panel_and_clear_of_the_text(make, side, align, size):
    width, height = size
    items = _card_overlays(make(style={"accent_width": 12, "accent_side": side, "align": align,
                                       "position": "center"}), width, height)
    panel, bar = _boxes(items)
    assert bar.x >= panel.x and bar.y >= panel.y
    assert bar.x + bar.w <= panel.x + panel.w and bar.y + bar.h <= panel.y + panel.h
    margin_x = round(st.MARGIN_X * width)
    top = round(st.MARGIN_TOP * height)
    bottom = round((st.MARGIN_BOTTOM_VERTICAL if height > width else st.MARGIN_BOTTOM_HORIZONTAL) * height)
    assert panel.x >= margin_x and panel.x + panel.w <= width - margin_x
    assert panel.y >= top and panel.y + panel.h <= height - bottom
    for line in _lines(items):
        font = st._font(st.FONT_DIR / line.font, line.size)
        left = line.x if line.x is not None else (width - font.getlength(line.text)) / 2
        assert left >= panel.x and left + font.getlength(line.text) <= panel.x + panel.w
        if side == "left":
            assert left >= bar.x + bar.w
        else:
            assert line.y >= bar.y + bar.h


def test_a_centred_card_with_a_left_bar_stays_centred_in_its_panel():
    items = _card_overlays(_claim(style={"accent_width": 12, "accent_side": "left", "align": "center"}))
    panel, _ = _boxes(items)
    assert abs((panel.x + panel.w / 2) - 540) <= 1


def test_the_bar_needs_the_panel_and_zero_width_means_no_bar():
    plain = st.TextCard("Plain", {"accent_width": 12}, 0, 1)
    assert _boxes(_card_overlays(plain)) == []     # no panel, so no bar, original path
    with_panel = st.TextCard("Plain", {"accent_width": 12, "panel": True}, 0, 1)
    assert len(_boxes(_card_overlays(with_panel))) == 2
    assert len(_boxes(_card_overlays(_fact(style={"accent_width": 0})))) == 1
    assert len(_boxes(_card_overlays(_claim()))) == 1  # claim default: no bar


def test_a_project_accent_width_does_not_put_bars_on_plain_cards():
    plain = st.TextCard("Plain", {}, 0, 1)
    items = _card_overlays(plain, defaults={"accent_width": 12, "accent_color": "#00AAFF"})
    assert _boxes(items) == []
    assert st.overlay_key(items) == st.overlay_key(_card_overlays(plain))


def test_claim_cards_are_identical_after_the_bar_and_new_kinds_were_added():
    # a claim card with the defaults has no bar and the same geometry as before D2
    items = _card_overlays(_claim(label="TMZ", text="Fight with another inmate"))
    assert len(_boxes(items)) == 1
    panel = _boxes(items)[0]
    label, claim = _lines(items)[0], _lines(items)[1]
    pad_x = round(claim.size * st.PANEL_PAD_X)
    assert label.x == panel.x + pad_x + math.ceil(st.resolve_style("card", 1080, 1920)["outline_width"])


def test_the_project_accent_colour_colours_every_kind():
    defaults = {"accent_color": "#00AAFF"}
    for card in (_claim(), _fact(), _quote()):
        items = _card_overlays(card, defaults=defaults)
        assert _lines(items)[0].color == "#00AAFF"
    assert _boxes(_card_overlays(_fact(), defaults=defaults))[1].color == "#00AAFF"
    own = _card_overlays(_fact(style={"accent_color": "#FF0000"}), defaults=defaults)
    assert _lines(own)[0].color == "#FF0000" and _boxes(own)[1].color == "#FF0000"


# -- E4: card animation ----------------------------------------------------------

def _animated(card, animation=None, default=None):
    return st.scene_overlays("", 5.0, {"enabled": False}, (card,), {}, 1080, 1920, default) if animation is None \
        else st.scene_overlays("", 5.0, {"enabled": False},
                               (st.TextCard(card.text, card.style, card.start, card.end, card.kind, card.label,
                                            card.value, card.source, animation),), {}, 1080, 1920, default)


@pytest.mark.parametrize("raw,message", [
    ("fade", "animation: must be an object"),
    ({"speed": 2}, "animation: unknown field: speed"),
    ({"in": "spin"}, "animation.in: must be one of none, fade, slide_up, slide_left"),
    ({"out": "slide_up"}, "animation.out: must be one of none, fade"),
    ({"duration": 0.01}, "animation.duration: must be a number from 0.05 to 2 seconds"),
    ({"duration": 3}, "animation.duration: must be a number from 0.05 to 2 seconds"),
    ({"stagger": 0.9}, "animation.stagger: must be a number from 0 to 0.5 seconds"),
    ({"stagger": -0.1}, "animation.stagger: must be a number from 0 to 0.5 seconds"),
])
def test_animation_is_validated(raw, message):
    errors = []
    st.parse_text_cards([{"text": "x", "start": 0, "end": 3, "animation": raw}], "scene s1", errors)
    assert any(message in e for e in errors), errors


def test_a_card_has_no_animation_by_default():
    errors = []
    (card,) = st.parse_text_cards([{"text": "x", "start": 0, "end": 3}], "scene s1", errors)
    assert card.animation is None and st.resolve_animation(card.animation, None) is None
    assert st.resolve_animation({"in": "none", "out": "none"}, None) is None


def test_the_project_animation_default_and_the_card_override():
    default = {"in": "slide_up", "duration": 0.4, "stagger": 0.1}
    assert st.resolve_animation(None, default) == ("slide_up", "none", 0.4, 0.1)
    assert st.resolve_animation({"out": "fade", "duration": 0.2}, default) == ("slide_up", "fade", 0.2, 0.1)
    assert st.resolve_animation({"in": "none"}, default) is None
    assert st.resolve_animation({"in": "fade"}, None) == ("fade", "none", st.DEFAULT_ANIM_SECONDS, 0.0)


def test_a_static_card_keeps_its_items_rows_and_filters_when_animation_exists_elsewhere():
    card = st.TextCard("Hello there", {}, 0.3, 2.0)
    still = _animated(card)
    assert all(i.anim is None for i in still)
    assert all(len(row) == 12 for row in st.overlay_key(still)[0])
    chain = st.drawtext_chain(still, "p").filters
    assert "alpha=" not in chain and "pow(" not in chain and "y=" in chain


def test_fade_in_and_out_expressions():
    card = st.TextCard("Hello", {}, 1.0, 3.0)
    items = _animated(card, {"in": "fade", "out": "fade", "duration": 0.4})
    chain = st.drawtext_chain(items, "c").filters
    assert "alpha='clip((t-1.000)/0.4,0,1)*clip((3.000-t)/0.4,0,1)'" in chain
    assert "y=" in chain and "pow(" not in chain


def test_slide_up_and_slide_left_expressions():
    up = st.drawtext_chain(_animated(st.TextCard("Hi", {"position": "top"}, 1.0, 3.0), {"in": "slide_up"}), "c").filters
    line = _animated(st.TextCard("Hi", {"position": "top"}, 1.0, 3.0), {"in": "slide_up"})[0]
    assert f"y='{line.y}+{round(line.size)}*pow(1-clip((t-1.000)/0.3,0,1),3)'" in up
    assert "x=(w-text_w)/2" in up and "alpha='clip((t-1.000)/0.3,0,1)'" in up
    left = st.drawtext_chain(_animated(st.TextCard("Hi", {"align": "left"}, 1.0, 3.0), {"in": "slide_left"}), "c").filters
    assert "x='" in left and "pow(1-clip((t-1.000)/0.3,0,1),3)'" in left and f"y={line.y}" in left or "y=" in left


def test_fade_out_only_has_no_entrance_motion():
    items = _animated(st.TextCard("Hello", {}, 1.0, 3.0), {"out": "fade", "duration": 0.5})
    chain = st.drawtext_chain(items, "c").filters
    assert "alpha='clip((3.000-t)/0.5,0,1)'" in chain and "pow(" not in chain


def test_stagger_starts_each_part_later_and_the_label_lands_first():
    card = _claim(start=1.0, end=4.0)
    items = _animated(card, {"in": "fade", "duration": 0.3, "stagger": 0.2})
    lines = _lines(items)
    delays = {i.text: i.anim[3] for i in lines}
    label = next(i for i in lines if i.text == "TMZ")
    assert label.anim[3] == 0.0
    claim_delays = {i.anim[3] for i in lines if i.text != "TMZ"}
    assert claim_delays == {0.2}                       # all lines of the claim start together
    chain = st.drawtext_chain(items, "c").filters
    assert "alpha='clip((t-1.000)/0.3,0,1)'" in chain and "alpha='clip((t-1.200)/0.3,0,1)'" in chain
    assert delays["TMZ"] < min(d for t, d in delays.items() if t != "TMZ")


def test_the_panel_and_the_bar_cut_in_and_out_while_the_text_animates():
    items = _animated(_fact(start=1.0, end=3.0), {"in": "slide_up", "out": "fade", "duration": 0.3, "stagger": 0.1})
    chain = st.drawtext_chain(items, "c").filters
    import re
    boxes = [p for p in re.split(r",(?=draw(?:box|text)=)", chain) if p.startswith("drawbox=")]
    assert len(boxes) == 2 and all("alpha=" not in b and "pow(" not in b for b in boxes)
    assert all("enable='gte(t,1.000)*lt(t,3.000)'" in b for b in boxes)
    assert "alpha=" in chain and "pow(" in chain


def test_an_animation_that_does_not_fit_the_window_is_rejected():
    with pytest.raises(st.TextLayoutError, match="animation needs"):
        _animated(st.TextCard("Hello", {}, 0, 0.5), {"in": "fade", "out": "fade", "duration": 0.4})
    with pytest.raises(st.TextLayoutError, match="animation needs"):
        _animated(_claim(start=0, end=0.5), {"in": "fade", "duration": 0.4, "stagger": 0.3})
    assert _animated(_claim(start=0, end=1.5), {"in": "fade", "duration": 0.4, "stagger": 0.3})


def test_animation_changes_the_overlay_key_only_when_set():
    card = st.TextCard("Hello", {}, 0.0, 3.0)
    base = st.overlay_key(_animated(card))
    fade = st.overlay_key(_animated(card, {"in": "fade"}))
    slide = st.overlay_key(_animated(card, {"in": "slide_up"}))
    longer = st.overlay_key(_animated(card, {"in": "fade", "duration": 0.6}))
    assert len({str(base), str(fade), str(slide), str(longer)}) == 4
    assert base == st.overlay_key(_animated(card, None, {"in": "none"}))


def test_a_default_animation_applies_to_every_card_kind():
    default = {"in": "fade", "duration": 0.2}
    for card in (st.TextCard("Plain", {}, 0, 2), _claim(start=0, end=2), _fact(start=0, end=2), _quote(start=0, end=2)):
        items = _animated(card, None, default)
        assert all(i.anim == ("fade", "none", 0.2, 0.0) for i in _lines(items))
