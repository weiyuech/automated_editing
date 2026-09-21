"""A batch carries reviewed scripts, never client-supplied media paths or timing."""

from pydantic import BaseModel, Field

from automated_video_editing_backend.core.composition import MappedNarrationRequest


class BatchNarrationItem(BaseModel):
    composition_id: str = Field(min_length=1)
    visual_signature: str = Field(min_length=1)
    narration: MappedNarrationRequest


class BatchNarrationRequest(BaseModel):
    items: list[BatchNarrationItem] = Field(min_length=1, max_length=200)
    skip_existing: bool = True
