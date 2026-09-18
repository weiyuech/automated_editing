> 版本说明：本文保留改版前的设计记录。当前已改为固定镜头、树形精确拼接和显式旁白映射；旧自动选片、节拍与语义匹配规则不再执行。当前操作与实现以 [根 README](../README.md) 和 [改版说明](controlled_capture_concat_plan.md) 为准。

# Automatic editorial planner v2

This document is the developer view of the four customer-facing choices in 智能剪辑. 专业剪辑
exposes pace, contour, point share and coverage as concise policy choices, but both modes call
the same planner. Seeds, candidate count and diversity selection remain internal. Every resolved
value is saved in the job timeline and `logs/batch-plan-<seed>.json`.

## Customer contract

The interface is unchanged whether footage has a capture sidecar and whether music exists.
It reports the evidence it recognised, then falls back without blocking the job:

| Evidence | Used when available | Fallback |
| --- | --- | --- |
| Full point spans | Transit/dwell, arrival outcome, point coverage | Pixel scenes |
| Marker-only points | Point identity and boundaries | Pixel scenes within each range |
| No/invalid sidecar | Nothing is inferred about places | Pixel scenes and technical quality |
| Point descriptions in 画面匹配备注 | Narration subjects may constrain corresponding point footage | Existing chronological picture selection |
| Structured music | Sections, beats, onsets, energy, tempo reliability | — |
| Ambient/unreadable/no music | Music may still be mixed when readable | Picture-driven contour; no beat snapping |

Each output belongs to exactly one source recording. Sources are dealt round-robin when the
requested output count is at least the source count, so five recordings and ten outputs means
two outputs per recording. A batch never shuffles recordings together inside one timeline.
Within an output, source intervals remain chronological; point order is not secretly rotated.

The four directions are coherent policy regions, not aliases for one hardcoded setting:

- `smart`: distributes a batch across feasible families; the first result is the safest.
- `showcase`: complete coverage, dwell/balanced material, normal-to-cinematic pacing.
- `dynamic`: transit/balanced material, fast-to-normal pacing, strongest musical accents.
- `immersive`: route order, longer stable shots, flat/decelerating/arc shape.

`None` remains the legacy API meaning. Old clients can still submit the previous axis lists and
receive the previous batch dealer. The new UI always sends one of the four values.

## Analysis

Video analysis is cached by video size, modification time, and sidecar modification time.
PySceneDetect uses `AdaptiveDetector` with PyAV and stores its content, colour-component, edge,
and adaptive-ratio metrics at every accepted boundary. Robot/marker subdivision preserves those
fields only where a real detector boundary exists. Each detected or robot-delimited span is
measured in local windows of at most 12 seconds and stores:

`quality, sharpness, exposure, motion, steadiness, Lab colour, perceptual hash`.

The local profile matters: four candidates drawing different moments from one long continuous
take now receive different evidence. The profile also reaches slot allocation, so high-quality
parts are more likely to receive cuts without becoming a hard exclusion rule.

Music is loaded once per file and cached by path, size, and modification time. Librosa supplies:

`duration, tempo, beats, beat reliability, onsets, strong accents, onset strength, RMS energy,
section boundaries`.

Section clustering uses beat-synchronous chroma plus MFCC features. Dynamic edits may add only
the strongest separated onsets to the beat grid; they no longer treat every detected note
attack as an equally important cut point.

Long songs are never compressed conceptually onto a short output. The planner ranks windows
whose starts/ends lie on detected section boundaries, and the renderer trims the exact selected
window. Short tracks loop explicitly and are trimmed to the target duration.

## Narration, notes, and semantic alignment

`画面匹配备注` and `旁白文案` have separate trust boundaries:

- the LLM receives only text deliberately entered in 旁白制作;
- capture notes remain in that recording's `.capture.json` and are never added to speech;
- only explicit mappings such as `点位1：产品展示区` are semantic evidence; bare prose is not
  assigned to a point by guesswork;
- generated voiceover text and word timestamps provide the narration clock.

The optional local `bge-small-zh-v1.5` INT8 ONNX model computes only text similarity. It does
not select cut duration, point order, quality, or source time. Narration is divided at provider
word timings (sentence length is a fallback), similarity is thresholded, then a monotonic
dynamic-programming assignment permits skipped/neutral sentences but forbids walking backwards
through robot points. Consecutive matches become semantic chapters.

Each matched chapter is a constraint around the existing placement machinery: the established
global rhythm is split at chapter boundaries, the required point is included in scope, and the
normal quality, footage-mix, pace, and forward-only rules select its clips. The rest of the
output uses ordinary remaining forward footage. If any chapter lacks enough chronological
footage, the complete semantic attempt is discarded and the unmodified base planner runs from
the same random state. Missing notes, TTS metadata, model assets, or a confident match therefore
cannot prevent an edit.

Duration is decided from the complete assigned recording before point scope:

```text
picture_target = min(requested_seconds, available_source_seconds)
output_seconds = max(picture_target, narration_seconds)
```

Thus a 3-second narration with a 30-second request still produces 30 seconds and leaves a
27-second picture-led tail. A 45-second narration with at least 45 seconds of unused footage
extends the output to 45 seconds and widens scope before planning. Picture looping is permitted
only when the narration itself exceeds all footage in the assigned recording.

For batches, source ownership remains balanced first. The voiceover deck remains balanced too,
then pairwise swaps improve source/voiceover compatibility without changing how often any
selected narration is used. One semantic alignment is computed per output slot and reused by
that slot's four candidates; it is not four model passes.

## Candidate portfolio

For `N` requested outputs the planner creates four cheap timelines per output and renders only
the `N` winners. A candidate's base score is:

`S = 0.9 * weighted_family_score + 0.1 * duration_accuracy`

The family weights combine these 0..1 terms:

- point coverage;
- technical quality, duration-weighted;
- chronological temporal flow (smaller forward gaps score higher);
- non-repetition from source intervals and perceptual hashes;
- family fidelity (coverage/dwell, transit/pace, or continuity/long-shot fit);
- cut-to-beat alignment, only for reliable structured music;
- visual flow from adjacent Lab colour distance and motion similarity;
- cut stability from shot steadiness and PySceneDetect boundary confidence.

Before scoring, two hard conditions are asserted: every clip must belong to the output's
assigned source, and clips must move forward in source time. The only legacy exception is an
explicitly reported picture loop when narration is longer than all available footage.

Selection keeps candidates within `0.15` of the best base score. From that quality band it
prefers the candidate maximising:

`0.75 * S + 0.25 * minimum_diversity_to_any_selected_output`

Diversity is:

`0.45 source intervals + 0.20 point set + 0.15 opening + 0.10 family + 0.10 music window`

An exact duplicate is rejected whenever a non-duplicate candidate exists. This is portfolio
selection, not ten independent random jobs; the batch manifest includes selected/rejected
candidates, seeds, policies, score components, music window, and clip intervals.

## What this does not claim

These scores are deterministic engineering priors, not learned taste. With no human ratings or
A/B data, the system cannot truthfully call one aesthetic globally optimal. The diagnostics are
there so future acceptance/re-render signals can calibrate weights without changing the simple
customer UI.
