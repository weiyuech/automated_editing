"""Automatic composition accepts recording identities, never client-supplied intervals."""

from pydantic import BaseModel, Field, model_validator

MAX_AUTOMATIC_OUTPUTS = 200


class AutomaticCompositionPlanRequest(BaseModel):
    media_ids: list[str] = Field(min_length=1, max_length=20)
    count: int = Field(ge=2, le=MAX_AUTOMATIC_OUTPUTS)
    include_transit: bool = False

    @model_validator(mode="after")
    def validate_selection(self):
        if len(set(self.media_ids)) != len(self.media_ids):
            raise ValueError("请勿重复选择同一录制")
        if self.count <= len(self.media_ids):
            raise ValueError("组合数量须大于所选录制数量")
        return self


class AutomaticCompositionRequest(AutomaticCompositionPlanRequest):
    plan_fingerprint: str = Field(min_length=1)
