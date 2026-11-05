# Scene pipeline

Build a video from a script split into scenes. Each scene has its own narration
text and its own image or video. The pipeline makes the narration for each scene,
measures its real length, makes the visual last exactly that long, and renders
through the local video project renderer (`app/services/video_project.py`), so
each scene is cached and can be re-rendered on its own. Captions, text cards,
transitions, pauses and background music are added afterwards as further cached
stages (`app/services/scene_post.py`).

## Project folder

```text
my-episode/
  scenes.json      the scene manifest (filenames only)
  assets/          images and videos
  audio/           pre-made narration (for example from Fish Audio) and music
  .scene_cache/    narration, clips and post stages (safe to delete)
  render/          renderer store: revisions and cached scene videos
  out/             the finished video, out/<project_id>.mp4 (or out/<name>.mp4 with --output-name)
```

## Commands

```text
python -m app.services.scene_pipeline --project my-episode validate
python -m app.services.scene_pipeline --project my-episode plan
python -m app.services.scene_pipeline --project my-episode plan --text
python -m app.services.scene_pipeline --project my-episode build
python -m app.services.scene_pipeline --project my-episode build --output-name take2 [--overwrite]
python -m app.services.scene_pipeline --project my-episode status
python -m app.services.scene_pipeline --project my-episode needs-network
```

`validate` checks the manifest and probes every asset and the music file.
`plan` is read-only and checks timing before you spend time rendering. For each scene
it shows the narration length (after silence trimming), the scene length in frames and
seconds, where it starts on the timeline, its transition, each asset's fit, motion,
start, end and frames, each text card's window and how many seconds it ends before the
scene does, and the caption cue count. It lists problems (cards that end after their
scene, cards at the same position that overlap in time, asset durations that do not add
up to the narration, transitions longer than their scenes, text that cannot fit) and
warnings (a transition that overlaps speech), and exits with 1 if there are problems.
The default output is JSON, one entry per scene; `--text` prints a readable report.
Scenes whose narration is not generated yet show as pending, and everything that
depends on their length is checked at build time instead.

`plan --summary` prints the project-level summary as JSON (totals, music pick, loudness).

`build --output-name NAME` writes `out/NAME.mp4` instead of `out/<project_id>.mp4`, so a
new build does not replace an earlier file. NAME is 1 to 64 letters, digits, `_` or `-`
(no extension, no path). It refuses to replace an existing `out/NAME.mp4` unless
`--overwrite` is given, and checks this before any narration is generated. Without
`--output-name` the build replaces `out/<project_id>.mp4` as before.

`needs-network` prints `yes` only if a scene still needs narration generated
(Edge TTS needs the network); with pre-made audio or cached narration it prints
`no`. Every problem in a manifest is reported together, and misuse is rejected
before any rendering starts.

## Manifest schema (version 1)

Every field is in use except `full_narration_audio`, which is reserved: setting it
to anything other than null is an error.

### Top level

| Field | Status | Meaning |
|---|---|---|
| `schema_version` | used | Must be `1` |
| `project_id` | used | Letters, digits, `_`, `-`; names the output file |
| `voice.name` | used | Edge TTS voice, required if any scene has no audio file |
| `voice.rate` | used | 0.5 to 2.0, default 1.0 |
| `settings` | used | See below |
| `bgm` | used | Background music, see below |
| `scenes` | used | 1 to 1000 scenes |
| `full_narration_audio` | reserved | One narration file in `audio/`, to be split into scenes by alignment |

### `settings`

| Field | Status | Meaning |
|---|---|---|
| `width`, `height`, `fps` | used | Defaults 1080, 1920, 30 (even sizes) |
| `narration_volume` | used | Default 1.0 |
| `trim_silence` | used | Trim trailing silence from every scene's audio; default `true` |
| `trim_padding` | used | Seconds of silence kept after the last sound, 0 to 2, default 0.15 |
| `captions` | used | Default caption settings for every scene: `{enabled, mode, style}` |
| `text_card_style` | used | Default style for every text card |
| `loudness` | used | `{target_lufs, true_peak_db}`: normalise the final audio; off unless set. Defaults -14 LUFS and -1.5 dBTP (target -50 to -5, peak -9 to 0) |
| `text_card_animation` | used | Default `{in, out, duration, stagger}` for every text card (see Card animation). Off unless set |
| `sfx` | used | `{enabled, volume}`: the master switch (default on) and the default volume (0.35) for sound effects (see Sound effects) |
| `look` | used | Default look for every scene: `{vignette, grade}` (see Look). Off unless set |

### `bgm`

| Field | Status | Meaning |
|---|---|---|
| `file` | used | Filename in `audio/`. Set this or `folder`, not both |
| `folder` | used | Plain name of a folder inside `audio/`; one file is picked from it (see Music) |
| `seed` | used | Whole number from 0; chooses the file from `folder`. Default: a stable hash of `project_id`, so each project picks differently. Not allowed with `file` |
| `volume` | used | Above 0, up to 1; default 0.25 |
| `fade_in`, `fade_out` | used | Seconds, 0 to 10, default 0 (no fade). The music fades in from silence at the start and fades out so it is silent at the last frame. Their sum must not exceed the video length (checked once the length is known, before rendering) |
| `ducking` | used | Omit for constant volume. `{}` turns ducking on with defaults |
| `ducking.amount` | used | dB the music is lowered while narration plays, 0 to 18, default 12 |
| `ducking.attack` | used | Milliseconds to duck once narration starts, 1 to 2000, default 150 |
| `ducking.release` | used | Milliseconds to recover after narration stops, 10 to 9000, default 800 |

### `scenes[]`

| Field | Status | Meaning |
|---|---|---|
| `id` | used | Unique, stable; keep it when replacing media |
| `narration` | used | Text to speak; required unless `audio` is set. Also the caption text |
| `audio` | used | Optional filename in `audio/`; skips TTS |
| `asset` | used | Filename in `assets/` (image or video). Use this or `assets` |
| `assets` | used | List of `{file, start, duration, motion, fit, camera}`; replaces `asset` |
| `motion` | used | `zoom_in` (default), `zoom_out`, `pan_left`, `pan_right`, `pan_up`, `pan_down`, `static`; images only |
| `camera` | used | An eased zoom and pan for an image, instead of `motion` (see Camera). Set `motion` or `camera`, never both |
| `fit` | used | `cover` (default) fills the frame and crops; `contain_blur` shows the whole picture over a blurred copy of itself |
| `pause_after` | used | Seconds of silence after the narration, 0 to 30 |
| `volume_db` | used | Gain for this scene's narration in dB, -30 to 12, default 0. Applied in the post stages, separately from `settings.narration_volume`; only that scene's narration segments (and its junctions) rebuild |
| `captions` | used | `{enabled, mode, style}`; overrides `settings.captions` field by field |
| `text_cards` | used | List of `{text, style, start, end, kind, label, value, source, animation, sfx}`, up to 20 (see Text card kinds and Card animation) |
| `transition` | used | `{type, duration, flash, sfx}`; joins this scene to the next. `flash` (see Look) and `sfx` (see Sound effects) are optional |
| `look` | used | `{vignette, grade}` for this scene; overrides `settings.look` field by field |

`assets[]` entries: `file` (filename in `assets/`), `start` (seconds into a video
file where playing begins, default 0; not for images), `duration` (seconds on the
scene timeline, optional), `motion` (optional, overrides the scene's `motion`). `fit` (optional, `cover` or
`contain_blur`; overrides the scene's `fit` for this entry only, so a portrait photo and a
background plate can sit in one scene with different fits).

### Style objects

Used by `captions.style`, `text_cards[].style`, `settings.captions.style` and
`settings.text_card_style`. Every key is optional.

| Key | Meaning |
|---|---|
| `font` | A font file in `resource/fonts` (for example `BeVietnamPro-Bold.ttf`) |
| `size` | Pixels, 8 to 400. Default is 5.5% of the shorter side for captions, 6% for cards. Text that does not fit is shrunk |
| `color`, `outline_color`, `box_color` | `#RRGGBB` |
| `outline_width` | Pixels, 0 to 20; default about size / 14 |
| `position` | `top`, `center` or `bottom`. Default `bottom` for captions, `center` for cards |
| `box`, `box_opacity` | A translucent box behind the text. Off for captions, on for cards (opacity 0.55) |
| `align` | Text cards only. `center` (default) or `left`. Left text starts at the left safe margin (or at the panel's padding) |
| `panel`, `panel_color`, `panel_opacity` | Text cards only. One rectangle behind the whole card instead of a box per line. Off for plain cards, on for claim cards; colour default `#000000`, opacity 0.72 |
| `accent_color` | Text cards only. `#RRGGBB`, default `#FFD400`. The label colour of claim cards. Set it in `settings.text_card_style` for a project default; a card's own `style.accent_color` overrides it |
| `accent_width`, `accent_side` | Text cards only. A solid bar in `accent_color` on the panel's `left` (default) or `top` edge, 0 to 40 px (default 0 = none; fact and quote cards default to 10). The bar needs the panel: it is only drawn when `panel` is on, so a project-wide `accent_width` never puts bars on plain cards |

Defaults come from the code; a scene's style overrides the settings default,
which overrides the code default.

Captions reject the card-only keys (`align`, `panel`, `panel_color`, `panel_opacity`,
`accent_color`).

## Examples

### Phase 1: one picture per scene, narration generated

```json
{
  "schema_version": 1,
  "project_id": "episode-01",
  "voice": {"name": "en-US-AriaNeural", "rate": 1.0},
  "settings": {"width": 1080, "height": 1920, "fps": 30},
  "scenes": [
    {"id": "intro", "asset": "stage.jpg", "narration": "Tonight, a surprise."},
    {"id": "clip", "asset": "crowd.mp4", "audio": "clip-voice.mp3", "narration": "The crowd reacts."}
  ]
}
```

### Phase 2: captions and text cards

```json
{
  "schema_version": 1,
  "project_id": "episode-02",
  "voice": {"name": "en-US-AriaNeural"},
  "settings": {
    "captions": {"enabled": true, "mode": "chunks",
                 "style": {"font": "BeVietnamPro-Bold.ttf", "color": "#FFFFFF", "position": "bottom"}},
    "text_card_style": {"color": "#FFEE88"}
  },
  "scenes": [
    {"id": "intro", "asset": "stage.jpg", "narration": "Tonight, a surprise from the tour.",
     "fit": "contain_blur",
     "text_cards": [{"text": "“We are not done yet.”\n- Ed Sheeran, 2026", "start": 0.5, "end": 3.0}]},
    {"id": "quiet", "asset": "crowd.jpg", "audio": "quiet.mp3", "captions": {"enabled": false}}
  ]
}
```

### Phase 3: several visuals, transitions and music

```json
{
  "schema_version": 1,
  "project_id": "episode-03",
  "voice": {"name": "en-US-AriaNeural"},
  "bgm": {"file": "theme.mp3", "volume": 0.3, "ducking": {"amount": 12, "attack": 150, "release": 800}},
  "scenes": [
    {"id": "intro", "narration": "Three moments, one night.",
     "assets": [{"file": "wide.jpg", "duration": 1.5},
                {"file": "close.jpg", "motion": "pan_right"},
                {"file": "crowd.mp4", "start": 0}],
     "pause_after": 0.5,
     "transition": {"type": "crossfade", "duration": 0.5}},
    {"id": "outro", "asset": "stadium.jpg", "narration": "See you next week.", "motion": "zoom_out"}
  ]
}
```

## Behaviour

### Narration and length

- A scene with `audio` uses that file. Otherwise narration is generated with the
  existing TTS code and kept in `.scene_cache/tts/`, keyed by voice, rate and
  text, so unchanged scenes are never re-synthesized and stay cached downstream.
- The scene length is the real duration of the scene's audio, measured with
  `ffprobe` after trimming. Fractional seconds are supported.
- Trailing silence (below -50 dB) is cut from both generated and pre-made audio,
  keeping `trim_padding` seconds. The trimmed copy is cached in
  `.scene_cache/audio/`. Scenes longer than 600 seconds cannot be trimmed;
  split them or turn trimming off.
- Missing, unreadable or unsupported assets fail validation before any narration
  is generated. Nothing is substituted.

### Pictures

- Images become a clip rendered with direct ffmpeg. Videos are looped or trimmed
  to the audio length by the renderer; their own sound is replaced by the
  narration. `motion` applies to images only. A pan holds a fixed zoom of at
  least 10% so there is room to move.
- `contain_blur` fits the whole picture inside the frame and fills the rest with
  a blurred, enlarged copy of the same picture. Motion moves the contained
  picture inside its own box.
- Several visuals (`assets`) play in order inside the scene, joined by hard cuts.
  Their time on the timeline is decided like this:
  - an asset with a `duration` keeps it;
  - assets without one share the time that is left equally;
  - if every asset has a duration, the total must match the scene's audio
    length within 0.25 s, and the last asset absorbs the small difference;
  - a larger mismatch, or no time left for assets without a duration, rejects
    the scene before any rendering, with the numbers in the message;
  - a video with a `start` above 0 does not loop and must have enough footage
    after its start; a video with `start` 0 loops if it is too short.

### Captions and text cards

- Captions are timed from the narration text and the scene's real (trimmed)
  audio length. No speech recognition or model download is involved. The text is
  split into short cues and each cue gets a share of the time proportional to its
  length, with extra time after sentence and clause ends. The first cue starts
  at 0, the last ends exactly where the audio ends, and `pause_after` time has no
  captions. Pause tags such as `[pause: 1s]` are not shown.
- `mode`: `chunks` (default, up to 3 words per caption), `words` (one word) or
  `lines` (up to 7 words). A scene may have at most 400 captions.
- Because the timing is proportional, a caption can drift from the spoken word by
  a fraction of a second inside a long cue. Shorter modes drift less.
- Text is wrapped with the real font's measurements to fit the safe area:
  6% margins left and right, 8% at the top, and 14% at the bottom on vertical
  video (8% on horizontal). Text that still does not fit is shrunk, and rejected
  only if it cannot fit at 10 px.
- Text card `start` and `end` are seconds from the start of the scene (including
  any `pause_after`). `end` must not be later than the scene's length; this is
  checked once the narration length is known, before rendering.
- Multi-line cards are drawn as centered lines, one per wrapped line.

### Card animation

Optional and off by default. A card without `animation` (and no `settings.text_card_animation`)
renders exactly as before.

```json
{"kind": "claim", "label": "TMZ", "text": "Fight with another inmate", "start": 0.3, "end": 5.3,
 "animation": {"in": "slide_up", "out": "fade", "duration": 0.3, "stagger": 0.12}}
```

- `in`: `none` (default), `fade`, `slide_up` (the text rises into place, fading in) or `slide_left`
  (it moves in from the left, fading in). `out`: `none` (default) or `fade` before the card ends.
- `duration`: seconds for each of the entrance and the exit, 0.05 to 2 (default 0.3).
- `stagger`: 0 to 0.5 s (default 0). Each part of a card starts that much after the one above it,
  so on a claim card the label lands first and the claim follows; on a fact card the label, the
  value and the caption arrive in order. All lines of one part start together.
- The panel and the accent bar do not animate: they cut in at `start` and out at `end` while the
  text animates over them. A plain card's per-line box fades with its text.
- The entrance, the exit and the stagger must fit in the card's time on screen
  (`delay + in + out` at most `end - start`); this is checked before rendering and by `plan`.
- Slides move by about one font size, ease out (cubic) and are drawn with `drawtext` expressions
  of time; there are no new filters or stages. Captions do not animate.
- Changing an animation rebuilds that scene's picture segments and junctions, like a text change.

### Text card kinds

`kind` is `plain` (default, the cards described above), `claim`, `quote` or `fact`. A plain card renders
exactly as it did before the kinds existed, with the same cache keys, unless you set
`align: "left"` or `panel: true` on it.

A **claim card** is a small label over a larger claim, left aligned, on one panel:

```json
{"kind": "claim", "label": "TMZ", "text": "Fight with another inmate",
 "start": 0.3, "end": 5.3, "style": {"position": "top"}}
```

- `label`: required, one line, 1 to 40 characters, shown in capitals at 55% of the claim
  size in `accent_color`. `text`: the claim, 1 to 140 characters, wrapped and shrunk like any
  card. `label` is rejected on other kinds.
- Claim defaults: `align: left`, `panel: true`, no per-line boxes (these win over
  `settings.text_card_style` for claim cards; the card's own `style` wins over them). The
  panel hugs the text, starts at the left safe margin (centred if `align` is `center`),
  and is padded by half the claim size sideways and 45% above and below.
- The panel and the text always stay inside the safe margins. The size shrinks until the
  whole block fits; a block that cannot fit at 10 px is rejected before rendering.
- Readability over photos: the default panel is 72% black, and the text keeps its dark
  outline. Lower `panel_opacity` only for plain backgrounds.
- The card appears and disappears with a hard cut. `plan` lists the kind and label.

A **fact card** is a small label over a big centred value, with an optional caption:

```json
{"kind": "fact", "label": "Last listed", "value": "Oct 26", "text": "Allenwood Low",
 "start": 10.6, "end": 19.7}
```

- `value`: required, one line, 1 to 14 characters, drawn at 2.2 times the card size and never
  shrunk below 48 px at 1080 wide (scaled with the frame); a value too wide for the frame at
  that floor is rejected before rendering. `label`: optional, one line, up to 40 characters, small
  capitals in `accent_color`. `text`: optional caption, up to 80 characters.
- Defaults: centred, panel on, a 10 px accent bar on top.

A **quote card** is an accent quotation mark, the quote and a dim source line:

```json
{"kind": "quote", "text": "We have no additional information to provide.",
 "source": "Bureau of Prisons", "start": 4.8, "end": 9.5}
```

- `text`: required, up to 200 characters. `source`: optional, one line, up to 60 characters,
  shown after an em dash at 65% size in grey. The opening mark is added for you.
- Defaults: left aligned, panel on, a 10 px accent bar on the left. Set `accent_width: 0` for
  no bar.

Fields belong to their kind: `label` is for claim and fact cards, `value` for fact cards,
`source` for quote cards; anywhere else they are rejected. All kinds use the safe margins and
the panel keeps the text readable over photos. Plain and claim cards are not affected by the
fact, quote and bar options: their output and cache keys are unchanged.

### Transitions and timing

The rule, in frames at the project frame rate:

1. A scene lasts `n = round((audio seconds + pause_after) * fps)` frames. During
   `pause_after` the last picture is held and the narration is silent.
2. A transition on scene A joins A to the next scene B. It lasts
   `t = round(duration * fps)` frames (so 0.25 s at 10 fps is 2 frames: Python
   rounds halves to the even number). `t` must be at least 2 frames, because
   ffmpeg's `xfade` produces no picture from a 1-frame overlap. A shorter
   transition is rejected when the manifest is read, with the minimum for the
   project frame rate: 0.2 s at 10 fps, 0.1 s at 20 fps, about 0.08 s at 24 fps,
   about 0.07 s at 30 fps.
3. B starts `t` frames before A ends. The last `t` frames of A and the first `t`
   frames of B play at the same time, blended by the transition. The total length
   is the sum of all `n` minus the sum of all `t`.
4. Narration follows the same frame counts. Over the overlap, A's narration fades
   out while B's fades in (an equal-power crossfade), for every transition type.
   Picture and sound are cut at the same frame boundaries, so they stay in sync.
5. Each scene must be at least as long as the transitions on both of its sides
   (`n >= t_before + t_after`); otherwise the build is rejected before rendering.
6. The last scene cannot have a transition (there is no next scene).
7. Because the narrations overlap, a transition longer than the quiet at the end
   of A and the start of B would cut across spoken words. The build result lists
   a warning for such a transition. Add `pause_after` at least as long as the
   transition to make the overlap fall in silence.

Types: all 46 of ffmpeg's `xfade` transitions plus `cut` (the default, no overlap).
Duration is 0.1 to 3 s (default 0.5) and at least 2 frames at the project frame rate (see
rule 2). A short default set that works on text-heavy graphics: `cut`, `crossfade`,
`fade_black`, `slide_left`, `slide_right`, `wipe_left`, `wipe_right`.

| Group | Names |
|---|---|
| Fades | `crossfade`, `fade_black`, `fade_white`, `dissolve`, `fade_grays`, `fade_fast`, `fade_slow` |
| Wipes | `wipe_left`, `wipe_right`, `wipe_up`, `wipe_down`, `wipe_tl`, `wipe_tr`, `wipe_bl`, `wipe_br` |
| Slides | `slide_left`, `slide_right`, `slide_up`, `slide_down`, `smooth_left`, `smooth_right`, `smooth_up`, `smooth_down` |
| Shapes | `circle_crop`, `rect_crop`, `circle_open`, `circle_close`, `radial`, `distance` |
| Doors | `vert_open`, `vert_close`, `horz_open`, `horz_close` |
| Diagonals | `diag_tl`, `diag_tr`, `diag_bl`, `diag_br` |
| Slices | `hl_slice`, `hr_slice`, `vu_slice`, `vd_slice` |
| Other | `pixelize`, `hblur`, `squeeze_h`, `squeeze_v`, `zoom_in` |

Some types (`distance`, `radial`, `pixelize`, `squeeze_h`) look wrong on text-heavy
graphics. Narration still crossfades with equal power for every type.

### Music

- The music file comes from `audio/`. It is looped or trimmed to the video length
  and mixed under the narration at `volume`. It ends together with the video; no
  fade is added.
- `bgm.folder` picks one track instead of naming it. The candidates are the audio files
  directly inside `audio/<folder>/`, sorted by name (hidden files and other extensions are
  ignored; no sub-folders). When `seed` is not given it is derived from `project_id` (SHA-256 of the id, the same on every
  machine); an explicit `seed` overrides that. The seed selects one by a SHA-256 hash of the seed and the file
  count, so the same seed, names and count give the same file on any machine. Nothing
  outside that folder can be chosen: the folder must be a plain name (no path), the folder
  and its files must not be symbolic links, and an empty folder is an error.
- The pick is recorded in the `plan` report (`plan --text` shows it, `plan --summary` gives
  JSON), in the build result under `music`, and in `out/<name>.music.json` with the folder,
  seed, `seed_source` (`manifest` or `project_id`), candidate count, chosen filename and path. Rebuilding with the same seed and the same
  folder contents picks the same file and reuses every stage. Adding or removing files
  changes the count and can change the pick; to keep a track, name it with `bgm.file`.
- Ducking lowers the music while narration plays, using a sidechain compressor
  keyed by the narration. `amount` is the approximate reduction in dB for
  narration at about -20 dBFS (typical for generated speech); louder narration
  ducks a little more. `attack` and `release` set how fast it ducks and recovers.
- The mix is limited at -0.45 dBFS to avoid clipping.
- `fade_in` and `fade_out` shape the music stem before ducking (`afade`, linear). A manifest
  without fades produces exactly the earlier mix; changing a fade rebuilds only the final mix.
- `scenes[].volume_db` raises or lowers one scene's narration (for example a quiet take or a
  loud one). A scene with a transition applies its gain on its own side of the narration
  crossfade. Loudness normalisation (`settings.loudness`) still runs on the finished mix.

### Loudness

- Off unless `settings.loudness` is set. The normal output `out/<name>.mp4` is written as
  before; the normalised copy is written next to it as `out/<name>.loudnorm.mp4`, with the
  report in `out/<name>.loudnorm.json`. The video stream is copied, not re-encoded.
- `true_peak_db` means the written file's true peak should not exceed it. loudnorm is given a
  target 0.3 dB lower (for -1.5 it gets -1.8) to absorb the overshoot the AAC encode adds. loudnorm cannot go below -9, so a limit
  between -9 and -8.7 gets less than 0.3 dB of headroom.
- Two passes of ffmpeg `loudnorm` (LRA target 11) on the final mixed audio: the first
  measures it, the second applies a constant gain (`linear=true`) and encodes AAC 192k
  at 48 kHz. The result is measured again from the written file.
- The build result and the `.loudnorm.json` file report: `mode`, `target_lufs`,
  `true_peak_db`, `input` and `output` (each `lufs`, `true_peak_db`, `lra`),
  `predicted_output` (loudnorm's own estimate before the AAC encode) and `ffmpeg_label`.
  `output` is measured from the file that was written.
- `mode` is `linear` when one constant gain was applied, and `dynamic` when it was not.
  Linear needs the gain that reaches the target to keep the true peak under that lowered
  target. If it
  would not, loudnorm falls back to dynamic processing, which can land well away from the
  target on short or steady audio. ffmpeg's own label says "linear" even in that case, so
  `mode` is decided from the measurements (peak headroom, and whether the output equals
  the input plus that gain) and the raw label is kept only as `ffmpeg_label`.
- Warnings are added to the build result when the output is more than 1 LU from the
  target or the true peak is more than 0.3 dB over the limit after the AAC encode.
- Silent audio cannot be measured and fails the build with a clear message.

### Sound effects

Synthesised by the tool from ffmpeg sources (noise and sine expressions): no sample files, no
downloads. Optional and off by default; a manifest without `sfx` renders exactly as before.

```json
"transition": {"type": "slide_left", "duration": 0.4, "sfx": {"type": "whoosh", "volume": 0.35, "offset": -0.1}}
"text_cards": [{"text": "...", "start": 0.3, "end": 1.9, "sfx": {"type": "click", "volume": 0.3}}]
"settings": {"sfx": {"enabled": true, "volume": 0.35}}
```

- `type`: `whoosh` (0.5 s of filtered pink noise with a fixed seed, so it is the same every time),
  `click` (a 60 ms decaying 1.8 kHz burst) or `riser` (a 1.2 s rising chirp).
- `volume`: 0 to 1, linear. Default `settings.sfx.volume`, itself 0.35. `offset`: seconds from the
  event, -1 to 1 (default 0). A transition's event is the start of the transition (the moment the
  next scene begins to appear; for a `cut`, the cut); a card's event is the card's `start`.
- A transition on the last scene cannot carry a sound. `settings.sfx.enabled: false` mutes every
  event without removing them from the manifest.
- All events are rendered into one track as long as the video and mixed in after the music
  ducking, so a whoosh is not ducked by the narration. Events must land inside the video (checked
  once the length is known, before rendering, and by `plan`); at most 100 events. The build
  result lists them under `sfx`, and `plan` prints them.
- Changing a sound rebuilds only the sound track and the final mix; pictures and narration
  segments are untouched. Loudness normalisation runs on the finished mix, effects included.
- Sound quality is subjective: synthesised whooshes can sound thin. Keep the volume low and
  listen to a preview first.

### Camera

An eased zoom and pan for a still image, as an alternative to a `motion` preset. Optional; the
presets and every manifest without a camera render exactly as before.

```json
"camera": {
  "from": {"zoom": 1.0, "x": 0.5, "y": 0.5},
  "to":   {"zoom": 1.25, "x": 0.35, "y": 0.4},
  "ease": "ease_in_out"
}
```

- `zoom`: 1 to 3 (1 shows the whole picture). `x`, `y`: where the centre of the visible window
  sits, 0 to 1 of the picture; the window is clamped so it never leaves the picture.
- `to` is required. `from` defaults to the whole picture, centred; fields left out of `to` stay
  as in `from`. `ease`: `linear`, `ease_in`, `ease_out`, `ease_in_out` (default).
- A camera can go on a scene or on an `assets[]` entry. On an entry it wins; an entry with
  `motion` uses that instead; an entry with neither uses the scene's camera or motion. A scene
  or entry cannot have both `motion` and `camera`.
- Images only: a camera on a video is rejected (videos are not zoomed). It works with
  `contain_blur` (it moves the contained picture inside its box).
- It uses the same `zoompan` render as the presets, so memory is unchanged. For 1080 wide
  graphics keep the zoom under about 1.5; higher values soften the picture. Changing a camera
  re-renders only that clip.

### Look

All optional and off by default. A manifest without `look` or `flash` renders exactly as before.

```json
"settings": {"look": {"vignette": {"amount": 0.35}, "grade": {"preset": "warm", "contrast": 1.05}}}
"transition": {"type": "crossfade", "duration": 0.5, "flash": {"duration": 0.08, "strength": 0.8, "color": "#FFFFFF"}}
```

- `vignette.amount`: 0 to 1; darkens the corners (0 is off).
- `grade`: `preset` (`warm`, `cool`, `faded`, `high_contrast`) and/or `contrast` (0.5 to 2),
  `saturation` (0 to 3), `brightness` (-0.5 to 0.5). Your numbers replace the preset's own
  contrast, saturation and brightness; `warm` and `cool` also shift the colour balance.
- The grade and the vignette are applied to the picture under the text, in the scene body and
  on both sides of a transition, so the look is the same across the overlap. A scene's `look`
  overrides `settings.look` per key (`vignette`, `grade`) and per field.
- `flash`: a brief colour flash over the middle of a transition. `duration` 0.03 to 0.3 s and
  shorter than the transition, `strength` above 0 up to 1 (default 0.8), `color` `#RRGGBB`
  (default white). It is a solid colour overlaid with a rising and falling alpha, so at low
  frame rates a short flash may be only a frame or two. A cut cannot flash.
- Grain is not part of this tier (file size and encode time).
- Changing a scene's look rebuilds that scene's picture segments and its junctions; changing a
  flash rebuilds only that junction and the joins.

### Caching and what rebuilds

Each stage's key includes all of its inputs (content of the source files, the
text, style, frame counts, settings), so a change rebuilds only what depends on it:

| Change | Rebuilt |
|---|---|
| A caption, text card or `pause_after` of scene X | X's picture, the junctions touching X, the picture join, the final mix |
| A scene's narration or audio | X's renderer stages, X's picture and narration segments, its junctions, both joins, the final mix |
| A transition between X and Y | the junction (and the neighbouring bodies' cut points), both joins, the final mix |
| `bgm` settings, file, or the file the seed picks | the final mix only |
| `settings.loudness` | the loudness stage only (the final mix is reused) |
| Another scene's anything | nothing of X's |

Any manifest that uses captions, text cards, transitions, `pause_after` or `bgm`
re-encodes each scene once more in the post stages. A manifest that uses none of
them takes the original path and its output is unchanged. Stages are written to
`.scene_cache/post/` and are not removed automatically.

### Limits

- Up to 1000 scenes per project.
- Everything runs through ffmpeg one stage at a time; memory does not grow with
  the number of scenes.
