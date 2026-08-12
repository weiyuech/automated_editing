# Review, Publish, and Seedance 2 Requirements

This document captures high-level product requirements only. It does not describe an implemented feature yet.

## In-App Video Viewing

- The app should support viewing videos directly inside the desktop UI instead of requiring users to open the local file manually.
- Users should be able to open a video from the media library, generated exports, robot-synced media, or a drag-and-drop area.
- The viewer should support playback, pause, seek, current timestamp display, duration display, and basic preview controls.
- Large or unsupported videos may require proxy generation or backend streaming with range requests for reliable seeking.

## Automation Modes

The system should support two high-level review modes:

- **Fully automated mode**: the app can audit, apply planned edits/effects, render, and optionally publish with minimal human intervention.
- **Human-check mode**: the app can audit and generate proposed edits/effects, but a human reviewer must inspect, adjust, approve, and publish.

These modes should be configurable at the workflow level, and human-check mode should be available per video or per batch.

## Review Workspace

The app should include a dedicated client-facing area for reviewed or auto-audited videos. This area should support:

- Dragging any video into the workspace for direct preview.
- Selecting media from the media library or robot-synced captures.
- Viewing audit results as timestamped markers.
- Clicking a timestamp to jump the video player to that moment.
- Editing generated stamps manually.
- Approving, rejecting, or adjusting automated recommendations.
- Publishing only after human approval when the workflow requires it.

## Timeline and Stamp Editing

For human review, the app should provide a lightweight editing timeline:

- Timestamp markers for audit findings, suggested cuts, captions, or effects.
- Span-based edits where an effect or action can cover a start and end time.
- Ability to drag or adjust spans.
- Ability to reorder effects/actions attached to a timestamp or span.
- Ability to remove effects/actions.
- Clear visual state for pending, edited, approved, and rejected timeline items.

## Seedance 2 Effects

Seedance 2 should be supported in both automated and human-check workflows.

- In automated mode, audit/planning can propose and apply Seedance 2 effects based on rules or model output.
- In human-check mode, reviewers should be able to click a timestamp or span and choose **Add Seedance 2 effect**.
- Seedance 2 effects should be stored as structured timeline actions, not only as rendered pixels, so reviewers can reorder or remove them before final render.
- Each Seedance 2 action should keep its prompt/config, target timestamp or span, source media reference, status, generated output reference, and error state.
- Effects should be rendered asynchronously and previewable before final publishing.

## Publish Flow

Publishing should be treated as a separate final step after render/review.

- Fully automated workflows may publish automatically only when configured to do so.
- Human-check workflows should require explicit approval.
- The publish area should show source video, final render, applied effects, audit status, and publish readiness.

## Implementation Difficulty Notes

- In-app playback for common local MP4/H.264 files is low to medium difficulty.
- Reliable playback for arbitrary MOV/HEVC/large robot files is medium difficulty and may need preview proxies or backend range streaming.
- Timestamp and span editing is medium to high difficulty because it needs a durable timeline data model and careful UI interaction.
- Seedance 2 integration is high difficulty until the exact API, cost, latency, output format, and compositing rules are confirmed.
- Publishing difficulty depends on the destination. Local export is medium; platform publishing requires separate authentication, review, retry, and status tracking.
