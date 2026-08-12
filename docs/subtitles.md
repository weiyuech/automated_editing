# 字幕 — the subtitle layer

Subtitles are burned in from the narration's own per-word timestamps. There is no speech
recognition involved and none is needed: the TTS provider already reports when it said each word,
so the text and its timing are both known exactly.

An explicit 16:9 or 9:16 framing preset is applied before the ASS subtitle filter. With no preset,
the source frame is retained instead. Cue wrapping, font size and safe margins are therefore
calculated for the actual final frame in either case; converting to portrait does not clip a
landscape subtitle layout. Cropping an already-subtitled delivered MP4 is still unsafe, so 手动微调
should use its paired subtitle-free master and retained subtitle sidecar.

**No voiceover, no subtitles.** The words are the narration. A job that asks for subtitles without
one renders without them and says so in its warnings rather than failing.

## It is a layer, not something baked into the plan

Each export writes a sibling `.ass` file next to the video — `我的视频.mp4` gets `我的视频.ass`.
That file *is* the subtitle layer:

- One `ass=` filter draws it over the finished picture. Nothing about it is tied to how the edit
  was cut, so the same track can be laid over any video.
- Re-timing it is a text rewrite. Nothing is re-encoded, and no images are generated.
- It is readable and hand-correctable. If the TTS misheard a product name, fix that line and burn
  it again.

This is why the earlier plan — one PNG per cue, composited at render time — was dropped. It made
the subtitles a property of one particular render, so 手动微调 could not touch them without
regenerating everything.

## Timing cannot drift out of sync

Cue times are stored **relative to the voiceover**, never to the timeline.

The voiceover's position lives in exactly one field, `EditTimeline.voiceover_start_seconds`. At
render time that one number drives two things: the `adelay` on the narration audio, and the offset
added to every cue as the `.ass` is written.

So moving the narration in 手动微调 moves the text with it, necessarily — there is no second copy
of the timing that could be forgotten. `test_moving_the_voiceover_moves_the_subtitles_by_the_same_amount`
asserts the two really do move together.

## Layout survives a vertical frame

Nothing in the style is in pixels. Every dimension is a fraction of the output frame, which is
held on the timeline as `output_width` / `output_height`.

| | Rule | 1280×720 | 720×1280 |
|---|---|---|---|
| Font size | `0.064 × min(w, h)` | 46px | 46px |
| Side margins | `0.07 × w` | 90px | 50px |
| Bottom margin | `0.085 × h` | 61px | 109px |
| Outline | `0.09 × font size` | 4px | 4px |
| Shadow | `0.045 × font size` | 2px | 2px |
| Line capacity | derived | 47 half-widths | 26 half-widths |

The outline and shadow defaults were set from real footage, not from a test card. White text with a
thin outline vanishes into a bright, busy frame — and an exhibition hall, which is most of what
this app films, is exactly that: white walls, signage, and sometimes other people's captions
already on screen.

The font scales by the **short side**, which is the part worth understanding. Scaling by height
would make portrait text nearly twice the size it is on landscape; scaling by width would shrink
it to nothing. The short side keeps a glyph the same physical size in both — and because the frame
gets narrower while the glyph does not, the number of characters that fit on a line falls out
automatically. A line that runs unbroken across a landscape frame wraps at the comma on a portrait
one, with nothing configured twice.

When the robot team confirm a vertical mode, the only change is the two numbers on the timeline.

## Cue building

Words → cues, breaking in this order of preference:

1. A full stop (`。！？!?…`)
2. A pause in the speech of 0.7s or more
3. A comma (`，、,;；：:`), but only once the line is at least 60% full — breaking at every comma
   makes a stutter of two-character flashes
4. Wherever the line would otherwise overflow the frame or run past 5 seconds

Cues shorter than 1 second are stretched into the silence after them, where there is silence to
take. Where there is not, they stay short rather than overlapping the next line — a subtitle that
outlives its own sentence is worse than a brief one.

Line breaking is done in Python rather than left to libass, because Chinese has no spaces to break
at and libass's guess is not stable across sizes. Automatic wrapping stays switched on in the
script header as a safety net, so a line the estimate misjudges is folded rather than run off the
side of the frame.

## Fonts

Three, all SIL OFL 1.1, fetched and prepared by `scripts/prepare_assets.py`.

| Key | Family libass asks for | Notes |
|---|---|---|
| `noto_sans_sc` | `Noto Sans SC` | 思源黑体. General-purpose; safe for mixed CN/EN |
| `smiley_sans` | `Smiley Sans Oblique` | 得意黑. Condensed oblique, the short-video look |
| `noto_serif_sc` | `Noto Serif SC` | 思源宋体. Documentary tone |

Two things about these are worth writing down.

**The Notos are pinned to weight 700.** Google ships them as variable fonts whose *default
instance is the lightest weight in the family* — Thin and ExtraLight. libass does not instance
variable fonts, so it takes that default, and Thin text over footage is close to unreadable. The
prepare script pins each to 700 and rewrites the name table to match.

**Smiley Sans is shipped completely unmodified.** Its licence declares Reserved Font Names
`"Smiley"` and `"得意黑"`, and the OFL forbids a Modified Version from carrying a reserved name.
It is already static and heavy, so it needs no instancing. This is also why the app asks for
`Smiley Sans Oblique` — the font's actual family name — rather than a tidier one.

The other two were checked before being modified: Noto Sans SC reserves only `'Source'` (it
descends from Source Han Sans) and Noto Serif SC reserves nothing, so both may be instanced and
keep their names. `check_license()` re-verifies this against the licence text at fetch time rather
than trusting the table.

### The font trap

libass matches on the family name a font **declares internally**, not on its filename. Asking for
a name no available font declares is *not an error* — libass substitutes a system font and renders
happily. FFmpeg exits 0 and the video has text on it, in a typeface nobody chose.

This happened during development, and every one of the three fonts fell back at once. Two guards
exist because of it:

- `prepare_assets.py` asserts each prepared font answers to the exact name the app will ask for.
- `test_each_bundled_font_renders_differently` renders all three and compares the pixels. If
  libass substituted, the three come out identical and the test fails.

## FFmpeg must have libass

Subtitles are drawn by libass, and many FFmpeg builds are compiled without it — **including the
one Homebrew currently ships**, which has neither libass nor libfreetype and cannot draw text at
all. `brew install ffmpeg` will not fix this; the formula no longer depends on either.

So the binary is fetched and pinned rather than found:

```
python scripts/prepare_assets.py            # current platform
python scripts/prepare_assets.py --platform win64
```

`RenderService.ffmpeg_binary()` prefers `backend/vendor/ffmpeg/<platform>/ffmpeg` over anything on
`PATH`, so which build rendered an export does not depend on what happens to be installed.

If the active binary cannot burn text, a job that wants subtitles **fails with that reason**
rather than rendering without them. An export with the text silently missing looks identical
whether the cause was the binary or a narration with no timestamps, and the operator can only act
on the difference if told which it was.

## What the operator sees when it cannot work

Every way this comes back empty names its own cause, because they call for different actions:

| Situation | Reported as |
|---|---|
| No voiceover selected | Control disabled: 先选一个旁白… |
| FFmpeg without libass | Control disabled, and a job would fail naming libass |
| Font files not fetched | Control disabled: 运行 scripts/prepare_assets.py |
| Provider returned no word timings | Warning: 该配音没有逐字时间戳 |
| Sidecar missing or corrupt | Warning naming which of the two |
| Requested font not on disk | Warning, and it falls back to one that is |

## Packaging

`backend/vendor/` and the fonts directory are gitignored — both are reproduced from pinned URLs by
`prepare_assets.py`, which the packaging workflow runs. `test_every_offered_font_is_really_installed`
fails the build if that step is skipped, so a release cannot ship offering fonts it does not carry.
