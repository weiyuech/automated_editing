from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RobotMode(StrEnum):
    REAL = "real"


class RobotState(BaseModel):
    connected: bool = False
    adapter: RobotMode = RobotMode.REAL
    connection_status: Literal[
        "disconnected",
        "connecting",
        "connected",
        "reconnecting",
        "error",
    ] = "disconnected"
    moving: bool = False
    recording: bool = False
    camera_angle: float = 0.0
    battery: int | None = None
    # "ready" or "error" from the heartbeat's system block. An error means the hardware itself
    # has faulted, which was previously read past entirely — the app showed a healthy robot.
    system_status: str | None = None
    map_name: str | None = None
    # "localization" or "mapping". A robot building a map cannot navigate, so goals sent in
    # that mode fail with nothing to explain why.
    map_mode: str | None = None
    map_status: str | None = None
    navigation_status: str | None = None
    goal_status: str | None = None
    object_status: str | None = None
    path_file: str | None = None
    goal_id: int | None = None
    goal_object: str | None = None
    yaw: float | None = None
    pitch: float | None = None
    media_url: str | None = None
    media_local_path: str | None = None
    media_sync_error: str | None = None
    last_command: str | None = None
    error: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class MoveCommand(BaseModel):
    direction: Literal["forward", "backward", "left", "right", "rotate_left", "rotate_right"]
    speed: float = Field(default=0.3, ge=0, le=1)
    duration_ms: int = Field(default=500, ge=50, le=10000)


class CameraAngle(BaseModel):
    angle: float = Field(ge=-90, le=90)


class RobotGoalCommand(BaseModel):
    path_name: str = Field(min_length=1, max_length=200)
    goal_id: int = Field(ge=0)
    # Optional: a point with no goal_object is navigation-only, and the robot is not
    # asked to align the gimbal on arrival.
    goal_object: str | None = Field(default=None, max_length=200)


class TimelineMarker(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: float = Field(ge=0)
    label: str = ""


class CaptureSession(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    title: str = "Untitled capture"
    active: bool = False
    started_at: datetime | None = None
    ended_at: datetime | None = None
    notes: list[str] = Field(default_factory=list)
    markers: list[TimelineMarker] = Field(default_factory=list)


class GimbalScanConfig(BaseModel):
    """Optional slow pan-and-return, performed only while the robot is parked at a point.

    Off by default. Navigation and gimbal control are independent commands, so enabling
    this does not change how the cruise drives, and manual camera control stays usable
    throughout a run.
    """

    enabled: bool = False
    direction: Literal["left", "right"] = "right"
    yaw_offset_deg: float = Field(default=15.0, ge=1.0, le=60.0)
    yaw_speed_deg_s: float = Field(default=5.0, ge=1.0, le=30.0)
    settle_tolerance_deg: float = Field(default=2.0, ge=0.1, le=15.0)

    @property
    def signed_offset_deg(self) -> float:
        return self.yaw_offset_deg if self.direction == "right" else -self.yaw_offset_deg

    @property
    def leg_budget_seconds(self) -> float:
        """Worst-case time for one leg: the pure travel time plus settle margin."""
        return self.yaw_offset_deg / self.yaw_speed_deg_s + 0.8

    @property
    def budget_seconds(self) -> float:
        """Worst-case time for out-and-back, used as a floor on dwell so a scan is
        never cut off mid-return."""
        return 2 * self.leg_budget_seconds


class CruisePoint(BaseModel):
    path_name: str = Field(min_length=1, max_length=200)
    goal_id: int = Field(ge=0)
    goal_object: str | None = Field(default=None, max_length=200)


class CruiseRequest(BaseModel):
    title: str = "Cruise capture"
    map_name: str | None = Field(default=None, max_length=200)
    points: list[CruisePoint] = Field(min_length=1, max_length=200)
    record: bool = True
    dwell_min_seconds: float = Field(default=5.0, ge=0.0, le=120.0)
    dwell_max_seconds: float = Field(default=10.0, ge=0.0, le=120.0)
    arrival_timeout_seconds: float = Field(default=180.0, ge=5.0, le=1800.0)
    gimbal_scan: GimbalScanConfig = Field(default_factory=GimbalScanConfig)

    @model_validator(mode="after")
    def validate_dwell_range(self) -> CruiseRequest:
        if self.dwell_max_seconds < self.dwell_min_seconds:
            raise ValueError("dwell_max_seconds must be >= dwell_min_seconds")
        return self


class CruiseSegment(BaseModel):
    index: int = Field(ge=0)
    path_name: str
    goal_id: int
    goal_object: str | None = None
    status: Literal["pending", "navigating", "arrived", "failed", "skipped"] = "pending"
    transit_start_seconds: float | None = None
    arrived_at_seconds: float | None = None
    departed_at_seconds: float | None = None
    dwell_seconds: float | None = None
    scanned: bool = False
    error: str | None = None


class CruiseRun(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    title: str = "Cruise capture"
    status: Literal["running", "succeeded", "failed", "canceled"] = "running"
    map_name: str | None = None
    recording: bool = False
    capture_session_id: str | None = None
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    segments: list[CruiseSegment] = Field(default_factory=list)
    markers: list[TimelineMarker] = Field(default_factory=list)
    media_url: str | None = None
    media_local_path: str | None = None
    # Things that went wrong without failing the run. Losing the point spans is the one that
    # matters: the recording is fine and the editor will treat it as ordinary footage, which
    # is a large, invisible downgrade unless somebody is told.
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None


class CruiseRoute(BaseModel):
    """A named, reusable cruise setup.

    goal_id cannot be enumerated from the robot, so a saved route is the only durable
    record of which ids are real on a given path.
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = Field(min_length=1, max_length=100)
    request: CruiseRequest
    created_at: datetime = Field(default_factory=utc_now)
    last_used_at: datetime = Field(default_factory=utc_now)


class CruiseRouteSaveRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    request: CruiseRequest


class CruiseRouteIssue(BaseModel):
    level: Literal["error", "warning"]
    field: Literal["robot", "map_name", "path_name"]
    value: str = ""
    message: str
    point_indexes: list[int] = Field(default_factory=list)


class CruiseRouteValidation(BaseModel):
    route_id: str | None = None
    checked: bool = True
    issues: list[CruiseRouteIssue] = Field(default_factory=list)

    @computed_field
    @property
    def ok(self) -> bool:
        return not any(issue.level == "error" for issue in self.issues)


class MediaItem(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    path: str
    kind: Literal["video", "audio", "image", "unknown"] = "unknown"
    created_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AnalysisResult(BaseModel):
    media_id: str
    scenes: list[dict[str, Any]] = Field(default_factory=list)
    beats: list[float] = Field(default_factory=list)
    # Loudness across the music on 0..1, sampled evenly. Lets cut length follow the track
    # instead of a fixed curve. Empty whenever there is no music to read.
    energy: list[float] = Field(default_factory=list)
    # Rich music data is optional so timelines and cached analyses written by older builds
    # still load.  New planning keeps it separate from the video measurements conceptually,
    # but carrying it here preserves the public AnalysisResult contract used by drafts/tests.
    music_duration_seconds: float = Field(default=0.0, ge=0)
    tempo_bpm: float | None = Field(default=None, ge=0)
    onset_times: list[float] = Field(default_factory=list)
    # The strongest subset of onset_times. Dynamic edits may cut on these accents; using every
    # detected note onset made the grid far denser than the music a listener perceives as the
    # beat and let weak events pull cuts away from strong ones.
    accent_times: list[float] = Field(default_factory=list)
    onset_strength: list[float] = Field(default_factory=list)
    section_boundaries: list[float] = Field(default_factory=list)
    beat_reliability: float = Field(default=0.0, ge=0, le=1)
    music_evidence: Literal["none", "structured", "ambient", "unreadable"] = "none"
    warnings: list[str] = Field(default_factory=list)


class MusicAnalysis(BaseModel):
    """Reusable full-track analysis before one exact render window is selected."""

    duration_seconds: float = Field(default=0.0, ge=0)
    tempo_bpm: float | None = Field(default=None, ge=0)
    beats: list[float] = Field(default_factory=list)
    onset_times: list[float] = Field(default_factory=list)
    accent_times: list[float] = Field(default_factory=list)
    onset_strength: list[float] = Field(default_factory=list)
    # Evenly sampled across ``duration_seconds`` and kept at a higher resolution than the old
    # 64 buckets so a 30-second excerpt can be cut out of a long song without time-warping it.
    energy: list[float] = Field(default_factory=list)
    section_boundaries: list[float] = Field(default_factory=list)
    beat_reliability: float = Field(default=0.0, ge=0, le=1)
    evidence: Literal["structured", "ambient", "unreadable"] = "unreadable"


EditorialPreset = Literal["smart", "showcase", "dynamic", "immersive"]


# How an edit leans on the footage the robot classified. Parked shots are not automatically
# the better ones — a glide down an aisle is often the most watchable thing in a run — so
# these are leanings across a batch, not a ranking that settles the question once.
FootageMix = Literal["dwell_heavy", "balanced", "transit_heavy"]
FOOTAGE_MIX_LEVELS: tuple[FootageMix, ...] = ("dwell_heavy", "balanced", "transit_heavy")

# What the edit spends its seconds on when it cannot do both: showing every point at least
# once, or giving the points it does show room to breathe.
EditEmphasis = Literal["target", "coverage"]
EDIT_EMPHASIS_LEVELS: tuple[EditEmphasis, ...] = ("target", "coverage")

# Mean seconds per cut. This one governs: it sets how many cuts an edit has, and therefore how
# many places it can show without glimpsing them. See docs/edit_dimension_space.md §3b.
EditPace = Literal["fast", "normal", "cinematic"]
EDIT_PACE_LEVELS: tuple[EditPace, ...] = ("fast", "normal", "cinematic")

# How cut length varies across the edit. A constant pace is only one of these, and the least
# interesting; real edits are shaped. `follow_energy` takes its shape from the music and falls
# back to `arc` without any.
EditContour = Literal["flat", "accelerate", "decelerate", "arc", "follow_energy"]
EDIT_CONTOUR_LEVELS: tuple[EditContour, ...] = ("flat", "accelerate", "decelerate", "arc", "follow_energy")

# How much of what is available an edit uses: a fraction of what the pace can carry, or "all"
# to stop narrowing entirely.
#
# A fraction rather than a named level because `k = round(f · k_cap)` is a whole number of
# places — the fraction only matters through the integer it lands on, so naming two of them
# ("half", "two thirds") was false precision that spent barely a third of the usable range. The
# levels below span it instead, and collapse by themselves where a small `k_cap` cannot tell
# them apart.
#
# Never a fraction below a half — nobody picks twelve points to make a video out of two — and
# `all` is not dealt, being guaranteed once per batch instead.
EditScope = Literal["all"] | float
EDIT_SCOPE_LEVELS: tuple[float, ...] = (0.5, 2 / 3, 5 / 6, 1.0)
MIN_SCOPE_FRACTION = 0.5

# The export frame. Named because two places need to agree on it: the renderer crops to it, and
# the subtitle layer sizes itself from it.
DEFAULT_OUTPUT_WIDTH = 1280
DEFAULT_OUTPUT_HEIGHT = 720
OutputAspectRatio = Literal["16:9", "9:16"]


class EditJobRequest(BaseModel):
    title: str = "Untitled edit"
    media_ids: list[str] = Field(default_factory=list)
    music_media_id: str | None = None
    voiceover_media_id: str | None = None
    output_name: str = ""
    target_duration_seconds: float = Field(default=30.0, ge=1.0, le=180.0)
    mute_original_audio: bool = True
    beat_sync: bool = True
    # None lets the app-wide framing preset decide. Once a job is accepted JobService resolves
    # this and stores the actual choice on the job, so changing the preset cannot alter a queued
    # or reproducible job later.
    output_aspect_ratio: OutputAspectRatio | None = None
    # Position within the source after the target canvas is fitted. 0 selects the left/top
    # edge, 1 the right/bottom edge, and 0.5 is the safe centred default.
    output_crop_x: float | None = Field(default=None, ge=0, le=1)
    output_crop_y: float | None = Field(default=None, ge=0, le=1)
    # Which cuts this particular output takes out of the same footage. Two jobs sharing a
    # seed render the same picture; None means the unshifted, wholly deterministic pick.
    variant_seed: int | None = Field(default=None, ge=0)
    # The batch this came from, carried so a whole day can be re-rolled, not just one output.
    batch_seed: int | None = Field(default=None, ge=0)
    # The only editorial decision exposed by the automatic UI.  The lower-level fields below
    # remain serialised for reproducibility and for opening jobs made by older app versions.
    editorial_preset: EditorialPreset | None = None
    # Which ranked musical excerpt this output uses. Internal portfolio metadata, not a UI
    # control; zero is the strongest window and later ranks create real batch variety.
    music_window_rank: int = Field(default=0, ge=0, le=999)
    # None means "not chosen" rather than "middle setting": the backend rolls one and writes
    # the result back here, so the job record always says which policy it actually used.
    footage_mix: FootageMix | None = None
    emphasis: EditEmphasis | None = None
    pace: EditPace | None = None
    contour: EditContour | None = None
    point_scope: EditScope | None = None
    recording_scope: EditScope | None = None
    # Which place opens the edit. 0 keeps the route in the order it was filmed.
    start_rotation: int | None = Field(default=None, ge=0)
    # Subtitles are drawn from the narration's own word timings, so there is nothing to subtitle
    # without a voiceover. Asking for them anyway is not an error — the job says so in its
    # warnings and renders without them.
    subtitles: bool = False
    subtitle_font: str = "noto_sans_sc"
    subtitle_size: Literal["small", "medium", "large"] = "medium"
    # A generated effect bumper attached to this one output, resolved by the batch from the
    # effect pools. The effect keeps its own audio; narration/music sit after an intro and
    # before an outro. Duration is on top of target_duration_seconds, not counted into it.
    intro_effect_media_id: str | None = None
    outro_effect_media_id: str | None = None
    # Off (default): narration/music sit around the effect, which keeps its own audio. On: the
    # narration/music cover it and the effect is treated like an ordinary input clip.
    effect_cover_audio: bool = False


class EditBatchRequest(BaseModel):
    title: str = "Automation batch"
    media_ids: list[str] = Field(default_factory=list, min_length=1, max_length=20)
    music_media_ids: list[str] = Field(default_factory=list, max_length=100)
    voiceover_media_ids: list[str] = Field(default_factory=list, max_length=100)
    output_count: int = Field(default=1, ge=1, le=100)
    target_duration_seconds: float = Field(default=30.0, ge=1.0, le=180.0)
    mute_original_audio: bool = True
    beat_sync: bool = True
    output_aspect_ratio: OutputAspectRatio | None = None
    output_crop_x: float | None = Field(default=None, ge=0, le=1)
    output_crop_y: float | None = Field(default=None, ge=0, le=1)
    # Left blank a fresh one is drawn and recorded on every job in the batch, so a day's
    # output can be reproduced or deliberately re-rolled rather than merely repeated.
    seed: int | None = Field(default=None, ge=0)
    editorial_preset: EditorialPreset | None = None
    # Whether the outputs of one batch differ in their picture, or only in their sound.
    #   per_output — every output takes its own cuts. Most variety.
    #   shared     — one seeded set of cuts across the batch, so music and narration are the
    #                only variables. This is how you A/B a soundtrack against a fixed edit.
    #   fixed      — the canonical unseeded cuts, identical to what every export rendered
    #                before seeding existed. Reproduces old work without knowing a seed.
    cut_variation: Literal["per_output", "shared", "fixed"] = "per_output"
    # Which editing policies this batch spreads its outputs across. Left empty, every level
    # is used and dealt evenly, which is the point: one recording should not yield a hundred
    # videos that all made the same editorial choices. Naming levels narrows the spread, and
    # naming exactly one pins that dimension for the whole batch.
    footage_mixes: list[FootageMix] = Field(default_factory=list)
    emphases: list[EditEmphasis] = Field(default_factory=list)
    paces: list[EditPace] = Field(default_factory=list)
    contours: list[EditContour] = Field(default_factory=list)
    point_scopes: list[EditScope] = Field(default_factory=list)
    recording_scopes: list[EditScope] = Field(default_factory=list)
    # Most places an edit may open on other than the first. Rotating further stops reading as
    # a different video, so this is a small number by design.
    start_rotations: int = Field(default=4, ge=1, le=12)
    # Applied to every output in the batch. Not a dimension the batch varies across: subtitles
    # are an accessibility and reach decision, not an editorial one, so spreading a batch over
    # "some with, some without" would be a strange thing to want.
    subtitles: bool = False
    subtitle_font: str = "noto_sans_sc"
    subtitle_size: Literal["small", "medium", "large"] = "medium"
    # 特效池: intro clips are attached at the start, outro clips at the end (a video may get
    # both). ``effect_scope`` decides which outputs are decorated — "all" gives every output a
    # bumper (reusing the pool), "auto" is scarce: the best-scored outputs in 智能剪辑, a random
    # subset in 专业剪辑. One effect per output per pool, no repeats within a scarce deal.
    intro_effect_media_ids: list[str] = Field(default_factory=list, max_length=100)
    outro_effect_media_ids: list[str] = Field(default_factory=list, max_length=100)
    effect_scope: Literal["auto", "all"] = "auto"
    # Off (default): narration/music sit around the effect, which keeps its own audio. On: the
    # narration/music cover it and the effect is treated like an ordinary input clip.
    effect_cover_audio: bool = False


class TimelineDraftRequest(BaseModel):
    title: str = "Timeline draft"
    media_ids: list[str] = Field(default_factory=list, min_length=1, max_length=20)
    music_media_id: str | None = None
    voiceover_media_id: str | None = None
    target_duration_seconds: float = Field(default=30.0, ge=1.0, le=180.0)
    mute_original_audio: bool = True
    beat_sync: bool = True
    output_aspect_ratio: OutputAspectRatio | None = None
    output_crop_x: float | None = Field(default=None, ge=0, le=1)
    output_crop_y: float | None = Field(default=None, ge=0, le=1)
    # A preview is only useful if it shows the cuts the job will actually render, so the
    # draft takes the same seed and the same editing policy.
    variant_seed: int | None = Field(default=None, ge=0)
    editorial_preset: EditorialPreset | None = None
    music_window_rank: int = Field(default=0, ge=0, le=999)
    footage_mix: FootageMix | None = None
    emphasis: EditEmphasis | None = None
    pace: EditPace | None = None
    contour: EditContour | None = None
    point_scope: EditScope | None = None
    recording_scope: EditScope | None = None
    start_rotation: int | None = Field(default=None, ge=0)
    subtitles: bool = False
    subtitle_font: str = "noto_sans_sc"
    subtitle_size: Literal["small", "medium", "large"] = "medium"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class JobRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    request: EditJobRequest
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0
    message: str = "Queued"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    result_path: str | None = None
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    timeline: Any | None = None


class TimelineClip(BaseModel):
    media_id: str
    source_path: str
    start: float
    duration: float
    timeline_start: float
    # A still has no length of its own, so it is held for `duration` instead of trimmed to it.
    kind: Literal["video", "image"] = "video"
    # Generated effects may carry useful sound. It is mixed as a separate, timeline-aligned
    # layer instead of becoming the soundtrack bed, so adding it cannot move narration or its
    # subtitles. Ordinary clips keep this off.
    include_audio: bool = False
    audio_volume: float = Field(default=1.0, ge=0.0, le=1.0)
    # What the robot was doing while this was filmed, when the footage came from a cruise.
    # "unknown" covers every ordinary import, where nothing recorded the camera's intent.
    footage: Literal["dwell", "transit", "failed", "skipped", "unknown"] = "unknown"
    # The cruise point this came from, e.g. "path1#3". Empty for non-cruise footage.
    label: str = ""


class TimelineAudioBed(BaseModel):
    """One continuous soundtrack laid under the picture, independent of the cuts.

    An export's music and narration were mixed into a single track when it rendered, so
    refining that export means carrying its audio as one unbroken piece. Taking audio from
    each clip instead would chop the sound at every cut, which is the thing 手动微调 must
    not do.
    """

    source_path: str
    # Where to start inside the source, so the sound keeps the relationship it was mixed with.
    source_start: float = Field(default=0.0, ge=0)
    # Where it begins in the output; non-zero only when something was added ahead of the picture.
    timeline_start: float = Field(default=0.0, ge=0)
    # Known narration in this bed makes generated effect sound background material. This is
    # metadata only: subtitle timing still comes exclusively from source_start/timeline_start.
    has_voiceover: bool = False


class SubtitleCue(BaseModel):
    """One subtitle line, timed against the voiceover — never against the timeline.

    This is the whole reason subtitles cannot drift out of step with the narration. The
    voiceover's position on the timeline is held once, in `EditTimeline.voiceover_start_seconds`,
    and applied to both the audio and these cues when the export is built. Storing absolute
    times here instead would make a second copy of that relationship, and moving the narration
    in 手动微调 would leave the text where it was.
    """

    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str


class SubtitleTrack(BaseModel):
    """The subtitle layer: a list of cues plus how they should look.

    A layer rather than something burned into the plan. It is serialised to an ASS script at
    render time, so re-timing it is a text rewrite — nothing is re-encoded, and the same track
    can be laid over any video.
    """

    cues: list[SubtitleCue] = Field(default_factory=list)
    font: str = "noto_sans_sc"
    # Fraction of the frame's short side. See services/subtitles.SubtitleStyle for why the short
    # side is the right thing to scale by when the frame may be landscape or portrait.
    size: float = Field(default=0.064, gt=0, le=0.3)
    side_margin: float = Field(default=0.07, ge=0, lt=0.45)
    bottom_margin: float = Field(default=0.085, ge=0, lt=0.9)
    # Both as a fraction of the font size, so they thicken with the text rather than thinning
    # away at large sizes. Defaults are set for bright, busy frames — see services/subtitles.py.
    outline: float = Field(default=0.09, ge=0, le=0.3)
    shadow: float = Field(default=0.045, ge=0, le=0.3)
    primary_colour: str = "FFFFFF"
    outline_colour: str = "000000"
    max_lines: int = Field(default=2, ge=1, le=4)


class EditTimeline(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    title: str
    clips: list[TimelineClip]
    music_path: str | None = None
    # The exact excerpt analysis planned against.  Rendering must trim to the same values or
    # beat/energy decisions describe music the audience never hears.
    music_start_seconds: float = Field(default=0.0, ge=0)
    music_duration_seconds: float | None = Field(default=None, gt=0)
    music_loop: bool = False
    music_evidence: Literal["none", "structured", "ambient", "unreadable"] = "none"
    editorial_preset: EditorialPreset = "smart"
    # Machine-readable evidence and resolved scores for developer diagnostics. This is never
    # used to render and remains deliberately open-ended across planner versions.
    planning_diagnostics: dict[str, Any] = Field(default_factory=dict)
    voiceover_path: str | None = None
    # Where the narration begins in the output. Zero today; it exists so that when 手动微调 moves
    # the voiceover, one field moves the audio and the subtitles together.
    voiceover_start_seconds: float = Field(default=0.0, ge=0)
    # How far to push the music bed back, so an intro effect plays before the music starts.
    # Zero on every ordinary timeline; set only when a 片头特效 is attached.
    music_delay_seconds: float = Field(default=0.0, ge=0)
    subtitles: SubtitleTrack | None = None
    # The export frame. Held here rather than hardcoded in the renderer because a vertical mode
    # is coming, and subtitle geometry is all expressed as fractions of these two numbers.
    output_width: int = Field(default=DEFAULT_OUTPUT_WIDTH, ge=16, le=7680)
    output_height: int = Field(default=DEFAULT_OUTPUT_HEIGHT, ge=16, le=7680)
    # `cover` is an explicit 16:9/9:16 crop. `contain` is the unset/original choice: keep the
    # first source's native canvas and never cut content off differently shaped later clips.
    output_fit: Literal["cover", "contain"] = "cover"
    output_crop_x: float = Field(default=0.5, ge=0, le=1)
    output_crop_y: float = Field(default=0.5, ge=0, le=1)
    target_duration_seconds: float = Field(default=30.0, ge=1.0, le=180.0)
    markers: list[TimelineMarker] = Field(default_factory=list)
    # 手动微调 lays one unbroken soundtrack under the picture. Effects are placed as ordinary
    # clips, so no separate effect-placement list is needed.
    audio_bed: TimelineAudioBed | None = None
    output_path: str
    mute_original_audio: bool = True
    beat_sync: bool = True
    # Set by the planner once it knows every source really carries an audio stream. Mixing a
    # clip that has none would fail the whole render, so the request's wish is downgraded here.
    include_original_audio: bool = False
    warnings: list[str] = Field(default_factory=list)
    format: Literal["internal-json"] = "internal-json"


class MediaAsset(BaseModel):
    id: str
    name: str
    path: str
    role: Literal["raw_video", "music", "tts_voice", "image", "seedance_effect", "export", "preview", "cache", "unknown"]
    kind: Literal["video", "audio", "image", "file", "unknown"]
    area: Literal["downloads", "tts", "seedance", "seedance_cache", "exports", "previews", "cache", "external", "unknown"] = "unknown"
    extension: str = ""
    size_bytes: int = 0
    created_at: datetime
    modified_at: datetime
    day: str
    managed: bool = True
    can_delete: bool = True
    # Imported clips are the operator's own files. The app may forget them, but must never
    # delete them, so removal from the library is a separate capability from deletion.
    can_forget: bool = False
    # Cache and preview files are churn, not assets worth naming.
    can_rename: bool = False
    can_preview: bool = False
    can_use_as_source: bool = False
    # Files that are two halves of one finished video — the delivered cut and its subtitle-free
    # master — share a group, so the library can show them as one entry that opens up rather
    # than as two rows the operator has to mentally pair. Empty for everything else.
    export_group: str = ""
    variant: str = ""
    variant_label: str = ""


class MediaCalendarDay(BaseModel):
    day: str
    total_bytes: int = 0
    asset_count: int = 0
    assets: list[MediaAsset] = Field(default_factory=list)


class StorageBucket(BaseModel):
    key: str
    label: str
    path: str
    size_bytes: int = 0
    file_count: int = 0
    deletable: bool = True


class StorageReport(BaseModel):
    total_bytes: int = 0
    threshold_bytes: int = 20 * 1024 * 1024 * 1024
    over_threshold: bool = False
    buckets: list[StorageBucket] = Field(default_factory=list)
    cleanup_candidates: list[MediaAsset] = Field(default_factory=list)


class CleanupResult(BaseModel):
    deleted_count: int = 0
    freed_bytes: int = 0
    skipped_count: int = 0


class SecretStatus(BaseModel):
    configured: bool = False
    masked: str | None = None


class LLMSettingsSummary(BaseModel):
    enabled: bool = False
    provider: Literal["doubao"] = "doubao"
    model: str = ""
    api_key: SecretStatus = Field(default_factory=SecretStatus)
    timeout_ms: int = 20000


class TTSSettingsSummary(BaseModel):
    enabled: bool = False
    provider: Literal["volcengine_sync"] = "volcengine_sync"
    app_id: SecretStatus = Field(default_factory=SecretStatus)
    access_token: SecretStatus = Field(default_factory=SecretStatus)
    voice_type: str = ""
    cluster: str = "volcano_tts"
    encoding: Literal["mp3", "wav"] = "mp3"
    speed_ratio: float = 1.0
    volume_ratio: float = 1.0
    pitch_ratio: float = 1.0
    with_timestamp: bool = True
    # A daily cap on voiceover generations, mirroring the Seedance daily limit. Counts every
    # 旁白 produced, whether or not the LLM assist drafted the text.
    daily_limit: int = 100


class SeedanceSettingsSummary(BaseModel):
    enabled: bool = False
    provider: Literal["volcengine_ark"] = "volcengine_ark"
    api_key: SecretStatus = Field(default_factory=SecretStatus)
    model: str = ""
    # Seedance makes video, Seedream makes images: different models, different endpoints.
    image_model: str = ""
    image_size: str = ""
    base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    tos_access_key_id: SecretStatus = Field(default_factory=SecretStatus)
    tos_secret_access_key: SecretStatus = Field(default_factory=SecretStatus)
    tos_security_token: SecretStatus = Field(default_factory=SecretStatus)
    tos_bucket: str = ""
    tos_region: str = "cn-beijing"
    tos_endpoint: str = "tos-cn-beijing.volces.com"
    tos_object_prefix: str = "seedance/staging"
    tos_url_expires_seconds: int = 86400
    daily_limit: int = 10
    default_duration_seconds: int = 5
    resolution: str = "720p"
    ratio: str = "16:9"


class RobotSettingsSummary(BaseModel):
    enabled: bool = False
    websocket_url: str = ""


class AutomationSettingsSummary(BaseModel):
    """How much the app may produce in a day, regardless of what any one batch asks for."""

    daily_output_limit: int = 100
    used_today: int = 0
    remaining_today: int = 100
    # This is an editing/framing preset, not a camera command: the robot protocol has no aspect
    # ratio operation. It is shown under Hardware & Camera so the operator chooses the frame
    # before shooting and can compose for the centre crop.
    # None is an intentional setting: no fixed canvas, so exports retain the source frame.
    output_aspect_ratio: OutputAspectRatio | None = None
    framing_configured: bool = False
    framing_mode: Literal["center", "custom"] = "center"
    framing_crop_x: float = Field(default=0.5, ge=0, le=1)
    framing_crop_y: float = Field(default=0.5, ge=0, le=1)


class SettingsSummary(BaseModel):
    llm: LLMSettingsSummary
    tts: TTSSettingsSummary
    seedance: SeedanceSettingsSummary
    robot: RobotSettingsSummary
    automation: AutomationSettingsSummary
    settings_path: str


class LLMSettingsUpdate(BaseModel):
    enabled: bool | None = None
    provider: Literal["doubao"] | None = None
    api_key: str | None = None
    model: str | None = None
    timeout_ms: int | None = Field(default=None, ge=1000, le=120000)


class TTSSettingsUpdate(BaseModel):
    enabled: bool | None = None
    provider: Literal["volcengine_sync"] | None = None
    app_id: str | None = None
    access_token: str | None = None
    voice_type: str | None = None
    cluster: str | None = None
    encoding: Literal["mp3", "wav"] | None = None
    speed_ratio: float | None = Field(default=None, ge=0.2, le=3.0)
    volume_ratio: float | None = Field(default=None, ge=0.1, le=5.0)
    pitch_ratio: float | None = Field(default=None, ge=0.2, le=3.0)
    daily_limit: int | None = Field(default=None, ge=1, le=1000)


class SeedanceSettingsUpdate(BaseModel):
    enabled: bool | None = None
    provider: Literal["volcengine_ark"] | None = None
    api_key: str | None = None
    model: str | None = None
    image_model: str | None = None
    image_size: str | None = Field(default=None, max_length=32)
    base_url: str | None = Field(default=None, max_length=2048)
    tos_access_key_id: str | None = None
    tos_secret_access_key: str | None = None
    tos_security_token: str | None = None
    tos_bucket: str | None = Field(default=None, max_length=128)
    tos_region: str | None = Field(default=None, max_length=64)
    tos_endpoint: str | None = Field(default=None, max_length=256)
    tos_object_prefix: str | None = Field(default=None, max_length=512)
    tos_url_expires_seconds: int | None = Field(default=None, ge=60, le=2592000)
    daily_limit: int | None = Field(default=None, ge=1, le=100)
    default_duration_seconds: int | None = Field(default=None, ge=2, le=15)
    resolution: str | None = Field(default=None, max_length=32)
    ratio: str | None = Field(default=None, max_length=32)


class RobotSettingsUpdate(BaseModel):
    websocket_url: str | None = Field(default=None, max_length=2048)

    @field_validator("websocket_url")
    @classmethod
    def validate_websocket_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        stripped = value.strip()
        if stripped and not stripped.startswith(("ws://", "wss://")):
            raise ValueError("Robot websocket URL must start with ws:// or wss://")
        return stripped


class AutomationSettingsUpdate(BaseModel):
    daily_output_limit: int | None = Field(default=None, ge=1, le=10000)
    output_aspect_ratio: OutputAspectRatio | None = None
    framing_configured: bool | None = None
    framing_mode: Literal["center", "custom"] | None = None
    framing_crop_x: float | None = Field(default=None, ge=0, le=1)
    framing_crop_y: float | None = Field(default=None, ge=0, le=1)


class FramingPreferenceSaveRequest(BaseModel):
    aspect_ratio: OutputAspectRatio
    mode: Literal["center", "custom"] = "custom"
    crop_x: float = Field(default=0.5, ge=0, le=1)
    crop_y: float = Field(default=0.5, ge=0, le=1)


class SettingsUpdateRequest(BaseModel):
    llm: LLMSettingsUpdate | None = None
    tts: TTSSettingsUpdate | None = None
    seedance: SeedanceSettingsUpdate | None = None
    robot: RobotSettingsUpdate | None = None
    automation: AutomationSettingsUpdate | None = None


class ProviderTestResult(BaseModel):
    ok: bool
    provider: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class TTSGenerateRequest(BaseModel):
    title: str = "voiceover"
    text: str = Field(default="", max_length=1500)
    use_llm: bool = False
    # Desired spoken length in seconds. Only meaningful with use_llm: the LLM is what shapes
    # the script to length. The backend turns it into a target 字数; blank keeps the old prompt.
    target_seconds: float | None = Field(default=None, ge=1, le=600)


class TTSAsset(BaseModel):
    id: str
    name: str
    audio_path: str
    metadata_path: str | None = None
    text: str = ""
    duration_ms: int = 0
    word_count: int = 0
    created_at: datetime = Field(default_factory=utc_now)


class TTSQuota(BaseModel):
    """Today's voiceover allowance, the TTS analogue of SeedanceQuota."""

    date: str
    used: int = 0
    limit: int = 100
    remaining: int = 100


class TTSGenerateResult(BaseModel):
    media_item: MediaItem
    asset: TTSAsset
    words: list[dict[str, Any]] = Field(default_factory=list)
    final_text: str
    quota: TTSQuota


class SeedanceFrameRequest(BaseModel):
    source_video_media_id: str
    timestamp_seconds: float = Field(default=0.0, ge=0)


class SeedanceGenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=1000)
    title: str = "seedance-effect"
    # "video" uses Seedance, "image" uses Seedream. Either can start from a source picture or
    # from the prompt alone.
    output: Literal["video", "image"] = "video"
    source_image_media_id: str | None = None
    source_video_media_id: str | None = None
    timestamp_seconds: float | None = Field(default=None, ge=0)
    source_image_url: str | None = Field(default=None, max_length=4096)
    duration_seconds: int | None = Field(default=None, ge=2, le=15)
    reuse_existing: bool = True


class SeedanceAsset(BaseModel):
    id: str
    name: str
    kind: Literal["video", "image"] = "video"
    # Holds a .mp4 for a Seedance clip or a .png for a Seedream image.
    output_path: str = ""
    metadata_path: str
    prompt: str
    source_image_path: str | None = None
    source_image_url: str | None = None
    source_object_key: str | None = None
    source_video_path: str | None = None
    timestamp_seconds: float | None = None
    model: str = ""
    duration_seconds: int = 5
    task_id: str | None = None
    status: Literal["draft", "queued", "running", "succeeded", "failed"] = "draft"
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_video_path(cls, data: Any) -> Any:
        """Assets written before images were supported stored the path as video_path."""
        if isinstance(data, dict) and not data.get("output_path") and data.get("video_path"):
            data = {**data, "output_path": data["video_path"]}
        return data


class SeedanceQuota(BaseModel):
    date: str
    used: int = 0
    limit: int = 10
    remaining: int = 10
    count_remaining: int = 10
    used_seconds: int = 0
    seconds_limit: int = 50
    remaining_seconds: int = 50
    default_duration_seconds: int = 5
    minimum_billable_seconds: int = 5


class SeedanceGenerateResult(BaseModel):
    asset: SeedanceAsset
    media_item: MediaItem | None = None
    reused: bool = False
    quota: SeedanceQuota
