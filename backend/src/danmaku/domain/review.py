from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from danmaku.domain.analysis import Category, Classification
from danmaku.domain.inputs import Cue
from danmaku.domain.workbench import Page, ResourceId

class ReviewFilter(BaseModel):
    category: Category | None = None
    selected: bool | None = None
    q: str = Field(default="", max_length=200)


class ReviewRow(BaseModel):
    item_id: str
    time_ms: int
    offset_ms: int = 0
    text: str
    text_edited: bool = False
    color_rgb: int | None
    font_size: int
    mode: int
    checkbox_type: Literal[0, 4] = 0
    classification: Classification | None
    nearby_cues: list[Cue]
    evidence_cues: list[Cue]
    selected: bool
    selection_source: Literal["suggestion", "manual"]


class ReviewPage(Page[ReviewRow]):
    revision: int
    selected_count: int


class ReviewRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: ResourceId
    expected_revision: int = Field(ge=0, strict=True)


class ReviewChange(ReviewRevision):
    item_ids: list[str] = Field(min_length=1, max_length=500)
    selected: bool = Field(strict=True)


class ReviewOffset(ReviewRevision):
    item_id: str = Field(min_length=1)
    offset_ms: int = Field(strict=True, ge=-9_007_199_254_740_991, le=9_007_199_254_740_991)
    apply_to_following: bool = Field(strict=True)


class ReviewText(ReviewRevision):
    item_id: str = Field(min_length=1)
    text: str | None = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def require_nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("弹幕内容不能为空")
        return value


class ReviewCommit(BaseModel):
    revision: int
    changed_count: int
    selected_count: int
