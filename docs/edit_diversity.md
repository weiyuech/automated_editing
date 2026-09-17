# Edit Diversity

How one pile of footage becomes many different videos.

The formal statement — the fixed dimension list, how large the space is for a given input,
and how N outputs are mapped onto it — lives in `docs/edit_dimension_space.md`. This document
explains why each dimension exists and what it does to an edit; that one counts them.

Diversity here is a property of a **batch**, not of a single edit. Within one video the
planner should make one coherent set of choices; the variation lives in making *different*
choices for the next video. At a hundred outputs a day, the question is not "is this edit
good" but "are these hundred edits different from each other in ways a viewer notices".

## Where the batch stands today

`JobService.create_batch` already establishes the pattern. Music and voiceover are dealt
from pools with `_deal_pool`, and `_avoid_repeat_pairs` spends the whole music × voiceover
grid before any pairing repeats. That is a real combinatorial diversity engine — it just
only has two dimensions plugged into it.

Everything downstream of the deal is deterministic. There is no `random` in `timeline.py`,
`analysis.py`, or `render.py`. The picture is a pure function of:

```text
(source videos, their order, music track, target_duration_seconds, beat_sync)
```

So the number of distinct **pictures** a batch can produce is the number of distinct
`(video order × music track)` combinations, and nothing else:

| Setup | Distinct pictures in a 100-output batch |
|---|---|
| 1 cruise recording, 1 music track | **1** — one hundred identical videos with different audio |
| 1 cruise recording, 4 music tracks, `beat_sync` on | **4** |
| 1 cruise recording, 4 music tracks, `beat_sync` off | **1** — beats are the only way music reaches the picture |
| 5 imported clips, 4 music tracks | up to 4 × 120 orderings, though `_select_chunks` samples all of them the same way |

Music changes the picture only because beat intervals become chunk lengths
(`timeline.py:156`). With `beat_sync` off, music is decoration over an identical cut.

## The dimensions

Grouped by what they vary, and marked by what each costs to build.

**Live** — already varies per output.
**Cheap** — the value exists, it is just fixed where it should be dealt.
**Plumbing** — needs data carried from the cruise into the planner first.
**New** — the capability does not exist at all.

### A. Audio

| # | Dimension | Levels | State |
|---|---|---|---|
| A1 | Music track | the music pool | **Live** |
| A2 | Voiceover track | the voiceover pool | **Live** |
| A3 | Keep or mute original camera audio | on / off | **Cheap** — batch-wide bool today |
| A4 | Music duck level under narration | e.g. 0.15 / 0.25 / 0.40 | **Cheap** — `MUSIC_BED_VOLUME` is a constant |

A1 and A2 are the only dimensions currently doing any work. Note that a voiceover is always
TTS (`role == "tts_voice"`) and can never be a music file, and only the voiceover's length
can stretch an edit — music never does.

### B. Which footage gets used

This is where the real perceived difference lives, and where almost nothing exists yet.

| # | Dimension | Levels | State |
|---|---|---|---|
| B1 | Sampling phase | which cut is taken from each stride window | **Live** |
| B2 | **Footage mix** | `dwell_heavy` / `balanced` / `transit_heavy` | **Live** |
| B3 | Failed-leg policy | folded into B2 — every mix pushes failed and skipped legs down | **Live** |
| B4 | **Emphasis** | `target` / `coverage` | **Live** |
| B5 | Point order | route order / reversed / shuffled | **Ready to build** |
| B6 | Tour start point | rotate which point opens the video | **Ready to build** |
| B7 | Point subset | e.g. "any 6 of the 12 points" | **Ready to build** |

B2 and B4 are the two that a viewer actually notices, and they are the ones a batch varies.
B1 changes *which seconds* are sampled, which is real but invisible — two edits that made the
same editorial choices look like the same video whichever seconds they used.

**B2, the mix**, leans an edit towards footage shot standing still or footage shot moving:

| Mix | dwell | transit | unknown | failed / skipped |
|---|---|---|---|---|
| `dwell_heavy` | 3.0 | 1.0 | 2.0 | 0.3 |
| `balanced` | 2.0 | 2.0 | 2.0 | 0.5 |
| `transit_heavy` | 1.0 | 3.0 | 2.0 | 0.5 |

These are multipliers on how much of that footage exists, not quotas. A lean can therefore
never demand more of something than was filmed — three seconds of parked footage stays three
seconds under `dwell_heavy` rather than being looped to fill a quota. Failed and skipped legs
are pushed down everywhere but never to zero, so a run where everything failed still produces
a video. On a four-point run at a 30-second target the three mixes spend 19.3s, 11.2s and
5.0s on parked footage respectively.

**B4, the emphasis**, decides what to do when the target cannot buy everything:

- `target` — fill the running time with cuts of a natural length, sampled proportionally.
- `coverage` — split the time between the points first, so none is missed.

They diverge exactly when the footage is uneven. On a run whose first point had a long
approach and whose other four were quick, `target` spends 26 of its 30 seconds on point one
and misses two points entirely; `coverage` gives all five six seconds each. When the target
is too short to give every point a watchable cut, coverage takes an even spread across the
route — not the first N — and warns how many it left out.

Each sort of footage is filled to its own share rather than trimmed from a merged list
afterwards, because trimming a merged list can only cut from the end, and the end is
chronological: an overshoot would be paid for by whatever was filmed last, silently dropping
the final points of a run.

**B1 is live.** `_select_chunks` used to stride from index 0 every time, so two outputs with
the same footage picked the same cuts. It now rolls each stride window separately from the
output's `variant_seed`.

Rolling *per window* rather than shifting every window by one shared phase matters more than
it looks. Both preserve the coverage guarantee — the windows tile the recording and exactly
one cut comes out of each, in order — but a shared phase can only produce as many distinct
pictures as there are candidates inside one window. On three minutes of footage cut to
thirty seconds that is six. Per-window rolls multiply the choices instead of adding them:
the same material yields 100 distinct pictures from 100 seeds, every one still spanning the
whole recording.

### Choosing them, or not

`footage_mix` and `emphasis` are three-state: a level, or **unset**. Unset means "you pick",
not "use the middle setting" — quietly defaulting to `balanced` and `target` would make every
edit nobody configured the same edit, which is the thing all of this exists to stop. One video
asked for on its own is as likely to lean on movement as any output of a batch of a hundred.

The roll is resolved **once, when the job is created**, and written back onto the request, so:

- the job record is an honest account of what it made, not of what was asked for;
- resending that request reproduces that video;
- a seeded request derives its policy from the seed, so the seed alone still reproduces the
  whole job, cuts and policy together.

An explicit choice is never overridden. Within a batch the levels are dealt from a reshuffled
deck rather than rolled independently, so twelve outputs land four per mix and six per
emphasis instead of a lucky clump; a single job rolls uniformly, there being nothing to
spread across. `EditBatchRequest.footage_mixes` and `.emphases` narrow the spread when only
some levels are wanted. Those fields are what the operator buttons will drive — and the
control needs an explicit 自动 position, because a dropdown sitting on `balanced` would
misrepresent the default as a middle setting rather than a roll.

B2–B7 shared one prerequisite, and it is now met. `CruiseSegment` already held
`transit_start_seconds`, `arrived_at_seconds`, `departed_at_seconds` and `status`, but
`attach_to_recording` wrote only `notes` and `markers`, so the sidecar
recorded arrival instants and never departures — a dwell had a start and no end, and nothing
downstream could tell a parked shot from a moving one.

The sidecar now carries `segments`. `AnalysisService._merge_cruise_segments` cuts the
recording along those spans and tags every scene `dwell` / `transit` / `failed` / `skipped` /
`unknown` with its point label; `EditPlanner` carries the tag onto each `Chunk` and out to
`TimelineClip.footage` and `TimelineClip.label`. Detected cuts still subdivide the spans, so
beat-syncing keeps somewhere to cut, and a stretch that belongs to no span — footage rolling
after the last point — is honestly labelled `unknown` rather than guessed at.

Recordings made before this change, and every manual capture, fall back to the marker path
unchanged. Footage the robot never classified has nothing to lean on, so every mix and
emphasis produces the same edit from an ordinary import — the policies cost nothing where
they mean nothing.

On B2: dwell footage is not automatically the better footage. A glide down an aisle is often
the most watchable thing in a run, and a static parked shot can be dead. So this is a
**mix weighting that varies across outputs**, not a fixed ranking — some videos lean on
parked shots, some on movement, and the batch covers both.

### C. Rhythm

| # | Dimension | Levels | State |
|---|---|---|---|
| C1 | Target duration | e.g. sample 15–45s per output instead of one batch value | **Cheap** |
| C2 | Beat sync on/off | on / off | **Cheap** — batch-wide bool today |
| C3 | Beat grid | half-bar / bar / two-bar | **Cheap** — `step = 4` is hardcoded |
| C4 | Cut length when not beat-driven | fast 2s / normal 5s / long 8s | **Cheap** — `CHUNK_SECONDS` is a constant |

C1–C4 are all one-line constants or batch-wide fields today. They are the cheapest way to
add visible variety after B1, and C3 in particular changes the feel of an edit a lot for
how little it costs.

### D. Look

| # | Dimension | Levels | State |
|---|---|---|---|
| D1 | Aspect ratio | original / 16:9 landscape / 9:16 vertical | **Shipped** — optional framing preset before subtitles |
| D2 | Transitions | hard cut / crossfade / dip to black | **New** — only hard cuts exist |
| D3 | Speed ramp on transit legs | none / 1.5× / 2× | **New** |
| D4 | Colour or grade preset | none / warm / cool / contrast | **New** |

D1 is app-wide rather than varied within a batch: the operator may keep the source frame or
choose a destination frame under Hardware & Camera before shooting. Every new job freezes that
choice, applies any selected crop, and only then lays out subtitles.

## Is this enough for a hundred a day?

Two different questions hide inside that one, and they have opposite answers.

**Does the footage run out?** No, and not remotely. An hour of recording cut to thirty
seconds is 120:1 compression. Simulating a hundred outputs from a sixty-minute, thirty-point
run:

```text
mean footage overlap between any two outputs:  2.6%
pairs sharing nothing at all:               4591 / 4950
pairs sharing more than 25% of footage:       178 / 4950
```

Ninety-three percent of all pairs share **no source footage whatsoever**. At this ratio the
material is nowhere near the binding constraint.

**Do the outputs look different?** That is a different measure, and here the answer is: less
than the footage numbers suggest. Two edits built from different seconds of visually similar
footage, having made the *same editorial choices*, still read as the same video. A robot
gliding past similar aisles produces minute 7 and minute 37 that look alike.

What separates one video from another is its **structural signature** — the set of editorial
choices behind it. Today that is:

| Signatures | From |
|---|---|
| 3 | footage mix |
| × 2 | emphasis |
| **= 6** | |

A hundred outputs over six signatures is about seventeen videos per signature: seventeen that
made identical editorial choices and differ only in which similar-looking seconds they used,
plus their soundtrack. Those seventeen will feel same-y however little footage they share.

Music and narration do a great deal of perceptual work — audio dominates how a short video
reads — so a ten-track music pool and ten voiceovers genuinely help. But they cannot fix a
picture that made the same choices.

Raising the signature count is therefore the highest-value work left, and it is cheap:

| Add | Levels | Signatures |
|---|---|---|
| today | — | 6 |
| C1 duration band (15 / 30 / 45s) | ×3 | 18 |
| C4 cut length (fast / normal / long) | ×3 | 54 |
| C3 beat grid (half-bar / bar / two-bar) | ×3 | 162 |
| B6 start point rotation | ×4 | 648 |

C1 and C4 together take it from six to fifty-four — roughly two outputs per signature at a
hundred a day, instead of seventeen. Both are module constants and a batch-wide field today,
which is why they are the next thing to build.

## How to combine them

Three ways to spend a batch across the grid.

**Full exhaustion** — extend `_avoid_repeat_pairs` to N dimensions, so no combination
repeats until all are used. This is what the code does today for music × voiceover, and it
stops scaling almost immediately: four music × three voice × three mixes × three durations ×
two beat grids is 216 combinations, so a 100-output day cannot even cover it once, and which
100 you get is arbitrary.

**Independent random rolls** — each output rolls each dimension on its own. Simple, but with
100 draws you will get clumps: some level of some dimension will appear twenty times and
another twice, purely by luck.

**Stratified sampling (recommended)** — guarantee each *level* of each dimension appears
proportionally across the batch, without requiring the full cross-product. With 100 outputs
and three mix profiles, exactly 33/33/34 outputs get each one; the same holds independently
for duration band, beat grid, and music. This gives even coverage of every dimension at
realistic batch sizes, which full exhaustion cannot, and avoids the clumping of independent
rolls.

## Seeds

Every batch carries one. `EditBatchRequest.seed` drives the music and voiceover deals, the
source ordering, and each output's own `variant_seed`; left blank, one is drawn and recorded
rather than lost. Each job stores both its `batch_seed` and its `variant_seed`, so:

- **Replay a day** — resend the batch with the same `seed`.
- **Roll it again** — resend with a different one.
- **Rebuild one output** — create a single job with that job's `variant_seed`, without
  rerunning the other ninety-nine.

Seeds stay inside a signed 32-bit range so they survive JSON, logs, and being pasted back in.
A job created by hand with no seed keeps the old, wholly deterministic pick — only batches
roll dice.

### Seeded is not the opposite of repeatable

A seed is a *fixed* choice, not a random one, so "different every time" and "identical every
time" are the same mechanism set to different positions:

| `variant_seed` | Cuts |
|---|---|
| `None` | The canonical stride-from-0 pick. Byte-identical to every export rendered before seeding existed. |
| a pinned number | A different pick, equally repeatable. Seed 4242 gives the same timestamps forever. |
| drawn per output | Maximum variety. |

`EditBatchRequest.cut_variation` chooses which of the three a batch uses:

- **`per_output`** (default) — every output takes its own cuts.
- **`shared`** — one seeded set of cuts across the whole batch, so music and narration are
  the only variables. This is the setting for judging two soundtracks against each other, or
  for a series whose edit is approved and whose narration is all that changes.
- **`fixed`** — the canonical unseeded cuts, for reproducing work made before seeding
  without needing to know a seed.

Diversity does not have to live in the cuts. Holding the picture still moves it to the other
dimensions — music, voiceover, duration, rhythm — rather than switching it off.

## Remaining order

1. ~~**B1 sampling phase**~~ — done.
2. ~~**Sidecar segment spans**~~ — done.
3. ~~**B2 mix and B4 emphasis**~~ — done; the editorial choices now vary across a batch.
4. **C1–C4 rhythm dimensions** — constants and batch-wide flags become per-output deals.
   Cheapest visible variety left.
5. **B5–B7 point order, start point, subset** — unblocked by the spans, not yet built.
6. **D1 aspect ratio** — only if the output is headed somewhere vertical.

D2–D4 (transitions, speed ramps, grading) are genuinely new capabilities rather than
unfinished ones, and are parked until the list above is spent.

Presets and the operator-facing buttons come last: a preset is a named point in this space,
so the space has to exist before naming points in it means anything.
