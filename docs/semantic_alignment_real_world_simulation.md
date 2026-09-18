> 版本说明：本文保留改版前的设计记录。当前已改为固定镜头、树形精确拼接和显式旁白映射；旧自动选片、节拍与语义匹配规则不再执行。当前操作与实现以 [根 README](../README.md) 和 [改版说明](controlled_capture_concat_plan.md) 为准。

# Capture-note semantic alignment: real-world simulation

This is an executable integration scenario, not a hand-written example of the desired output.
The tests load the bundled INT8 BGE model, parse realistic robot and TTS JSON, and invoke the
production timeline planner.

## Inputs

- `backend/tests/fixtures/semantic/complex_robot_recording.capture.json`
  - abstract robot path `cs2`;
  - four arrived points;
  - point 3 fails object alignment, then succeeds on a second attempt;
  - recording notes mix camera/noise observations with explicit point descriptions;
  - point 2 receives a later supplementary description.
- `backend/tests/fixtures/semantic/complex_tts_metadata.json`
  - 27 seconds of provider-style word timing with natural pauses;
  - a neutral introduction;
  - paraphrases rather than copied point descriptions;
  - display → production → warehouse content;
  - a final sentence about reception, which would require returning from point 4 to point 1.

The simulated source contains 95 seconds of chronological point footage. The requested output
is 38 seconds, so eleven seconds after narration must remain picture-led rather than shortening
the result to the TTS length.

## Observed model alignment

| Narration clock | Meaning | Result | Mixed score |
| --- | --- | --- | ---: |
| 0.00–3.80 | General introduction | Neutral | — |
| 3.80–8.45 | Solutions and samples | `cs2#2` display | 0.6524 |
| 8.45–14.60 | Robot-arm assembly and inspection | `cs2#3` production | 0.6690 |
| 14.60–20.05 | Packed goods waiting to ship | `cs2#4` warehouse | 0.7242 |
| 20.05–27.00 | Registration at the earlier reception desk | Neutral | — |

The last sentence has a confident relationship with point 1, but the monotonic assignment
rejects it because point 1 follows point 4 in speech while it precedes point 4 in the recording.
This preserves source chronology instead of satisfying text similarity by making a visible jump
backwards. The matched share is 16.25/27 seconds (`0.6019`).

## Timeline result

- semantic schedule applied: **yes**;
- output duration: **38.0 seconds**;
- source times: strictly increasing;
- point order: `cs2#2 → cs2#3 → cs2#4`;
- failed first attempt at point 3 selected: **no**;
- return to `cs2#1`: **no**;
- picture-led tail after narration: **11 seconds**;
- duplicate customer warning: **no**.

The test initially exposed a real bug: normal within-point placement spread the final matched
warehouse chapter to the end of all warehouse footage, leaving no forward material for the
eleven-second tail and causing a false semantic fallback. The planner now reserves the raw
seconds required by later chapters before spreading an intermediate semantic chapter. The final
chapter still uses normal seeded spreading.

## Scope of this simulation

This verifies capture-sidecar parsing, TTS timing segmentation, real local embedding inference,
monotonic matching, semantic timeline constraints, duration integrity, and fallback invariants.
The audio/video files themselves are placeholder containers because these layers consume the
JSON and already-analysed scene spans. Frame decoding, camera quality scoring, audio decoding,
and final FFmpeg rendering remain covered by their own media tests and require genuine media for
a perceptual end-to-end review.
