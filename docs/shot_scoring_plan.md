# Shot Scoring

A design record for making the editor able to tell a usable shot from a damaged one.

The cheap technical scorer, cached analysis, PyAV-backed scene detection, local quality
profiles, quality-weighted allocation, and portfolio similarity terms are now implemented.
The remaining limit is aesthetic/semantic judgement: no uncalibrated model is presented as a
substitute for human ratings we do not yet have.

Scoring is worth more than the quality it buys, which is why it comes before the remaining
diversity work rather than after it. See §6.

## 0. The prerequisite: analysis is not cached

Measured on a two-minute clip: `analyze_video` takes **9–11 seconds** and runs **once per
job**. Scene detection is a subprocess launch and a full decode, repeated for every output of
a batch.

| Batch | Analysis cost today | With scoring added, uncached | Cached per source |
|---|---|---|---|
| 100 outputs, 1 recording | ~17 min | ~2 hr | ~70 s |

The music path already has this fix — beats and loudness are held in memory keyed on path,
size and mtime, because a ten-clip job was asking for the same song ten times. Scene detection
never got the same treatment, and scoring is the more expensive half.

**So: cache the whole per-source analysis before anything else.** Every phase below is
unaffordable without it and routine with it.

Key on `(resolved path, size, mtime_ns)`, as the music cache does. Hold scenes, shot scores and
hashes together — they all come from the same decode and are invalidated by the same edit.

## 1. Use the scene detector properly

> **Found while doing this: PySceneDetect had never run.** Its OpenCV backend asks the capture
> for a position in milliseconds and converts that to a timecode; OpenCV answers `NaN`, the
> conversion raises, and the decode thread dies. It failed on **every file tried** — synthetic
> clips, repaired proxies, and the project's own exports and downloads alike. The failure
> surfaced only as a warning, so detection fell back to the crude frame-differencer — the one
> capped at eight scenes — for the entire life of the project.
>
> The fix is the **PyAV backend**, which reads timestamps from the container rather than asking
> a capture object. With it, all three detectors return the exact ground-truth cuts on a test
> clip, and analysis of a real recording drops from ~10s to ~3s because the timestamp-repair
> proxy is no longer needed either.
>
> This is worth more than everything else in this document. Every scene boundary the planner
> has ever worked from came from the fallback.

`scene_detect_worker.py` calls `ContentDetector()` with every parameter at its default. The
library ships four other detectors and several knobs that bear directly on our footage.

| Change | Why |
|---|---|
| `AdaptiveDetector` instead of `ContentDetector` | Its threshold is a rolling average of recent frame deltas rather than a fixed number, which exists specifically to stop fast camera motion registering as cuts. A robot gliding down an aisle is exactly that case, and we currently use the detector least suited to it. |
| `weights=Components(delta_edges=…)` | Edge differencing defaults to **0.0** — switched off. It is the signal that survives low-contrast, evenly-lit interiors, which is most of what a shop robot films. |
| `min_scene_len` tied to `MIN_SLOT_SECONDS` | Currently 15 frames by coincidence. A shot shorter than a cut is not a shot. |

Measured cost of the edge weight: about 30% more time (3.8s → 4.8s on a hundred-second clip),
which is affordable now that analysis happens once per file rather than once per output.

**Correction to an earlier draft of this document**: `HashDetector` is a *detector* — it finds
cuts by hash distance and exposes no per-shot hash. It is not the tool for near-duplicate
rejection. Those hashes have to be computed in the scoring pass (§2), where the frames are
already decoded.

Neither this nor anything in the library answers whether a shot is *good* — detection says
where shots begin. Quality is §2 onward.

## 2. Cheap metrics, no new dependencies

> **Found while doing this: OpenCV cannot decode this project's files either.** `VideoCapture`
> opens them and then `grab()` fails on the very first call, so nothing built on its decoder
> can measure anything — and the OpenCV *scene fallback*, which is what every analysis has
> been landing on, was therefore returning its hardcoded six-second stub. Frame sampling now
> uses PyAV. OpenCV's image operations are fine and are still used; only the reading of frames
> moved.

Sampled at a handful of frames per shot, decoded once per file:

| Metric | How | Catches |
|---|---|---|
| Sharpness | variance of the Laplacian | out of focus, motion blur |
| Exposure | histogram mass at 0 and 255 | crushed blacks, blown windows |
| Motion | mean absolute frame difference | violent pans **and** dead static shots |

The first two answer **"is this shot broken?"**, which is most of the available win and none of
the dependency risk.

**Motion is not one of those, and an earlier draft had it wrong.** It was written as "reject
high motion", as though less were always better. It is not a direction, it is a **band**: a
violent pan is unusable, and so is a locked-off shot of an empty aisle where nothing happens
for eight seconds. One is unwatchable, the other is boring, and a metric that only penalises
one end will quietly fill an edit with the other. The score should peak in the middle and fall
away at both ends.

Motion has a second use beyond scoring: **cut at motion minima**. A cut placed mid-pan reads as
a mistake to a viewer who could not say why. The grid already knows where it wants its cut
points; nudging each to the nearest quiet moment is the same shape of operation as snapping to
a beat, and can share that code.

## 3. Where a score enters the pipeline

Scores are a property of a **shot**, computed once in stage 0 and consumed in stage 4 —
choosing which source moment fills each slot. No other stage changes.

The integration point is `_place_run`, which today walks a group of shots and spends the
unused footage as `spread_gaps` — a random partition of the slack. Scoring replaces the
uniform walk with a **weighted one**: build a quality-weighted distribution over the group's
timeline and draw the cut positions from it, in order. Low-scoring stretches get walked past;
high-scoring ones get visited more often.

Every existing invariant survives, because positions are still drawn in ascending order from
disjoint windows: no duplicates, no jumps backwards, no cut crossing a shot boundary.

### The tension that has to be designed around

**Quality and diversity pull in opposite directions.** If every output takes the best shots,
every output is the same video. Ranking is therefore the wrong operation. Three bands instead:

```text
below the floor   never used            (broken)
above the floor   weighted, not sorted  (better shots are likelier, not certain)
the seed          samples the weights   (this is where diversity comes from)
```

This is the same "deal from a pool" shape the batch already uses for music, voiceovers and
policy levels, and it keeps the two goals compatible instead of trading one for the other.

## 4. Similarity: a term in the score, not a rule

Repetition should be **priced, not forbidden**. A hard "no two clips may be similar" rule is a
feasibility constraint, and this planner has now produced three separate ordering bugs from
exactly that shape — a rule that cannot always be satisfied, a fallback for when it cannot, and
the bug living in the fallback. Footage of one shop aisle may simply have no dissimilar pair in
it; a rule then has to either fail or give up, while a penalty just becomes uniform and stops
mattering. Similarity is continuous, so any threshold on it is arbitrary.

The goal is not "no duplicates". It is **smooth, and not boring** — and those are two different
things that want opposite treatment at different scales.

| Scale | Want | Measure | Too little | Too much |
|---|---|---|---|---|
| Between adjacent cuts | *continuity* | mean colour and exposure delta | jump-cut, looks like a mistake | a jolt at every cut |
| Across the whole output | *variety* | pairwise perceptual-hash distance | boring | incoherent |
| Across the batch | *diversity* | content other outputs did not use | a hundred near-copies | — |

The important line is the first two: **similarity is wanted locally and unwanted globally.**
That is ordinary editing grammar — shots that sit next to each other should belong together,
while the piece as a whole should go somewhere. A single "reject duplicates" rule serves
neither, because it pushes towards dissimilarity at *both* scales and so buys variety by
spending smoothness.

Worth separating the two measurements rather than deriving both from one number:

- **Colour and exposure delta** for smoothness. Three numbers per shot (mean L, a, b) and a
  subtraction. Directly targets the visible jolt when a dim interior is cut against a bright
  window, which is what actually reads as unpolished.
- **Perceptual hash distance** for variety. Answers "have we shown this already", which colour
  cannot: two different aisles can share an average colour.

### How it reaches the walk

`_place_run` already picks a position inside each window, with the seed choosing where. The
score biases that choice: among the positions a window offers, prefer one that is sharp, in the
motion band, close in colour to the cut before it, and far in hash from everything already
placed — with the seed still choosing among near-equal candidates, so two outputs of the same
footage do not collapse onto the same picks.

Greedy and one-pass, which is enough. This is biasing, not optimising, and a global search
would buy little for a large change in the one part of the code every ordering bug has come
from.

## 5. DOVER-Mobile

[DOVER](https://github.com/VQAssessment/DOVER) (ICCV 2023) scores video on two disentangled
branches — **technical** and **aesthetic**. The split is unusually well matched to this
problem: a robot pan can be technically clean and aesthetically dead, and those should rank
differently. `DOVER-Mobile` is 9.86M parameters and 52.3 GFLOPs, 5.4× lighter than full DOVER.

It answers the question the cheap metrics cannot: not "is this shot broken" but "is this shot
*good*".

Treat it as an optional extra alongside `backend[beat]`, so a machine without it still edits:

- absent → §2 metrics alone, aesthetic term neutral
- present → aesthetic and technical scores per shot, blended with §2

Two risks worth stating before committing:

- **Not on PyPI.** Installation is a git clone plus `pip install -e .`, in a project whose
  existing heavy dependency needed version pinning to avoid local LLVM builds. Vendoring a
  pinned commit is likely safer than tracking the repo.
- **Weights.** They must live under `.cache/`, like the numba and proxy caches, or the
  project's "nothing outside this folder" rule is broken by a model download.

Cost, once §0 exists: perhaps 60 shots per recording at CPU inference, order a minute, **once
per recording** rather than once per output. Amortised across a hundred outputs it disappears.

## 6. Why scoring is worth more than quality

It is the missing input for four things that are not about quality at all.

**It fixes the diversity collapse on plain footage.** An hour of non-cruise footage currently
supports 15 distinct outputs, because every dimension except pace and contour is gated on
cruise points. A score profile over a recording has peaks, and those peaks are *segments* —
the same role a cruise point plays. With them, emphasis, scope and rotation all apply to
ordinary footage: **15 → 1440**, with no robot involved.

**It demotes the dwell/transit distinction to a prior.** `footage_mix` exists because we could
not tell a good moving shot from a bad parked one, so we guessed by category. A good transit
shot beats a dull dwell shot, and a score knows which is which. The mix becomes a lean, not the
main signal.

**It gives the shortfall problem something to work with.** A cinematic pace on short shots
currently warns and comes up short. A scorer that also knows shot *lengths* can prefer shots
able to supply the requested cut.

**It makes the capacity estimate honest.** The per-batch cap is only as good as its estimate of
how many different videos the material can make, and that estimate presently ignores content
entirely.

## 7. Order, and what each step costs

| # | Step | New deps | Risk | Payoff | State |
|---|---|---|---|---|---|
| 0 | Cache analysis per source | none | low | 18 min → 11 s on a 100-output batch | **done** |
| 1 | PyAV backend, AdaptiveDetector, edges | `av` | low | scene detection runs *at all*, for the first time | **done** |
| 2 | Sharpness / exposure / motion band | none | low | shots are measured; scores differ 0.43 vs 1.00 on test footage | **done** |
| 3 | Local profiles + quality-weighted allocation | none | medium | quality reaches the screen while chronology stays hard | **done** |
| 4 | Similarity as candidate score terms | none | low | adjacent visual flow plus whole-output non-repetition | **done** |
| 5 | DOVER-Mobile | torch | medium | "good" rather than "not broken" | **deferred: no labelled validation** |
| 6 | Score-derived segments | none | medium | 15 → 1440 on plain footage | |

Steps 0–2 are free of dependency risk and independently useful; 3 is the one that touches code
protected by the ordering regression tests and should be done as a replacement behind them, not
an edit in place. 5 can be deferred indefinitely without stranding anything, because every
consumer of a score treats the aesthetic term as optional.
