from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ExternalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=255)
    object: Literal["model"] = "model"


class ExternalModelList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    object: Literal["list"] = "list"
    data: list[ExternalModel] = Field(max_length=256)


class ExternalUsageScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: Literal["daily", "lifetime"]
    period_start: int = Field(ge=0)
    period_end: int | None = Field(default=None, ge=0)
    request_count: int = Field(ge=0)
    request_limit: int | None = Field(default=None, gt=0)
    request_remaining: int | None = Field(default=None, ge=0)
    token_consumed: int = Field(ge=0)
    token_reserved: int = Field(ge=0)
    token_limit: int | None = Field(default=None, gt=0)
    token_remaining: int | None = Field(default=None, ge=0)
    updated_at: int | None = Field(default=None, ge=0)


class ExternalUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    object: Literal["usage"] = "usage"
    as_of: int = Field(ge=0)
    daily: ExternalUsageScope
    lifetime: ExternalUsageScope
