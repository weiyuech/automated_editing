# The Edit Dimension Space

A formal statement of what an automated edit can vary, how large the resulting space is, and
how a batch of N outputs is mapped onto it. Written before the code, because "make them
diverse" is not implementable until the space is fixed and countable.

Companion to `docs/edit_diversity.md`, which explains *why* each dimension exists. This one
only counts.

## 1. Notation

An edit is a point in a product space. Each dimension `Xᵢ` is a finite set of levels with
radix `rᵢ = |Xᵢ|`. A **signature** is the tuple of structural levels — the part a viewer
perceives. Two outputs sharing a signature are the same edit made from different material,
however different their footage.

```text
Σ  = X₁ × X₂ × … × X_k        the signature space
|Σ| = r₁ · r₂ · … · r_k
```

## 2. The dimensions

Fixed list. Nothing else is a dimension; everything else is either a quality fix or an
implementation detail.

| Tier | # | Dimension | Levels | Radix | Class | State |
|---|---|---|---|---|---|---|
| 1 | X₀ | **Pace** | `fast`, `normal`, `cinematic` | 3 | governing | **live** |
| 2 | X₁ | Footage mix | `dwell_heavy`, `balanced`, `transit_heavy` | 3 | structural | **live** |
| 2 | X₂ | Emphasis | `target`, `coverage` | 2 | structural | **live** |
| 2 | X₃ | Contour | `flat`, `accelerate`, `decelerate`, `arc`, `follow_energy` | 5 | structural | **live** |
| 2 | X₄ | Start rotation | which point opens the edit | 4 | structural | **live** |
| 2 | X₅ | Recording scope | `½`, `⅔`, `⅚`, `1` of what the pace carries | 4 | content | **live** |
| 2 | X₆ | Point scope | `½`, `⅔`, `⅚`, `1` of what the pace carries | r₆(X₀) | structural | **live** |
| 3 | X₇ | Music track | the music pool | \|M\| | content | **live** |
| 3 | X₈ | Voiceover track | the voiceover pool | \|V\| | content | **live** |
| 4 | X₉ | Sampling seed | which cut comes out of each window | ∞ | cosmetic | **live** |

X₀ is the only **governing** dimension: its level changes the *radices* of the tier below it,
not merely the output. Everything in tier 2 is a peer of everything else in tier 2. See §3b.

`follow_energy` is listed as a contour rather than as its own axis. An earlier draft had it
separately as "intensity pacing", which double-counted it — following the music's loudness is
one way for cut length to vary across an output, and `arc` is another.

Three classes, because they do different work:

- **Structural** — changes the editorial decisions. This is what makes two videos look like
  two videos. Only these count towards `|Σ|`.
- **Content** — changes the material or the sound. Strong perceptually, but two outputs with
  the same signature and different music are still the same edit.
- **Cosmetic** — X₉ changes *which seconds* are used. Necessary to stop outputs being
  byte-identical, but on visually uniform footage it is nearly invisible. It is the
  tie-breaker when signatures must repeat, not a source of diversity in its own right.

### Notes on the undecided radices

- **X₄ (start rotation)** is written as 4, not P. Rotating a P-point route has P distinct
  starts, but past about four the outputs stop reading as different. Four is a choice, not a
  fact; the radix is a parameter.
- **X₅ and X₆ (scope)** are *fractions*, not subsets. Subsets of `n` things number `2ⁿ − 1`,
  which explodes and is mostly meaningless — two subsets differing by one item are not two
  different videos. The dimension is how much of what is available to use; *which* items are
  drawn is a secondary roll folded into X₉. Both use the same rule, §3a.

  The levels span the whole usable range rather than naming two points in it. `k = round(f ·
  k_cap)` is a whole number of places, so a fraction only reaches the edit through the integer
  it lands on: naming "a half" and "two thirds" was false precision that produced the same
  video at most paces and left the top third of the range unused. Four fractions separate into
  four distinct place counts where `k_cap` is large, and collapse by themselves where it is
  not — which is the applicability rule of §3 operating inside a dimension.

  There is deliberately no fraction below a half. Nobody selects eight recordings in order to
  make a video out of one, so those levels would be dealt to part of the batch and wanted by
  nobody. `all` is not a dealt level either — it means *stop narrowing*, not "the largest
  fraction", and is guaranteed once per batch by C1, which is where "at least one output
  covers everything" belongs.

## 3a. The scope rule

Scope is not a free choice. Showing `k` things in `T` seconds gives each of them `T/k`
seconds, and below some floor `τ` a thing is not shown at all — it is glimpsed. So there is
a hard ceiling on how many things an edit of a given length can carry:

```text
k_max = ⌊ T / τ ⌋            most things that still get a watchable share
k_cap = min(n, k_max)        bounded by what actually exists
k     = round(f · k_cap)     f ∈ {1/2, 2/3} is the dimension
```

with `τ` the shortest span that reads as a shot rather than a flash. `MIN_CHUNK_SECONDS`
(0.6s) is the wrong value for this — that is the flash-frame floor, the point below which a
cut is not a cut. A *place* needs several seconds to register. **τ ≈ 5s** is the proposed
value.

The fraction applies to `k_cap`, not to `n`. That distinction matters: applying it to `n`
makes the dimension inert whenever material is plentiful, because both fractions of twelve
exceed the ceiling and both get clamped to the same number.

### It reproduces the intuition

Twelve points, thirty seconds, τ = 5:

```text
k_max = ⌊30/5⌋ = 6      k_cap = min(12, 6) = 6      k = round(f·6) ∈ {3, 4}
```

Three or four points out of twelve — which is what you said before either of us wrote a
formula. That is a good sign the floor is in roughly the right place.

| T | τ | k_max | n = 12 | k at f=1/2 | k at f=2/3 |
|---|---|---|---|---|---|
| 15s | 5 | 3 | 12 | 2 | 2 |
| 30s | 5 | 6 | 12 | **3** | **4** |
| 60s | 5 | 12 | 12 | 6 | 8 |
| 30s | 5 | 6 | 4 | 2 | 3 |

At 15 seconds both fractions collapse to the same number, so X₇ degenerates to one level —
correctly, and by the same applicability principle as every other dimension. A fifteen-second
edit has room for two places, and no dimension can conjure a third.

### It also fixes a live defect

`_coverage_order` currently caps coverage at `⌊needed / MIN_CHUNK_SECONDS⌋`. With thirty
points and a ten-second target that is *sixteen points at 0.6 seconds each* — sixteen flash
frames, verified in testing. Under this rule the same job covers `⌊10/5⌋ = 2` points properly.
Covering two places well beats glimpsing sixteen, and the current behaviour is indefensible
rather than merely suboptimal.

## 3b. Pace is not a peer dimension — it governs

`τ` was written above as a constant, and the first draft of this document recommended keeping
it one on the grounds that the alternative complicated the mapping. That was the wrong call,
and for an instructive reason: it treated pace as a peer of the dimensions it actually
governs.

A place needs roughly two cuts to register, so `τ ≈ 2 × mean cut length` — 4s under a fast
edit, 16s under a cinematic one. A cinematic edit *genuinely* covers fewer places; that is
not an approximation error to be tolerated, it is the thing being chosen. Forcing it into a
flat product either loses it or fakes it.

So the space is a **hierarchy**, decoded top down:

```text
Tier 0   input facts          recordings, points, footage kinds, music
                              (not dimensions — they decide what applies)
Tier 1   pace                 mean cut length: fast | normal | cinematic
                              governs τ, hence every scope cap below
Tier 2   structural           contour, mix, emphasis, point scope,
                              start rotation, recording scope
Tier 3   content              music, voiceover
Tier 4   cosmetic             sampling seed
```

Tier 1 is decoded first; Tier 2's radices are then computed **given** that level. This makes
Σ a sum of products rather than a product:

```text
|Σ| = Σ         Π  rᵢ(ℓ)
      ℓ∈Tier1   i∈Tier2
```

which is still exactly countable and still deals by index — precompute the cumulative size of
each Tier-1 branch, find which branch an index falls in, decode the remainder in that
branch's radix vector. The dealing guarantees of §4 survive unchanged; only the decode gains
a step.

The gain is that `k_cap` is now honest per branch. At T = 30s with 12 points:

| Pace | mean cut | τ = 2·mean | k_max | k ∈ {½, ⅔}·k_cap |
|---|---|---|---|---|
| fast | 2s | 4s | 7 | 4, 5 |
| normal | 5s | 10s | 3 | 2, 2 |
| cinematic | 8s | 16s | 1 | 1, 1 |

A cinematic thirty-second edit shows **one place**, and should. Under a flat τ it would have
been handed six and glimpsed all of them.

Note what this does to the radices: `r₇` is 2 in the fast branch and 1 in the other two, so
scope stops being a dimension exactly where it stops being meaningful. That is the
applicability rule of §3 applied within a branch rather than across the whole space.

## 3c. Rhythm is a contour, not a number

The second consequence is larger than the first. Nothing above requires pace to be *constant*
across an output — and real edits are not. An edit can open slowly, accelerate, and settle;
that variation is most of what makes one feel authored rather than generated.

So Tier 1 sets the **mean** cut length, and a Tier 2 dimension sets the **shape** around it:

| Contour | Shape | Reads as |
|---|---|---|
| `flat` | constant | steady, current behaviour |
| `accelerate` | slow → fast | building |
| `decelerate` | fast → slow | arriving, settling |
| `arc` | slow → fast → slow | a complete piece |
| `follow_energy` | tracks the music's own loudness | cut with the track |

`follow_energy` is what was previously listed as X₅ "intensity pacing". It is not a separate
dimension; it is one contour among several, and treating it as its own axis double-counted it.

### What this costs to build

This is the one item in this document that is not a small change, because it inverts an
assumption in the planner. Today `_candidate_chunks` decides a cut's **duration** while
walking the source, before anything knows where in the output that cut will land. A contour
is a function of output position, so duration cannot be known at that point.

The inversion:

```text
now:      walk source → fixed-length chunks → select a subset → concatenate
contour:  walk source → candidate cut *points* → select N of them
                     → assign durations from the contour → concatenate
```

with, for a contour shape `s(u)` normalised over `u ∈ [0,1)`:

```text
N   = T / mean_cut_length              from Tier 1
dᵢ  = T · s(i/N) / Σⱼ s(j/N)           durations sum to T by construction
dᵢ ← min(dᵢ, shot_end(i) − startᵢ)     never read past the end of a shot
dᵢ ← snap to nearest beat multiple     when beat sync is on
```

Durations summing to `T` by construction is a real simplification — it removes the
per-kind fill, the water-filling top-ups, and `_stretch_last`, all of which exist only
because duration is currently fixed before the total is known.

The risk is that this is a rewrite of the selection core rather than an addition to it, and
that core now has regression tests protecting three fixed bugs. It should be done as a
replacement behind the same tests, not as an edit in place.

## 3. Applicability: the space is conditional on the input

A dimension with nothing to act on collapses to one level. This is the part that must be
computed per job rather than assumed, and it is where an honest answer about diversity lives.

| Dimension | Applies only when | Otherwise |
|---|---|---|
| X₀ pace | always | r₀ = 3 |
| X₁ mix | ≥ 2 footage kinds present (i.e. cruise footage with spans) | r₁ = 1 |
| X₂ emphasis | ≥ 2 labelled points | r₂ = 1 |
| X₃ contour | always; `follow_energy` needs music, so r₃ = 4 without it | r₃ = 4 or 5 |
| X₄ start rotation | ≥ 2 labelled points | r₄ = 1 |
| X₅ recording scope | ≥ 2 source recordings, and `k_cap ≥ 3` | r₅ = 1 |
| X₆ point scope | ≥ 2 labelled points, and `k_cap ≥ 3` **given the pace** | r₆ = 1 |

The `k_cap ≥ 3` condition is the scope rule's own applicability: below three, both fractions
round to the same `k` and the dimension has one level whatever the levels say. For X₆ that
condition is evaluated per pace branch, which is what makes it `r₆(X₀)`.

So the effective signature space is a sum over the governing tier of products within it:

```text
|Σ| = Σ         Π  rᵢ(ℓ) · 1[Xᵢ applies]
      ℓ∈X₀      i≥1
```

### Worked cases

All at T = 30s, 12 points, τ = 2 × mean cut length. Point scope survives only in the fast
branch, where `k_cap = ⌊30/4⌋ = 7`; at normal `k_cap = 3` and at cinematic `k_cap = 1`.

| Input | branch | r₁ | r₂ | r₃ | r₄ | r₅ | r₆ | product |
|---|---|---|---|---|---|---|---|---|
| **A.** One cruise recording, music | fast | 3 | 2 | 5 | 4 | 1 | 2 | 240 |
| | normal | 3 | 2 | 5 | 4 | 1 | 1 | 120 |
| | cinematic | 3 | 2 | 5 | 4 | 1 | 1 | 120 |
| | | | | | | | **\|Σ\| =** | **480** |
| **B.** Four cruise recordings, music | — | ×2 on r₅ throughout | | | | | | **960** |
| **C.** One cruise recording, no music | — | r₃ = 4, no `follow_energy` | | | | | | **384** |
| **D.** One cruise recording, 2 points | — | r₂, r₄, r₆ collapse | | | | | | **45** |
| **E.** One ordinary clip, no music | — | only pace and contour survive | | | | | | **12** |
| **Today** (X₁, X₂ only) | — | | 3 | 2 | — | — | — | **6** |

Case E is still the important one. **A single unclassified clip with no music supports twelve
distinct edits — pace times contour — and no amount of seeding changes that.** It is more than
the three the flat model allowed, because pace and contour are the two dimensions that need
nothing from the footage. Diversity remains a property of the input as much as the algorithm.

Case A against today's 6 is the argument: 6 → 480.

Note how much of case A comes from having *many points*. Case D — the same recording with two
points instead of twelve — collapses emphasis, start rotation and point scope together,
falling from 480 to 45. **Route length is a diversity input.** A twelve-point cruise is worth
far more than two six-point ones, which is a filming decision rather than a software one.

## 3d. Composition: the resolution order

The dimensions are not independent in *time*. Some cannot be decided until others are, and
some have nothing to decide when their input is absent. Getting the order wrong is how a knob
ends up doing nothing — the failure `style_preset` already demonstrated.

There is exactly one order that works, because each stage consumes only what the stages above
it have already fixed:

```text
0  facts        what exists: recordings, spans, labels, kinds, music?, voiceover?
                and what the operator pinned versus left open
1  budget T     how many seconds the output runs
2  policy       resolve every unpinned dimension by dealing
3  grid         a blank timeline of N slots with durations, no footage yet
4  fill         choose the source moment for each slot
5  audio        lay music and voiceover under the finished picture
```

Stage 3 is the inversion of §3c: the slot durations are built from the budget, the pace and
the contour **before any footage is looked at**. Stage 4 then goes shopping against a fixed
shopping list.

### Stage 1 — the budget, first because everything is a function of it

```text
T = target_duration
T = max(T, voiceover_length)      narration is never cut mid-sentence
T = min(T, total_available)       unless the voiceover raised it, in which case picture loops
```

### Stage 2 — pinned versus open

Every dimension is tri-state: a level, or **unset**. Unset means "you pick", never "use the
middle setting".

The deal in §4 must run over the **open** dimensions only. A pinned dimension is a constant,
not a radix — folding it in as a one-level axis would be harmless, but leaving it as a full
radix and then overriding it wastes deck positions and destroys the balance guarantee.

```text
Σ_free = Π rᵢ  over dimensions the operator left open
```

So pinning pace to `cinematic` does not merely fix one value; it shrinks the space the batch
is spread across, and the batch should be told so. A hundred outputs with everything pinned
except the seed is a hundred copies, and the honest response is to say that up front.

**A pinned but inapplicable level must not fail silently.** Pinning `follow_energy` with no
music has no meaning. Fall back to the nearest applicable level (`arc`), and warn. Doing
nothing quietly is the one behaviour that is never acceptable.

### Stage 3 — where librosa enters, and only here

This is the answer to "how do the pieces work together when some are missing": beats affect
**the grid**, never the footage. So every absence degrades in one place instead of everywhere.

```text
m   = mean cut length                         from pace
N   = round(T / m)                            slot count
dᵢ  = T · s(i/N) / Σⱼ s(j/N)                  from the contour, sums to T by construction
dᵢ ← snap boundaries to nearest beat          only if music and beat sync
```

librosa contributes exactly two separable things, both optional:

| librosa gives | Used for | Absent ⟹ |
|---|---|---|
| beat times | snapping slot boundaries so cuts land on the music | slots keep their analytic durations |
| onset/RMS envelope | the `follow_energy` contour shape | the analytic contours still work |

| Input | T from | Slot durations | Contour available |
|---|---|---|---|
| no music, no voiceover | target | analytic | 4 of 5 |
| music, beat sync off | target | analytic | 4 of 5 |
| music, beat sync on | target | snapped to beats | all 5 |
| voiceover only | max(target, vo) | analytic | 4 of 5 |
| voiceover + music + beat sync | max(target, vo) | snapped to beats | all 5 |

Nothing above changes stage 4. A run with no audio at all still gets pace, contour, mix,
emphasis, scope and rotation — which is why case E of §3 is twelve edits rather than one.

### Stage 4 — filling slots, in integers rather than seconds

With durations already fixed, stage 4 allocates **slots**, not seconds. This is the
simplification that makes the whole rearrangement worth doing:

```text
scope          → k places in play          (k ≤ k_cap, §3a)
start rotation → which place opens
emphasis       → how N slots divide among k places
                 target:   proportional to each place's footage
                 coverage: equal, ⌊N/k⌋ or ⌈N/k⌉ each
mix            → which footage kind each slot draws from, within its place
seed           → which exact moment inside the chosen window
```

Dividing N slots among k places is an integer partition. Compare that with today, where
seconds are divided among kinds and points in floating point, and every one of the three bugs
fixed this session lived in the machinery for making those sums come out right — the per-kind
fill, the water-filling, and holding the last cut longer. **All three become unnecessary**,
because durations are decided once, in stage 3, and never renegotiated.

There is a result hiding in the τ rule that makes this safe. Since `τ = 2m` and `N = T/m`:

```text
k_cap = ⌊T/τ⌋ = ⌊T/2m⌋ = ⌊N/2⌋
```

**Coverage is therefore always satisfiable by construction** — every place in scope can be
given at least two slots, whatever the pace. The τ = 2 × mean-cut rule is not a rule of thumb;
it is exactly the condition that makes "show every place properly" always achievable.

### The one edge that is not top-down

Scope picks `k` places before stage 4 knows how much footage those particular places hold. If
the chosen `k` happen to be thin, their footage can fall short of `T` and the picture would
have to loop while unused places sit outside the scope.

So scope selection carries a constraint as well as a count: **choose k places whose combined
footage covers T, with slack**, widening the selection if the first draw does not. Widening is
the least damaging repair — it keeps the requested length and the requested pace, and only
makes the edit slightly broader than the fraction asked for.

The slack has to be about **half a cut per run of footage**, which is what a cut landing
awkwardly against the end of a shot costs. It therefore scales with the pace, not with the
target. A flat percentage of `T` gets this wrong in both directions: too little slack for held
shots, and so much for quick ones that every scope widens back to the same size — which
silently flattens X₅ and X₆ into one level each while appearing to protect them.

## 4. The mapping

Given N outputs and applicable radices `(r₁ … r_k)`, assign each output `n ∈ [0, N)` a point
in Σ.

**Mixed-radix decoding.** Every point in Σ has an index `x ∈ [0, |Σ|)`, and

```text
digitᵢ(x) = ⌊ x / (r₁·r₂·…·r_{i−1}) ⌋  mod  rᵢ
```

so a single integer names a whole signature. No cross-product is ever materialised.

**Dealing.** Two properties are wanted, and — corrected from an earlier draft of this
document — **neither implies the other**:

1. Every level of every dimension gets its fair share of the batch.
2. No two outputs make the same set of choices while unused combinations remain.

Dealing each dimension from its own reshuffled deck gives (1) and lets combinations clump.
Drawing distinct indices out of the product gives (2) and, where the product dwarfs the batch,
leaves each dimension's share to luck — an earlier draft claimed (1) followed from (2), which
is only true when N approaches |Σ|.

So both, in two steps:

```text
1. deal each dimension from its own reshuffled deck        → balance
2. while any two outputs share a signature:
     swap one dimension's value between two outputs        → distinctness
```

A swap moves a level from one output to another **without changing how many outputs hold it**,
so the balance of step 1 survives any number of swaps by construction. Measured on a
hundred-output batch: 100 distinct signatures, with pace at 34/33/33, contour at 20/20/20/20/20
and emphasis at 50/50.

This subsumes `_avoid_repeat_pairs`, which was a two-dimensional patch on exactly this
problem.

**Content and cosmetic dimensions** are dealt separately, from their own decks: X₇ and X₈ as
today, X₉ drawn distinct per output. Their job is to separate outputs that must share a
signature when N > |Σ|.

**Only open dimensions are dealt** (§3d stage 2). The index space is `Σ_free`, the product
over dimensions the operator left unset; pinned dimensions are constants applied after
decoding. When `N > |Σ_free|` the batch should say how much of the space it actually had —
"a hundred outputs over twelve signatures" is information the operator needs before waiting
for the render, not after watching it.

## 5. Constraints on the mapping

Constraints are applied after decoding, and each one costs a little uniformity. Stated
explicitly so the cost is deliberate.

- **C1 — at least one output uses everything.** Pin output 0 to full scope on both X₆ and
  X₇: every recording, every point. `all` is not a dealt level precisely because it belongs
  here — dealt, it would land on a third of the batch; pinned, it lands exactly once. Without
  it, a batch drawn purely at random can leave the complete edit unmade, which is the one
  output most likely to be wanted.
- **C2 — every recording appears somewhere in the batch.** Implied by C1. Note it is *not*
  implied by the scope dimensions alone: repeated halves and two-thirds can miss an item
  entirely across a whole batch.
- **C3 — every output is reproducible.** Its full decoded tuple plus its seed is recorded on
  the job, so one output can be rebuilt without rerunning the batch.
- **C4 — the batch is reproducible.** One batch seed drives every draw above.

C1 is the only constraint that biases the distribution, and it biases exactly one output out
of N.

## 6. What is not a dimension

Three things worth building that do **not** enlarge Σ, and should not be counted as if they
did:

- **Beat snapping.** Cuts are currently beat-*sized* but not beat-*aligned* — lengths come
  from beat intervals, but cuts are laid end to end from each scene start, so landing on a
  beat is coincidence. Snapping cut points to the nearest beat is a quality fix to how X₃ is
  realised. It makes every level of X₃ better; it adds no levels.
- **Discarded tempo.** `beat_track` returns a BPM estimate that is thrown away, and cut
  lengths are re-derived by differencing beat timestamps and clamping to 1–6s — a clamp that
  itself breaks alignment. Using the reported tempo makes X₃ exact rather than approximate.
- **Repeated analysis.** The same music file is analysed once per source video, so a
  ten-clip job runs ten identical beat detections. The fix is not a disk cache — a beat list
  is a few hundred floats, a few KB, so holding it in memory for the life of the job (keyed
  by path and mtime) removes the waste with no storage cost and nothing to clean up. Disk
  caching would trade a compute problem for a storage problem, which is the wrong trade at
  this size.

X₅ (intensity pacing) *is* a dimension rather than a quality fix, because `flat` and
`follow_energy` are two different edits of the same footage, both defensible.

## 7. Order of work

The math above says which order pays.

All of §2 is built. Case A went from 6 signatures to 480.

What the rewrite of §3c actually removed, as predicted: the per-kind fill, the water-filling
top-ups and `_stretch_last` are all gone, along with the class of bug that lived in them.
Durations are decided once and never renegotiated.

Three defects were found and fixed while building it, each a case of counting cuts where
seconds were meant:

- A group asked for more cuts than its footage could fill replayed the earliest of them,
  which read as a jump backwards in time. Shares are now checked against footage as the cuts
  are handed out, not estimated from an average beforehand.
- A place too short for a whole cut was skipped rather than given a shortened one, so a
  two-second point vanished from an edit meant to cover every point.
- Beat snapping ran after the shot-length ceiling and could push a cut back over it. The
  ceiling now travels into the snap.

### What is left

| Step | Adds | Note |
|---|---|---|
| D2–D4 transitions, speed ramps, grading | — | genuinely new capabilities, parked |
| τ per contour rather than per pace | — | §3b's refinement; the coupling is honest at the pace level already |

One quality fix is still worth taking: `beat_track` returns a tempo estimate that is still
discarded, and cut lengths could be derived from it exactly rather than from the grid. It adds
no levels; it makes every level of X₀ and X₃ land more precisely.
