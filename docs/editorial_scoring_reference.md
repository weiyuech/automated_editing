> 版本说明：本文保留改版前的设计记录。当前已改为固定镜头、树形精确拼接和显式旁白映射；旧自动选片、节拍与语义匹配规则不再执行。当前操作与实现以 [根 README](../README.md) 和 [改版说明](controlled_capture_concat_plan.md) 为准。

# Editorial planner: executable contract and scoring reference

This is the developer-facing source of truth for the shared backend used by both `智能剪辑`
and `专业剪辑`. The customer UI remains concise; this document exposes the decisions, formulas,
fallbacks, and present limitations under the hood.

## 1. Non-negotiable batch contract

These are assertions, not taste scores. A candidate that violates them is rejected before it
can be rendered.

| Contract | Executable rule | Example |
| --- | --- | --- |
| One narrative per output | Every timeline clip has the assigned source media ID | 5 inputs + 10 outputs = 2 outputs per input, never 10 mixed montages |
| Source balance | Round-robin when outputs ≥ sources; evenly spaced source selection when outputs < sources | 5 inputs + 100 outputs = 20 each |
| Chronology | `clip[i].start + duration <= clip[i+1].start` | An output may skip forward, but never silently jump backward |
| Non-empty output | At least one picture clip must be planned | Analysis/planning failure is explicit, not a blank render |
| Explicit loop exception | Backward picture reuse is allowed only when narration is longer than all available footage, with a `画面循环` warning | Voiceover integrity takes precedence and the exception is visible |

The deprecated `recording_scope` and `start_rotation` fields are retained for API compatibility,
but batch planning records them at `all` and `0`. They no longer mix recordings or rotate a
route out of chronological order.

## 2. The two UI modes share one planner

| Customer mode | Customer chooses | Backend resolves | Backend does not duplicate |
| --- | --- | --- | --- |
| 智能剪辑 | 智能推荐 / 完整展示 / 动感巡游 / 沉浸参观 | A coherent family, source assignment, pace, contour, footage lean, point scope, music window, four candidates | Analysis, timeline placement, scoring, selection and rendering are the same services |
| 专业剪辑 | 剪辑节奏、节奏起伏、素材侧重、时长取舍、点位取用 | Pinned values plus automatic values dealt per assigned source's evidence | It calls the same batch endpoint and planner; it is not the old backend copied beside the new one |

Missing JSON or music does not change the interface. The dimension that lacks evidence becomes
neutral, and the job continues with visible capability/fallback diagnostics.

## 3. Source and point evidence

| Evidence | What is trusted | What is inferred/fallback |
| --- | --- | --- |
| Full capture sidecar spans | Transit/dwell, arrival/failure, point label, semantic boundary | PySceneDetect subdivides long spans; pixel metrics measure quality |
| Marker-only sidecar | Point label and marker time | Pixel scenes fill intervals between markers |
| Invalid/no sidecar | Nothing is claimed about points | Pixel scenes and technical quality only; point controls become neutral |

Robot boundaries and visual boundaries are different evidence. A sidecar marker does not receive
a fabricated visual-boundary score. PySceneDetect confidence survives semantic subdivision only
when the merged interval actually starts at that detected cut.

## 4. Video analysis and placement formula

PySceneDetect's `AdaptiveDetector` runs through the PyAV backend. Edge change carries weight
`0.5`; the minimum detected scene length is `0.6 s`. Every detected/robot-delimited scene is
then measured in local windows no longer than `12 s`, so different moments in a long continuous
robot take can score differently.

Three neighbouring-frame samples per window are decoded at a 320-pixel working width. The
technical terms are all in `[0,1]`:

| Term | Formula/normalisation | Meaning |
| --- | --- | --- |
| Sharpness `H` | smoothstep of `Laplacian variance / best in recording`, from `0.18` to `0.55` | Relative focus/blur |
| Exposure `E` | `1 - smoothstep(clipped_pixels, allowance, allowance + 0.30)`; allowance = `max(0.02, 1.5 × median)` | Worse-than-usual crushed/blown pixels |
| Motion `M` | trapezoid: 0 at `0.5`, 1 from `1.2..20`, 0 by `40` mean blurred-frame difference | Penalises dead and violent footage, not motion itself |
| Steadiness `T` | `1 - smoothstep(global_frame_shift, 1.6 px, 6.0 px)` | Camera shake rather than subject motion |

The local usability score is a softened geometric mean:

```text
Q = max(0.05,
        max(0.30,H)^0.30 × max(0.30,E)^0.20 ×
        max(0.30,M)^0.25 × max(0.30,T)^0.25)
```

No interval is removed solely by an uncalibrated measurement. Its allocation worth is:

```text
worth = seconds × max(0.15, Q) × footage_kind_multiplier
```

`dwell/transit` multipliers are `3/1` for 多停留, `2/2` for 均衡, and `1/3` for 多行进;
failed/skipped spans remain possible at `0.3..0.5` rather than becoming an infeasible hard ban.
Whole cuts are allocated by largest remainder, then placed forward inside each local window.

## 5. Pace, contour, and point capacity

| Professional control | Backend field | Exact effect |
| --- | --- | --- |
| 快切 / 常规 / 慢镜 | `pace` | Mean cut target `2 / 5 / 8 s`; cut count is `round(T / mean)` |
| 平稳 / 渐快 / 渐慢 / 弧线 | `contour` | Relative duration shape: constant, `1.6→0.4`, `0.4→1.6`, or slow-fast-slow; durations are normalised to sum to `T` |
| 跟音乐 | `follow_energy` | Loud windows shorten cuts and quiet windows lengthen them; absent/unreliable structure falls back safely |
| 按比例 | `target` | Cut share follows usable footage worth |
| 保覆盖 | `coverage` | Equal cut share per point, capped at two cuts per readable place |
| 自动 / 全部点位 | `point_scope` | Scope is sampled across the route but widened when necessary to fill the target; `all` still respects what the cut count can visibly cover |

Beat snapping moves internal cut edges to the nearest allowed event without starving either
neighbour below `0.6 s`, and never lets a cut exceed the longest available source window.

## 6. Music analysis and excerpt selection

Librosa produces tempo, beats, beat reliability, note onsets, strong separated accents, RMS
energy, and section candidates. Section clustering uses median beat-synchronous chroma + 13
MFCC bands with agglomerative segmentation. Sparse/ambient material falls back to the raw
feature clock and is not allowed to pretend it has a reliable beat grid.

Strong accents are onset candidates in the upper 30% of positive onset strength, separated by
at least `0.18 s`. Dynamic edits may use `beats ∪ accents`; other families use beats only.

For a music window `w`, define boundary closeness `B`, beat density `D`, strong-accent density
`A`, mean onset punch `P`, energy range `R`, and beat reliability `L`. Window scores are:

```text
dynamic   = .25B + .18D + .17A + .20P + .12R + .08L
immersive = .42B + .17(1-D) + .12(1-A) + .15(1-R) + .08(1-P) + .06L
general   = .40B + .18D + .12A + .10P + .08R + .12L
```

Long tracks are cropped from ranked section-aligned windows. Short tracks loop explicitly.
The renderer receives the exact selected start and duration; the whole song is never squeezed
conceptually into the output's shorter clock.

## 7. Narration duration and point-description alignment

Narration has two independent effects. Its duration protects speech integrity, while its text
may guide the picture only when the assigned recording has explicit point descriptions.

```text
A = sum(all selectable seconds in the assigned recording)
P = min(requested_seconds, A)
T = max(P, narration_seconds)       # when narration exists
```

The point scope is calculated using `T`, not the original request. Therefore an available point
cannot be excluded by a 30-second scope and then cause a false loop when a 45-second narration
is discovered. If `narration_seconds > A`, the planner makes the documented picture-loop
exception; otherwise source chronology remains hard-enforced.

The semantic layer consumes only:

1. explicit `点位N：描述` entries in the assigned recording's `.capture.json`;
2. the selected voiceover's saved text and provider word timestamps;
3. a local CPU embedding similarity matrix from the bundled INT8 BGE model.

For narration unit `i` and point description `j`:

```text
score(i,j) = .90 × cosine_bge(i,j) + .10 × lexical_bigram(i,j)
accept when score >= .62
```

If ONNX inference is unavailable, lexical matching alone uses a `.46` threshold. A Viterbi path
maximises `Σ(score - threshold)` while allowing `none` and requiring assigned point indices to
be nondecreasing. This keeps uncertain language neutral and prevents a narration from making
the route run backwards.

Accepted chapters constrain the existing selector; they do not replace it. Cut rhythm,
quality worth, footage mix, beat snapping, and within-point placement keep the formulas in
sections 4–6. If capacity or chronology cannot satisfy the entire semantic schedule, the
candidate is replanned with the unchanged base algorithm. Diagnostics retain evidence type,
matched seconds/ratio, point descriptions, chapter scores, whether it was applied, and the
fallback reason.

## 8. Candidate timeline score

Each requested smart output plans four timelines and renders only one. For every candidate:

```text
final_score = 0.90 × family_weighted_score + 0.10 × duration_accuracy
duration_accuracy = max(0, 1 - |planned_seconds - target_seconds| / target_seconds)
```

All other components are `[0,1]`:

| Component | Exact meaning |
| --- | --- |
| Coverage `C` | used point labels / available point labels; neutral 1 without point evidence |
| Technical `Q̄` | clip-duration-weighted local usability score |
| Temporal flow `F` | if chronological, `0.5 + 0.5 × mean(exp(-gap / (3 × adjacent_mean_cut)))`; otherwise 0 |
| Non-repetition `N` | `.5 × unique source intervals + .5 × perceptual-hash cluster ratio`; hashes within 8 bits share a cluster |
| Family fidelity `Y` | direction-specific point/motion/pacing formula below |
| Music alignment `U` | mean `exp(-(nearest_event_distance / .14)^2)` at cut edges; neutral `.5` without reliable structure |
| Visual flow `V` | adjacent mean of `.7 × exp(-Lab_distance/45) + .3 × (1-|motion_delta|)` |
| Cut stability `K` | `.65 × local steadiness + .35 × real boundary confidence`; local steadiness substitutes when not at a real cut |

Family fidelity is:

```text
showcase  Y = .50C + .30(dwell_share) + .20F
dynamic   Y = .40(transit_share) + .35 exp(-|mean_cut-2.5|/3) + .25(activity)
immersive Y = .45F + .35 exp(-|mean_cut-7|/5) + .20(steadiness)
```

The outer family weights `(C,Q̄,F,N,Y,U,V,K)` are:

| Family | C | Q̄ | F | N | Y | U | V | K |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 完整展示 | .25 | .18 | .10 | .10 | .12 | .05 | .12 | .08 |
| 动感巡游 | .08 | .16 | .07 | .14 | .18 | .18 | .09 | .10 |
| 沉浸参观 | .12 | .18 | .18 | .08 | .18 | .04 | .14 | .08 |

## 9. Portfolio selection and 100-output behaviour

Within each output slot, candidates no worse than `0.15` below the best base score remain
eligible. Exact duplicates are avoided whenever an alternative exists. The selected candidate
maximises:

```text
0.75 × candidate_score + 0.25 × minimum_diversity_to_already_selected
```

```text
diversity = .45 interval difference + .20 point-set difference +
            .15 opening difference + .10 family difference + .10 music-window difference
```

For 100 requested outputs the smart planner builds 400 cheap timelines, selects/renders 100,
and analyses each source/music file once per backend process. With five source recordings, the
ownership is exactly 20 outputs each. This is candidate planning, not 400 video renders.

## 10. Capability limits and honest fallbacks

| Area | Current level | What is deliberately not claimed |
| --- | --- | --- |
| Source/timeline integrity | Hard-enforced | The explicit long-voiceover picture loop is the documented exception |
| PySceneDetect use | Strong for shot boundaries and boundary evidence | It does not understand composition, products, faces, or narrative importance |
| Librosa use | Strong for beat/rhythm/dynamics and useful coarse structure | Clusters are not named verse/chorus; no lyrics, genre intent, or semantic mood matching |
| Pixel maths | Good technical usability and visual continuity priors | Relative thresholds are not camera-calibrated aesthetic judgement |
| Point JSON | Strong when the robot provides truthful spans | Missing metadata cannot be reconstructed as factual arrivals from pixels |
| Point-description matching | Local, monotonic, source-aware semantic guidance | It does not invent descriptions, infer unlabeled places, or override chronology/quality rules |
| Portfolio diversity | Deterministic, explainable, source-aware | Four candidates per output is bounded search, not a global optimum |
| Commercial aesthetic quality | Improving but unproven | Without acceptance labels, A/B tests, or human ratings, no numeric weight can be called commercially optimal |

The packaged backend adds the small INT8 `bge-small-zh-v1.5` ONNX model and CPU-only runtime.
Both are lazy and optional at execution time: missing or failed native inference is recorded as
semantic fallback and never blocks analysis, planning, rendering, or the existing no-note path.
