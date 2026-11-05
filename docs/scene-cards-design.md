# Stage D: card designs and effects, design note

Status: design only, for review. No code was written and nothing was run to produce
it. Facts about ffmpeg come from `docs/scene-effects-proposal.md` (checked in the
container, ffmpeg 5.1.9). Anything not checked there is marked **unverified** and
becomes the first task of the piece that needs it.

Rules that carry over unchanged: every new field is optional and additive; a manifest
that uses none of them renders exactly as it does today (same output, same cache
keys); unknown fields are rejected; all errors are reported together; checks that
depend on narration length run before rendering.

## 1. What exists today

- A text card is `{text, style, start, end}`. Style keys: `font`, `size`, `color`,
  `outline_color`, `outline_width`, `position` (top, center, bottom), `box`,
  `box_color`, `box_opacity`.
- One card has one style. Its text is wrapped inside the safe area (6% side margins,
  8% top, 14% bottom on vertical video), shrunk if needed, and each wrapped line is its
  own `drawtext`, centred, with its own box. Line boxes are as wide as their line, so
  the box edge is ragged.
- Fonts available: BeVietnamPro Bold and Medium, Charm, and some CJK fonts.
  BeVietnamPro has curly quotes, which the quote card needs (**unverified** for the
  exact glyph; checked in the first test).

The designs below need three things the current model cannot do: different styles on
different lines of one card, left-aligned text, and one panel behind the whole block.

## 2. Shared building blocks

1. **Per-line style.** `DrawLine` already carries its own font, size and colour; only
   the card parser forces one style. A card kind produces several `DrawLine`s with
   different styles from one layout pass. `plain` cards keep their current code path
   and their cache key stays byte for byte as it is.
2. **Alignment.** New style key `align`: `center` (default, today) or `left`. Left lines
   use `x = margin` instead of `(w-text_w)/2`. Needed by every design that has an
   accent bar.
3. **Panel.** New style key `panel` (colour and opacity) draws one `drawbox` behind the
   whole block, sized from the layout the code already computes (line widths from the
   font, line heights, margins). It replaces the ragged per-line boxes for the new
   kinds. `box` keeps its meaning for `plain` cards.
4. **Accent bar.** A `drawbox` rectangle next to or above the block (section 4).
5. **Card kind.** `text_cards[].kind`: `plain` (default), `claim`, `quote`, `fact`.
   The extra fields below belong to the kind and are rejected on other kinds.

## 3. Card designs

All sizes are for 1080x1920 and scale with the shorter side like the current defaults.

### 3.1 Label plus claim (`kind: "claim"`)

The pattern the fort-dix video builds by hand today ("TMZ:" over "FIGHT WITH ANOTHER
INMATE").

```json
{"kind": "claim", "label": "TMZ", "text": "Fight with another inmate",
 "start": 0.3, "end": 5.3, "style": {"position": "top"}}
```

- `label`: one line, up to 40 characters, small (about 55% of the claim size), accent
  colour, uppercase (drawtext has no letter spacing, so no tracking).
- `text`: the claim, up to 140 characters, large, wrapped and shrunk like today.
- Layout: label on top, claim below, left aligned, panel behind both, accent bar on the
  left edge of the panel.
- Why: the speaker or source and what they said are always two levels of emphasis;
  this removes the manual `\n` and the one-size-fits-all problem.

### 3.2 Quote card (`kind: "quote"`)

```json
{"kind": "quote", "text": "We have no additional information to provide.",
 "source": "Bureau of Prisons", "start": 4.8, "end": 9.5}
```

- `text`: up to 200 characters, medium weight, in curly quotes added by the layout (the
  author does not type them).
- `source`: one line, up to 60 characters, smaller, preceded by an em dash, dimmer
  colour.
- Layout: an oversized opening quote mark in the accent colour at the top left of the
  panel, quote text, then the source line. Left aligned, panel behind, bar optional.
- Risk: a large glyph is a separate `drawtext` positioned from the first line; if the
  quote wraps to many lines the mark must still sit at the top (it does, because it is
  placed from the block top).

### 3.3 Key-fact card (`kind: "fact"`)

```json
{"kind": "fact", "value": "Oct 26", "text": "Last location listed: Allenwood Low",
 "start": 10.6, "end": 19.7}
```

- `value`: one line, up to 14 characters (a date, a number, a name), very large (about
  2.2 times the caption size), accent colour or white.
- `text`: the caption, up to 80 characters, below the value.
- Layout: centred by default, panel behind, accent bar above the value. A value that
  does not fit on one line at the largest size is shrunk, and rejected under the
  readable minimum (same rule as today at 10 px, but with a higher floor of 48 px for
  the value so it never turns into small print).

### 3.4 Accent bar

Part of the style, not a kind: `style.accent = {"color": "#D9382B", "width": 12,
"side": "left"}`; `side` is `left` or `top`; `width` 4 to 40 px. It is a filled
rectangle the height (left) or width (top) of the panel, drawn with `drawbox`.
`settings.text_card_style` can set a default, so a whole video gets one accent colour.
Unlike `box`, it does not depend on the text colour or outline.

### 3.5 Animated entrance

Same model as section 4 of the effects proposal, extended to the new parts:

```json
"animation": {"in": "slide_up", "out": "fade", "duration": 0.3, "stagger": 0.12}
```

- `in`: `none` (default), `fade`, `slide_up`, `pop`. `out`: `none` or `fade`.
  Typewriter stays out (see section 7).
- `duration`: 0.05 to 2.0 s. In plus out must fit inside `end - start`.
- `stagger` (new, 0 to 0.5 s, default 0): each line of the card starts that much later
  than the one before, so the label lands first and the claim follows. This is what
  makes the label-plus-claim and fact cards feel designed. It costs nothing extra: it is
  a different start time in each line's expression.
- The panel and the bar enter with the first line. All parts leave together.
- A default can be set in `settings.text_card_animation`.

## 4. How it is drawn, and what is unverified

| Part | Primitive | Status |
|---|---|---|
| Text lines | `drawtext`, `alpha`, `y`, `fontsize` as expressions of `t` | Checked (proposal, 0.2) |
| Static panel and bar | `drawbox` with an `enable` window and a colour alpha | Expected to work, **unverified** here |
| Panel and bar fading or sliding with the text | `drawbox` has no time variable in its size or position | **Unverified**. Plan: smoke-test two options in the container: (a) a `color` source with an alpha channel overlaid with `overlay=x=...:y=...` (its position takes expressions of `t`), (b) fall back to a fixed panel and bar that appear with a hard cut while the text animates |
| Box fading with text | the proposal already flags that `alpha` may apply to the text only | **Unverified** (proposal 4); the same test decides whether panels must be overlays |

Decision rule: if (a) works, panel and bar animate with the text. If it does not, the new
kinds still ship with the text animating over a panel that cuts in and out, and the
note in the docs says so.

## 5. What each piece needs from the manifest

| Piece | New fields | Validation |
|---|---|---|
| Card kinds | `text_cards[].kind`, `label`, `source`, `value` | Kind-specific fields only on their kind; length caps above; text must fit the safe area |
| Alignment, panel | `style.align`, `style.panel` (`{color, opacity}`) | `left`/`center`; colour `#RRGGBB`; opacity 0 to 1 |
| Accent bar | `style.accent` (`{color, width, side}`) | ranges above |
| Entrance | `animation` per card, `settings.text_card_animation` | names, durations, stagger, in plus out fits the window |
| Look defaults | `settings.text_card_style` can carry all `style` keys | already the case today |

Nothing outside `text_cards` and `settings` changes. `schema_version` stays 1.

## 6. Caching and compatibility

- The new fields join `DrawLine`, which joins `overlay_key`, which is already in the
  scene body and junction keys. Changing one card rebuilds that scene's segments, its
  junctions and the later joins, as a text change does today.
- Fields are added to the key only when they are not at their default, so a manifest
  with only `plain` cards keeps its existing cache keys and nothing re-renders.
- If a later piece changes how every card is drawn (it should not), bump
  `SEGMENT_VERSION` in that change only.

## 7. Order of the effects

The order approved in `scene-effects-proposal.md` section 10 is unchanged:

1. **a. Transitions**, all 46 xfade names (a mapping table, no new stages).
2. **f. Audio polish.** Loudness is done in a different shape from the proposal: a
   second file after the final mix, target -14 LUFS, true peak -1.5 dBTP with 0.3 dB of
   headroom, no `mix` stage split. Still open from f: `bgm.fade_in`/`fade_out` and
   `scenes[].volume_db`. The proposal's `mix`/`measure`/`loudness` split is no longer
   needed for them; fades go on the music stem inside the existing final mix.
3. **e. Look without grain**: vignette, grade, flash.
4. **c. Text animation.** This is where the cards live. Section 3.5 is this item with
   `stagger` and the panel and bar added; typewriter stays last and optional (one
   `drawtext` per character).
5. **b. Camera with easing.**
6. **d. Synthesized sound effects**, last, since they need the audio stage and an
   audition step.

The static card kinds (3.1 to 3.4) are not in that list because the proposal did not
cover them; they are the base item 4 builds on, so they come immediately before it.

## 8. Size estimates (lines of code, not including docs; estimates, not measurements)

| Piece | Code | Tests | Risk |
|---|---|---|---|
| Primitive checks (section 4) | none, throwaway container checks | none | decides the animated panel |
| Shared blocks: `align`, `panel`, per-line style, `kind` parsing | about 150 | about 15 | low; keeps `plain` path untouched |
| Claim card | about 60 | about 8 | low |
| Fact card | about 60 | about 8 | low |
| Quote card | about 80 | about 9 | medium (glyph, wrap) |
| Accent bar | about 50 | about 6 | low if `drawbox` behaves |
| Entrance: fade, slide_up, pop, stagger | about 110 | about 12 | medium (box and alpha) |
| Typewriter (optional) | about 80 | about 5 | medium (instance count) |
| a. Transitions | about 40 | about 2 | negligible |
| f. fades and `volume_db` | about 120 | about 10 | low |
| e. vignette, grade, flash | about 200 | about 12 | medium (banding, levels) |
| b. Camera easing | about 250 | about 15 | medium (clip render and key) |
| d. Sound effects | about 350 | about 12 | medium (sound quality) |

Rendering cost of the card work: panels and bars add two or three filters per scene that
has a card, and no new stages. Expected effect on build time is under 3% (the proposal
estimates 0 to 3% for animation). Memory unchanged.

## 9. Recommendation: what to build first

1. **First: the primitive checks, then the shared blocks with the claim card** (static,
   no animation). Reasons: the label-plus-claim shape is the one already used by hand in
   the fort-dix manifest, so it shows value on a real video straight away; the shared
   blocks (`align`, `panel`, per-line style, `kind`) are the base for fact, quote, bar
   and animation; and the checks in section 4 answer the one open technical question
   before any animation code is written. This is one reviewable commit.
2. Then fact card, accent bar and quote card, one commit each or together if small.
3. Then the entrance animation (fade and slide_up first, then pop and stagger).
4. Transitions (a) is about 40 lines and can go before or after any of these; it is the
   cheapest item and does not depend on cards. If you prefer to follow the approved
   order strictly, do it first; it will not delay the rest.
5. Everything else follows the order in section 7.

## 10. Decisions (owner's answers)

1. The four kinds are right (`plain`, `claim`, `quote`, `fact`). The fact card takes a small
   label above the value.
2. Claim and quote cards are left aligned, the fact card is centred; alignment is a per-card
   setting (`style.align`).
3. A panel that cuts in and out while the text animates is acceptable. No time is spent on
   animating the panel, so the overlay experiment in section 4 is dropped.
4. The accent colour is a project default in `settings.text_card_style`, with a per-card
   override in `style`.
5. Typewriter is skipped.

## 11. Build log

**D1 (built):** the shared text-block primitive (`layout_parts`: parts with their own size and
colour, left or centred alignment, optional panel, safe margins) and the static claim card.
Deviations from the sketch above, made to fit the existing flat style dictionary:

- Style keys are flat, like `box` and `box_color`: `align`, `panel`, `panel_color`,
  `panel_opacity`, `accent_color`. There is no nested `panel` or `accent` object.
- The accent *bar* is not part of D1. `accent_color` only colours the claim label for now;
  D2 adds the bar (`accent_width`, `accent_side`) using the same colour.
- For claim cards the kind defaults (`align: left`, `panel: true`, `box: false`) are applied
  after `settings.text_card_style` and before the card's own style.
- Plain cards with the default alignment and no panel use the original code path, so their
  output and cache keys are unchanged.

**D2 (built):** fact card (optional label, value, optional caption), quote card (mark, quote,
dim source) and the accent bar (`accent_width`, `accent_side`), all static. Notes:

- Fact and quote cards default to a 10 px bar (top and left); claim and plain cards default
  to none, so their output is unchanged (checked frame by frame against the D1 preview).
- The bar is part of the panel and is only drawn when the panel is on.
- A left bar on a centred card pads both sides, so the text stays centred in the panel.
- The 48 px fact value floor scales with the frame (48 px at 1080).
- The quote mark is drawn at 2.4 times the size and reserves 45% of its line height, since the
  glyph sits at the top of its line box.

Next: animation (fade, slide_up, pop, stagger). Not started.

## 12. Workflow upgrade log (E1 to E6)

Built in one run on top of D2, nothing committed. Each stage is saved as its own patch from
the previous stage (`scene-projects/patches/E<n>.patch`), applied in order on HEAD. Every
stage keeps manifests that do not use its features byte-identical in output and cache keys.

**E1 transitions (built):** the 38 remaining ffmpeg xfade transitions, so all 46 are
available (plus `cut`). No new stages; the junction key already contains the xfade name. A test
checks every mapped name against `ffmpeg -h filter=xfade` and that no ffmpeg transition is
missing; the existing parametrised test builds all 46 and checks length and sync.
`docs/scene-effects-proposal.md` is included in this stage as the design record.

**E2 audio polish (built):** `bgm.fade_in` / `bgm.fade_out` (0 to 10 s) and `scenes[].volume_db`
(-30 to 12 dB). Loudness normalisation was already built (stage B) and is unchanged.
Changes from the proposal: the proposal split the final mux into `mix`, `measure` and
`loudness` stages; that is no longer needed because loudness already runs after the final mux,
so the fades go on the music stem inside the existing final mix and the gain goes into the
existing narration segments. Per-scene gain and fades enter cache keys only when set, so old
manifests keep their keys. `plan` shows fades and gains, and reports fades longer than the
video.

**E3 look (built):** `settings.look` and `scenes[].look` with a vignette and a colour grade
(presets plus contrast, saturation, brightness), and `transition.flash`. Grain is deferred, as
decided in the proposal. Code is in the new `app/services/scene_look.py`. The look sits in the
per-scene chain before the text, so it applies to the body and to both sides of a junction.
Look and flash enter the cache keys only when set. Changes from the proposal:
the flash is a solid colour overlay with an alpha envelope (`fade ... alpha=1`) instead of an
`eq` brightness pulse, because that supports a flash colour and was checked in the container
before use; the vignette angle grows with `amount` (a larger lens angle darkens more, measured
in the container; my first guess had the direction reversed and a test caught it).

**E4 card animation (built):** `animation {in, out, duration, stagger}` per card and
`settings.text_card_animation` as the project default. Entrances: `fade`, `slide_up`,
`slide_left`; exit: `fade`; `stagger` delays each part (label, then claim) by a fixed step. The
panel and bar cut in and out (decided in the review); text lines animate with `drawtext`
expressions of `t` (alpha, and y or x for the slides). Changes from the proposal: `pop` and
`typewriter` are not built (the request named slide and fade entrances; typewriter was skipped in
the review), and `slide_left` was added because left-aligned claim and quote cards enter
naturally from the left. A probe showed that a plain card's per-line box fades with the text,
since `alpha` applies to the box in this ffmpeg; the proposal flagged this as unverified. The
animation is part of each line's overlay key only when set, so static cards keep their keys.

**E5 camera easing (built):** `camera {from, to, ease}` on a scene or an `assets[]` entry, an
alternative to `motion` (never both), images only. The pure parts are in the new
`app/services/scene_camera.py`; the zoom filters in `scene_pipeline` accept a camera wherever
they accepted a preset name, so the presets keep their exact filter strings and the single-image
legacy path is untouched. Changes from the proposal: the clip key version stays `aclip-v1`
(the proposal bumped it) because a camera adds a structured element to the key only when set,
and old manifests must keep their keys; the x and y expressions use the zoom expression
directly instead of the `zoompan` `zoom` variable, which lags one frame; `to` fields missing
from the camera inherit `from`.
