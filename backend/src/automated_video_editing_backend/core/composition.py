"""Requests contain identities and choices, never client-supplied file paths or clocks."""

from typing import Literal

from pydantic import BaseModel, Field
from automated_video_editing_backend.core.models import CaptureSelection, OutputAspectRatio


class CompositionRequest(BaseModel):
    purpose: Literal["library", "edit"] = "edit"
    title: str = Field(default="精确拼接", max_length=200)
    media_ids: list[str] = Field(min_length=1, max_length=20)
    capture_selections: list[CaptureSelection] = Field(default_factory=list, max_length=20)
    music_media_id: str | None = None
    voiceover_media_id: str | None = None
    intro_effect_media_id: str | None = None
    outro_effect_media_id: str | None = None
    effect_cover_audio: bool = False
    mute_original_audio: bool = True
    subtitles: bool = True
    subtitle_font: str = "noto_sans_sc"
    subtitle_size: Literal["small", "medium", "large"] = "medium"
    output_aspect_ratio: OutputAspectRatio | None = None
    output_crop_x: float | None = Field(default=None, ge=0, le=1)
    output_crop_y: float | None = Field(default=None, ge=0, le=1)


class CompositionConfirm(BaseModel):
    signature: str


class NarrationBinding(BaseModel):
    node_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=4000)


class MappedNarrationRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    sections: list[NarrationBinding] = Field(default_factory=list, max_length=100)
    playback_rate: float = Field(default=1.0, ge=0.9, le=1.1)
    auto_tempo: bool = True


class NarrationReviewRequest(BaseModel):
    attempt_id: str = Field(min_length=1)


class NarrationAdjustRequest(NarrationReviewRequest):
    playback_rate: float = Field(default=1.0, ge=0.9, le=1.1)
    auto_tempo: bool = True


class NarrationConfirmRequest(NarrationReviewRequest):
    review_id: str = Field(min_length=1)


class NarrationAllocateRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    node_ids: list[str] = Field(default_factory=list, max_length=100)
    measured_feedback: bool = False


class StudioPreviewRequest(BaseModel):
    title: str = Field(default="", max_length=150)
    media_ids: list[str] = Field(min_length=1, max_length=20)
    voiceover_media_ids: list[str] = Field(default_factory=list, max_length=100)
    music_media_id: str | None = None
    intro_effect_media_id: str | None = None
    outro_effect_media_id: str | None = None
    mute_original_audio: bool = True
    effect_cover_audio: bool = False
    subtitles: bool = True
    subtitle_font: str = "noto_sans_sc"
    subtitle_size: Literal["small", "medium", "large"] = "medium"
