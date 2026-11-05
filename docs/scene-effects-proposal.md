# Scene effects: Tier 1 design proposal

Status: design only. Nothing in this document is implemented, and no code was
changed to write it. Every claim about ffmpeg below was checked inside the
hardened container (ffmpeg 5.1.9, network off) unless it is marked "estimate".

All new manifest fields are optional and additive. A manifest that uses none of
them must produce exactly what it produces today, and unknown fields stay
rejected, so older manifests keep working and `schema_version` stays 1.

## 0. What was checked

### 0.1 xfade transitions in ffmpeg 5.1

`ffmpeg -h filter=xfade` lists 46 named transitions (numbers 0 to 45) plus
`custom` (an expression, see 2.5). We map 8 of them today.

| Already mapped (manifest name -> xfade name) | |
|---|---|
| `crossfade` -> `fade`, `fade_black` -> `fadeblack`, `fade_white` -> `fadewhite`, `dissolve` -> `dissolve` | 4 |
| `wipe_left` -> `wipeleft`, `wipe_right` -> `wiperight`, `slide_left` -> `slideleft`, `slide_right` -> `slideright` | 4 |

38 names would be added (proposed manifest name in brackets):

| Group | xfade names |
|---|---|
| Wipes | `wipeup` [wipe_up], `wipedown` [wipe_down], `wipetl` [wipe_tl], `wipetr` [wipe_tr], `wipebl` [wipe_bl], `wipebr` [wipe_br] |
| Slides | `slideup` [slide_up], `slidedown` [slide_down] |
| Smooth slides | `smoothleft`, `smoothright`, `smoothup`, `smoothdown` [smooth_left, smooth_right, smooth_up, smooth_down] |
| Shapes | `circlecrop` [circle_crop], `rectcrop` [rect_crop], `circleopen` [circle_open], `circleclose` [circle_close], `radial`, `distance` |
| Doors | `vertopen` [vert_open], `vertclose` [vert_close], `horzopen` [horz_open], `horzclose` [horz_close] |
| Diagonals | `diagtl` [diag_tl], `diagtr` [diag_tr], `diagbl` [diag_bl], `diagbr` [diag_br] |
| Slices | `hlslice` [hl_slice], `hrslice` [hr_slice], `vuslice` [vu_slice], `vdslice` [vd_slice] |
| Other | `pixelize`, `hblur`, `fadegrays` [fade_grays], `squeezeh` [squeeze_h], `squeezev` [squeeze_v], `zoomin` [zoom_in], `fadefast` [fade_fast], `fadeslow` [fade_slow] |

### 0.2 Filters

All 16 filters you named exist in the container: `drawtext`, `zoompan`,
`rgbashift`, `vignette`, `noise`, `eq`, `curves`, `hue`, `gblur`, `boxblur`,
`tblend`, `anoisesrc`, `lowpass`, `afade`, `adelay`, `amix`. None is missing. These
extra ones also exist: `loudnorm`, `alimiter`, `acompressor`, `dynaudnorm`,
`volume`, `aevalsrc`, `sine`, `aresample`, `apad`, `atrim`, `highpass`, `bandpass`,
`aecho`, `colorbalance`, `colorchannelmixer`, `lutrgb`, `lutyuv`, `geq`, `unsharp`,
`chromashift`, `blend`, `overlay`, `alphamerge`, `maskedmerge`, `colorkey`,
`chromakey`, `lut3d`, `sidechaincompress`, `agate`.

Smoke tests that passed (a few frames each to a null sink):

- `drawtext` with `alpha` as an expression of `t`, `y` as an expression of `t`
  (cubic ease-out), `fontsize` as an expression of `t` (pop), stacked reveal
  windows (typewriter), a box with colour alpha, and alpha plus y plus box together.
- `zoompan` with a cosine-eased zoom, and with an eased moving focus.
- `rgbashift`, `vignette` (also animated with `eval=frame`), `noise` (temporal
  grain), `eq`, `curves` (preset), `hue`, `boxblur`, `tblend`, `colorbalance`, and an
  `eq` brightness pulse (flash cut).
- Audio: `anoisesrc` with a fixed `seed`, `aevalsrc` (decaying click, chirp
  riser), `adelay`, `afade` (computed start time and named curves), `loudnorm`
  single pass and measure pass (JSON), `maskedmerge`, and `xfade` with `custom`.

Limits found, which shape the design:

- `gblur` rejects an expression for `sigma` (only a constant). Animated blur is
  not available; use an `enable` window or `boxblur` with `enable`.
- `drawtext` has no `text_align` in 5.1. We already draw each wrapped line as its
  own centred `drawtext`; animations must keep doing that.
- `amix` has no `normalize` option in 5.1. We already work around it (amix then
  `volume=N`).
- `drawtext` `fontsize`, `x`, `y` and `alpha` accept expressions of `t`, but the
  wrapped line layout is computed once at the base size, so a size animation (pop)
  must stay small (about 10%).

### 0.3 Python packages in the image (for section 8)

`onnxruntime` 1.31.0 (CPU provider only), `numpy` 2.4.6, `Pillow` 11.3.0 are
installed. `opencv`, `torch`, `tensorflow`, `mediapipe` and `rembg` are not.

## 1. Shared design rules

- **Where the code goes.** Per-scene picture effects go into the existing
  per-scene filter chain in `scene_post._scene_chain`, which is used by both the
  scene body and the junction (transition) encodes. Audio effects go into the
  per-scene audio segments and the final mix. Clip-level changes (camera) go into
  the image-clip render in `scene_pipeline`.
- **Cache keys.** A stage's key must contain every input that changes its output.
  Picture effects add a normalised `effects` description next to the existing
  `lines_key` in the body and junction keys; the clip key gains the camera. Bump
  `SEGMENT_VERSION` (`seg-v1` to `seg-v2`) and the clip key version (`aclip-v1`
  to `aclip-v2`) in the same change, so stale artifacts are never reused.
- **Settings-level defaults.** Like captions, looks and audio polish accept a
  default in `settings` that a scene can override field by field.
- **Validation.** Same style as today: every field is validated, unknown fields
  are rejected, all errors are reported together, and anything that depends on the
  real narration length is checked after narration is known and before any
  rendering.
- **Tests.** Same style as today: pure-Python tests for the expression builders
  and validators; container ffmpeg tests with synthetic assets for picture and
  level checks.

## 2. a. All additional xfade transition types

### Manifest

No new field. `transition.type` accepts the 38 extra names listed in 0.1. The
existing rules stay: duration 0.1 to 3 s and at least 2 frames; the last scene
cannot have a transition.

### ffmpeg approach

Extend the `TRANSITION_TYPES` map in `scene_post` from manifest name to xfade name.
The junction graph already passes `transition=<name>` to `xfade`; nothing else
changes. Narration still crossfades with equal power for every type.

### Cache keys and stages

None new. The junction key already includes the xfade name, so changing a type
rebuilds only that junction and the later joins.

### Validation

`type` must be in the table. A one-line note in the error lists the valid names.

### Tests

The existing parametrized test (`test_every_transition_type_keeps_length_and_sync`)
already iterates `TRANSITION_TYPES`; it covers every new name for length and sync
with no change. Add one test that every name maps to an xfade name that appears in
`ffmpeg -h filter=xfade` (guards against a typo in the table).

### Risks

- Some types look wrong on text-heavy graphics (`distance`, `radial`, `pixelize`,
  `squeezeh`). Mitigation: document a short "good defaults" list and keep the full
  list available.
- Several are directional (left/right/up/down); the names follow ffmpeg's, and the
  direction was never checked by eye for the 8 we have beyond the Phase 3 frames.
  Add a frame-sample check per type before documenting directions.
- 2-frame transitions on shape/slice types may look like a glitch; the 2-frame
  minimum stays.

### 2.5 `custom` (not proposed)

`xfade=transition=custom:expr=...` works in 5.1. Letting manifests carry free
expressions would need an expression validator and sandboxing of the text. Skip;
if wanted later, offer a small fixed set of named expression presets instead.

## 3. b. Combined zoom and pan with easing

### Manifest

A `camera` object per scene (and per `assets[]` entry), mutually exclusive with
`motion`; the existing `motion` presets stay as shorthand and expand to a camera
internally.

```json
"camera": {
  "from": {"zoom": 1.0, "x": 0.5, "y": 0.5},
  "to":   {"zoom": 1.25, "x": 0.35, "y": 0.4},
  "ease": "ease_in_out"
}
```

- `zoom`: scale factor, 1.0 to 3.0 (1.0 shows the whole picture).
- `x`, `y`: where the centre of the visible window is, as fractions of the picture
  (0 to 1). The window is clamped so it never leaves the picture.
- `ease`: `linear`, `ease_in`, `ease_out`, `ease_in_out` (default `ease_in_out`).
- `from` omitted means the picture centred at zoom 1.0; `to` is required.

### ffmpeg approach

`zoompan` only (the `scale_crop` engine would be left for the legacy presets).
With progress `p = on/frames`:

- `linear`: `p`
- `ease_in`: `p*p`
- `ease_out`: `1-(1-p)*(1-p)`
- `ease_in_out`: `0.5-0.5*cos(PI*p)` (the cosine form was smoke-tested)

Then `z = z0 + (z1-z0)*e`, focus `fx = x0 + (x1-x0)*e`, and
`x = max(0, min(iw-iw/zoom, fx*iw - iw/zoom/2))` (same for y). The 2x oversample
already used keeps the window moves sub-pixel.

### Cache keys and stages

The image-clip key (`aclip-v2`) gains the full camera (after defaults are filled
in and the easing is named). No new stage. For multi-asset scenes each asset has
its own camera (default: the scene's).

### Validation

Ranges above; `camera` and `motion` together is an error; `to` required; a camera
on a video asset is rejected (video assets are not zoomed, as today).

### Tests

- Pin the generated expression strings for each ease and for the legacy presets
  (the legacy `zoom_in` / `static` strings must stay byte for byte as they are).
- Measure the start and end scale and focal point with the existing fit helper;
  check the scale at 25%, 50% and 75% against the easing formula (within 0.01).
- Clamping: a window pushed past the edge stays inside the picture.

### Risks

- `zoompan` rounds to whole pixels; large zoom values on a small source look soft
  (zoom 3.0 magnifies a 1080 px picture three times). Document a recommended maximum
  of about 1.5 for 1080-wide graphics.
- Memory: unchanged from today's measured 400 to 550 MB per clip render at
  1080x1920 (dominated by the 2x oversample).
- Existing cached clips are invalidated by the key bump (one re-render).

## 4. c. Animated text cards

### Manifest

```json
"text_cards": [{
  "text": "SOURCE A\nSaid one thing happened",
  "start": 0.3, "end": 1.9,
  "animation": {"in": "slide_up", "out": "fade", "duration": 0.3}
}]
```

- `in`: `none` (default), `fade`, `slide_up`, `pop`, `typewriter`.
- `out`: `none` (default) or `fade`.
- `duration`: seconds for each of the in and out animations, 0.05 to 2.0.
- A settings-level `settings.text_card_animation` supplies a default.

### ffmpeg approach

All animations are `drawtext` expressions of `t` on the existing per-line
`drawtext`, so no new filters and no new stages:

- `fade`: `alpha='clip((t-start)/d,0,1)*clip((end-t)/d,0,1)'` (the out part only if
  `out` is `fade`). The box is faded with the text by also using the alpha option;
  confirm in a frame test that the box fades with the text, since 5.1 applies
  `alpha` to the text colour and the box colour alpha is set separately.
- `slide_up`: `y='base + 60*pow(1-clip((t-start)/d,0,1),3)'` together with the
  fade (cubic ease-out, tested).
- `pop`: `fontsize='size*(1 + 0.12*sin(PI*clip((t-start)/d,0,1)))'` with the fade;
  limited to about 12% so wrapped lines do not collide.
- `typewriter`: the card's lines are pre-wrapped as today; the characters are
  revealed over `duration` by stacking one `drawtext` per reveal step (the longer
  substring replaces the shorter via `enable` windows). Limited to cards of at most
  80 characters.

### Cache keys and stages

The animation becomes part of each `DrawLine` and therefore of `overlay_key`, which
is already in the body and junction keys. Changing one card's animation rebuilds that
scene's segments, its junctions and the later joins, exactly like a text change.

### Validation

Animation names from the lists; `duration` range; `in + out durations` must fit
inside `end - start`; typewriter text at most 80 characters; the card's `start` and
`end` rules are unchanged.

### Tests

- Pin the expression strings per animation.
- Frame checks at t = start (nothing or small), mid-animation, and after: text
  presence by comparing against a no-card frame, as the existing card test does.
- Typewriter: count of visible characters at three timestamps.
- Cards past the end of the scene are still rejected before rendering.

### Risks

- `pop` changes line height slightly; kept small for that reason.
- `typewriter` multiplies `drawtext` instances (about one per character), which
  slows filter start-up; the 80-character cap bounds it.
- The baked headlines on the graphics make any card or caption collide; this is a
  layout problem the animation does not fix.

## 5. d. Synthesized sound effects

### Manifest

```json
"transition": {"type": "slide_left", "duration": 0.4,
               "sfx": {"type": "whoosh", "volume": 0.35, "offset": -0.1}}
"text_cards": [{"text": "...", "start": 0.3, "end": 1.9,
                "sfx": {"type": "click", "volume": 0.3}}]
```

- `type`: `whoosh`, `click`, `riser`.
- `volume`: 0 to 1 (linear), default from `settings.sfx.volume` (default 0.35).
- `offset`: seconds relative to the event (transition start, card start), -1 to 1,
  default 0.
- A top-level `sfx` block can switch the whole feature on or off.

### ffmpeg approach

Each sound is generated once from lavfi sources (no files, no downloads):

- `whoosh`: `anoisesrc` (pink, fixed `seed` so the output is deterministic) through
  `highpass`/`lowpass` and two `afade` ramps (up then down), about 0.5 s.
- `click`: `aevalsrc` decaying sine burst, about 50 ms.
- `riser`: `aevalsrc` chirp with a rising envelope, about 1.2 s.

Placement: events live on the final timeline, so each narration segment (scene body
or junction) gets a parallel SFX segment of the same sample length: silence from
`anullsrc` plus the events that fall inside it, positioned with `adelay` and summed
with `amix` plus a `volume` restore (the same workaround already used for music).
The segments are joined by stream copy like the narration segments, giving a single
`sfx` track the same length as the narration master. The final mix adds that track
(not ducked, so the transition sound stays audible).

### Cache keys and stages

New stages: `sfx:<type>:<params>` (the synthesized sound, keyed by type, duration,
seed and a version), one `sfx-seg` per scene or junction that has events (keyed by
the event list and the sound keys), one shared silence segment per distinct length,
and `join:sfx`. Segments without events reuse the silence segment, so changing one
card's sound rebuilds one segment, the join and the final mix.

### Validation

Type names, volume range, offset range, and a check once durations are known that
every event lands inside the video (an event placed before 0 or after the end is an
error). A cap of 100 events per project.

### Tests

- Each synthesized sound has the expected length and is not silent (volumedetect).
- Placement: the level in a 50 ms window at the expected time is above the level just
  before it; total length unchanged.
- Volume: doubling `volume` raises the window level by about 6 dB.
- One changed event rebuilds only its segment, the join and the final mix.

### Risks

- Sound quality is subjective; synthesized whooshes can sound thin. Plan an
  audition step (render three samples) before committing to defaults.
- Overlapping loud sounds can clip; the final limiter covers it, but the mix needs a
  level test with events plus narration plus music.
- This needs the audio mix to be a separate stage (see section 7), so it should come
  after audio polish.

## 6. e. Grain, vignette, colour grade and flash cut

### Manifest

```json
"look": {
  "vignette": {"amount": 0.35},
  "grade": {"preset": "warm", "contrast": 1.05, "saturation": 1.1, "brightness": 0.0},
  "grain": {"amount": 0.2}
}
"transition": {"type": "crossfade", "duration": 0.4,
               "flash": {"duration": 0.08, "strength": 0.8, "color": "#FFFFFF"}}
```

- `look` goes in `settings.look` (default) and in a scene (override).
- `vignette.amount` 0 to 1; `grade` accepts a preset (`warm`, `cool`, `faded`,
  `high_contrast`) and optional `contrast`, `saturation`, `brightness`; `grain.amount`
  0 to 1.
- `flash` belongs to a transition: a brief brightening at the middle of the
  transition; `duration` 0.03 to 0.3 s, `strength` 0 to 1, optional colour.

### ffmpeg approach

Added to the per-scene chain, so the same chain runs in the body and in both sides
of a junction and the look is identical across a transition:

- grade: `eq` (contrast, saturation, brightness) then `curves` preset or
  `colorbalance` for temperature;
- vignette: `vignette=angle=...` (angle from `amount`);
- grain: `noise=alls=<amount*N>:allf=t` last (so it sits over the text and unifies
  the frame), optional;
- order: grade, vignette, then the text overlays, then grain.
- flash: an `eq` brightness pulse on the output of the xfade in the junction graph,
  with a triangular envelope around the junction midpoint (`eval=frame`).

### Cache keys and stages

A normalised `look` description joins the body and junction keys next to `lines_key`;
the flash joins the junction key only. No new stages.

### Validation

Ranges above, preset names, colour as `#RRGGBB`, and flash length shorter than the
transition.

### Tests

- Vignette: corner luma lower than centre luma on a flat grey frame.
- Grain: pixel variance on a flat frame rises with the amount.
- Grade: mean R/G/B balance shifts in the expected direction.
- Flash: the brightness spike appears only in the junction window.
- Timeline length is unchanged by any look.

### Risks

- **Grain is expensive.** Noise is nearly incompressible at crf 18, so file size can
  grow a lot (estimate: 30% to 100%) and the encode slows (estimate: 20% to 35%).
  Keep grain optional, low by default, and measure on the Phase 3 project first.
- Vignette on 8-bit video can show banding in dark gradients.
- Colour grading shifts levels in limited-range yuv420p; keep the effects mild and
  check the histogram on a test frame.
- Grain restarts at each scene (the noise is random per frame); this is invisible.

## 7. f. Audio polish

`bgm.volume` and `settings.narration_volume` already exist and are unchanged. Only
the following is new.

### Manifest

```json
"bgm": {"file": "music.mp3", "volume": 0.25, "ducking": {},
        "fade_in": 1.0, "fade_out": 1.5}
"scenes": [{"id": "s2", "volume_db": -2.0, "...": "..."}]
"settings": {"loudness": {"target_lufs": -14, "true_peak_db": -1.0,
                           "mode": "two_pass"}}
```

- `bgm.fade_in`, `bgm.fade_out`: seconds, 0 to 10, default 0 (today's behaviour).
- `scenes[].volume_db`: gain for that scene's narration, -30 to +12 dB, default 0.
- `settings.loudness`: optional. `target_lufs` -24 to -9 (default -14),
  `true_peak_db` -6 to 0 (default -1.0), `mode` `single_pass` or `two_pass`
  (default `two_pass`). Omitted means no loudness processing, as today.

### ffmpeg approach

- Fades: `afade=t=in:d=<fade_in>` and `afade=t=out:st=<T - fade_out>:d=<fade_out>` on
  the music stem (before ducking), where T is the video length known from the frame
  count. A curve such as `qsin` or `tri` can be chosen later; default `tri`.
- Per-scene gain: `volume=<10^(db/20)>` in that scene's narration segment (body and
  junction). It is applied in the post stage, separate from `narration_volume`
  (which is applied earlier and rebuilds the renderer stages).
- Loudness: split today's single final-mux command into stages:
  1. `mix`: narration + (sfx) + faded, ducked music, with the limiter, written as a
     WAV;
  2. `loudness`: `two_pass` runs `loudnorm=...:print_format=json` on the mix to
     measure, then applies `loudnorm` with the measured values and `linear=true`;
     `single_pass` applies `loudnorm` once (dynamic, can pump);
  3. `final`: mux the picture (copy) with the result, encode AAC.
  A peak limit (`alimiter`) follows loudnorm as a safety net.

### Cache keys and stages

New stages `mix`, `measure` and `loudness` between `join:narration` and `final`.
Keys: `mix` from the join keys plus the music file hash and music settings (fades
included); `measure` from the mix key; `loudness` from the mix key and the target
values; `final` from the picture join key and the loudness key. A per-scene gain
changes only that scene's narration segments (and its junctions), then the joins,
mix and later stages. A change to fades or loudness never rebuilds a picture
segment or a narration segment.

### Validation

Ranges above; `fade_in + fade_out` must not exceed the video length (checked when
the length is known); `loudness` without `bgm` is allowed (it still normalises the
narration); `mode` must be one of the two names.

### Tests

- Fade: level in the first 0.2 s is below mid-video; same at the end for fade-out;
  zero fades reproduce today's output (byte-identical mix stage key for old
  manifests is not required, but levels must match).
- Gain: the level of the scene's window changes by the requested dB (within 1 dB).
- Loudness: measure the result with `loudnorm print_format=json`; integrated loudness
  within 1 LU of the target and true peak below the limit.
- Cache: changing only `target_lufs` rebuilds `loudness` and `final` only.

### Risks

- `loudnorm` single pass is unreliable on very short clips (it works on windows of
  about 3 s); two-pass linear mode is the safe default for Shorts of 15 to 60 s.
- Re-encoding to AAC after limiting can overshoot the peak slightly; aim the limit
  at -1.0 to -1.5 dBTP.
- Platforms normalise loudness themselves (about -14 LUFS for YouTube); a very
  quiet video is not boosted, so the target matters mostly for quiet material.
- Splitting `_final` into stages changes existing code, so it must come with the
  cache-key bump and the full Phase 3 test pass.
- The measured problem this addresses: in the Phase 3 test the music sat about 28 dB
  under the narration after ducking, and it stopped abruptly at the end.

## 8. Text behind a subject for still images (separate section)

### Idea

For a photo, draw a text card behind the person: text appears over the background
but the person stays in front of it. This needs one subject mask per photo.

### Segmentation approach and models

- Run an ONNX segmentation model with the `onnxruntime` already in the image, plus
  `numpy` and `Pillow`. No new Python package is needed.
- Candidate models (sizes are from memory, not verified; check before relying on
  them): U2-Net small (about 5 MB, rough edges), Silueta (about 45 MB), U2-Net
  (about 170 MB), IS-Net general (about 170 MB, better edges), MODNet (about 25 MB,
  portraits only), BiRefNet (best edges, but hundreds of MB and too heavy for the
  2 GB container).
- Suggested starting point: IS-Net general-use or U2-Net. Licences are Apache-2.0 or
  MIT for these families as far as I remember; confirm per model before use.
- One-time download of a single model file, checksum pinned. It needs a short
  network step and an exception amendment (a new read-only `/models` mount). Not
  downloaded here.

### Pipeline

1. A `cutout` stage per photo: input the photo, output an 8-bit mask image (white =
   subject), cached under `.scene_cache/cutouts/<key>` where the key contains the
   photo hash, the model hash and the parameters (input size, threshold, edge
   feather).
2. A mask clip: render the mask with the same camera as the picture clip (same
   frames, same zoompan expression) into a small grey H.264 clip, cached next to the
   picture clip.
3. In the per-scene chain: split the scene video, draw the card on one copy, and
   combine with `maskedmerge` so subject pixels come from the clean copy and the rest
   from the text copy. `maskedmerge` was smoke-tested in the container.
   Manifest: `text_cards[].behind_subject: true`, valid only on image scenes.

### Can it run in the hardened container?

Yes in principle: onnxruntime (CPU) is installed; the model file would be mounted
read-only; inference runs with the network off. Memory: an IS-Net or U2-Net run at
1024x1024 is likely to use about 1 to 1.5 GB (estimate), so it must run alone, not
alongside ffmpeg, inside the 2 GB limit. Time: a few seconds per photo on CPU
(estimate). Both numbers must be measured before committing.

### Risks

- Edge quality on hair, thin objects (microphone stands) and busy backgrounds.
- The current graphics have the headline baked into the image, so this only works on
  raw photos without printed text.
- Model licence and download provenance need a decision first.
- A new mount and a download step both need approval and an exception edit.

Recommendation: treat this as a Tier 2 spike on three photos, not part of Tier 1.

## 9. What to build first, what to skip, and cost

### Order

1. **a. transitions (all types).** About one mapping table and documentation; no new
   stages; negligible risk.
2. **f. audio polish.** Fixes two measured problems (music inaudible and cut off) and
   gives a loudness target. Needs the final mux split into `mix`, `loudness` and
   `final`; moderate change, low risk.
3. **e. look, without grain at first** (vignette, grade, flash), then grain behind a
   file-size check.
4. **c. text-card animation: `fade` and `slide_up` first**, then `pop`; typewriter last.
5. **b. camera with easing.** Larger change to the clip render and key; do after the
   cheap items.
6. **d. synthesized SFX.** Depends on the `mix` stage from (f) and needs an audition
   step; do last.

### Skip for now

- `xfade` `custom` expressions (validation and security cost).
- Animated `gblur` (not possible in 5.1).
- Typewriter on long cards (instance count).
- Text behind a subject (separate spike, section 8).

### Estimated effect on memory and render time

These are estimates, not measurements. Baseline measurements: per-process peak 400 to
550 MB at 1080x1920 (the zoompan clip render dominates); the Phase 3 test (5 scenes,
16.3 s) built in 41 s; the 100-scene realistic test (282 s of video) built in 222 s,
about 0.8 s of build per output second.

| Item | Memory | Render time | Output size |
|---|---|---|---|
| a. transitions | unchanged | unchanged (a junction is 2 to 90 frames) | unchanged |
| b. camera easing | unchanged | unchanged (same zoompan) | unchanged |
| c. text animation | unchanged | +0 to 3%; typewriter scenes +5 to 10% | unchanged |
| d. SFX | +small (audio only) | about +1 s per project | unchanged |
| e. vignette, grade, flash | +30 to 80 MB | +5 to 10% of scene encode | unchanged |
| e. grain | +30 to 80 MB | +20 to 35% of scene encode | +30 to 100% |
| f. audio polish | unchanged (audio only) | two-pass loudness about +1 to 2 s per minute of audio | unchanged |
| Section 8 (spike) | +1 to 1.5 GB for the segmentation step only | a few seconds per photo | unchanged |

Plan to measure the items that are built on the existing `test-phase3` project and
report real numbers before the next item.

## 10. Decisions

Recorded from the owner's answers. Nothing is built yet; the build is approved
separately.

1. **Transitions (item a).** Expose all 38 added transition names, so all 46 xfade
   transitions are available. The documentation recommends a short default set:
   `cut`, `crossfade`, `fade_black`, `slide_left` / `slide_right` and
   `wipe_left` / `wipe_right`.
2. **Camera (item b).** The `motion` presets stay as shorthand for `camera`. A scene
   uses either `motion` or `camera`, never both.
3. **Loudness (item f).** Target -14 LUFS integrated with a -1 dBTP true-peak limit,
   using the two-pass method for final renders. Not yet settled: whether the target
   applies to every build by default or only when it is set in the manifest; decide
   this when item f is built.
4. **Look (item e).** Flash is in scope (vignette, grade and flash). Grain is
   deferred and will not be built in this tier.
5. **Text behind a subject (section 8).** A later spike, not part of Tier 1.

**Build order, approved in principle:**

1. transitions (a)
2. audio polish (f)
3. vignette, grade and flash (e, without grain)
4. text animation (c)
5. camera easing (b)
6. sound effects (d)

Building has not started. Each step needs its own approval before work begins.
