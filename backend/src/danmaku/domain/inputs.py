from typing import Annotated, Literal

from pydantic import BaseModel, Field, computed_field

from danmaku.domain.workbench import Bvid, Command, NumericId, PageNumber, ReasoningEffort, ResourceId

DownloadMode = Literal["mock", "bilibili"]
SubtitleEncoding = Literal["utf-8", "utf-16", "gb18030", "shift_jis"]
SubtitleFormat = Literal["ass", "srt", "txt"]
SubtitleLanguage = Literal["japanese", "chinese"]
MAX_SUBTITLE_BYTES = 2 * 1024 * 1024
SEGMENT_MS = 360_000


class VideoPart(BaseModel):
    bvid: Bvid
    page: PageNumber
    cid: Annotated[str, Field(pattern=r"^[1-9][0-9]*$")]
    duration_ms: Annotated[int, Field(gt=0)]
    title: str
    video_title: str | None = None


class CidPart(Command):
    cid: NumericId
    duration_ms: None = None


SourcePart = VideoPart | CidPart


class ProjectParts(BaseModel):
    mode: DownloadMode
    source: VideoPart | None = None
    target: VideoPart


class SourceItem(BaseModel):
    platform_id: str | None = None
    text: str
    time_ms: Annotated[int, Field(ge=0)]
    color: Annotated[int, Field(ge=0, le=0xFFFFFF)] | Literal["special"]
    font_size: Annotated[int, Field(gt=0)]
    mode: Annotated[int, Field(ge=0)]


class NormalizedItem(BaseModel):
    source_key: str
    delivery_key: str | None = None
    text: str
    time_ms: int
    color_rgb: int | None
    font_size: int
    mode: int
    checkbox_type: Literal[0, 4] = 0
    diagnostic: Literal["unsupported_color", "outside_video"] | None = None
    normalizer_version: Literal[1] = 1


REPOST_NOTICE = NormalizedItem(
    source_key="notice:repost",
    text="站内转载，因此会将之前的弹幕一并搬运过来（不含弹幕翻译）",
    time_ms=500,
    color_rgb=0xFFFFFF,
    font_size=25,
    mode=5,
    checkbox_type=4,
)


class Snapshot(BaseModel):
    id: str
    mode: DownloadMode
    part: SourcePart
    range_kind: Literal["current_segments", "synthetic", "merged"]
    state: Literal["draft", "complete"] = "draft"
    segment_count: int
    completed_segments: int = 0
    covered_until_ms: int = 0
    item_count: int = 0
    sequence: int | None = None
    members: list[str] = Field(default_factory=list)
    merged_name: str | None = Field(default=None, exclude=True)

    @computed_field
    @property
    def name(self) -> str:
        if self.merged_name is not None:
            return self.merged_name
        prefix = f"{self.sequence:02d} " if self.sequence is not None else ""
        if isinstance(self.part, CidPart):
            return f"{prefix}CID {self.part.cid}"
        return f"{prefix}{self.part.video_title or self.part.title} P{self.part.page} {self.part.bvid}"


class SnapshotMerge(Command):
    snapshot_ids: Annotated[list[ResourceId], Field(min_length=2)]
    confirm_duration_mismatch: bool = False


class SnapshotSelection(Command):
    snapshot_id: ResourceId


class ReviewReady(Command):
    run_id: ResourceId
    revision: Annotated[int, Field(strict=True, ge=0)]


class Workflow(BaseModel):
    snapshot_id: str | None = None
    japanese_subtitle_id: str | None = None
    chinese_subtitle_id: str | None = None
    analysis_id: str | None = None
    reasoning_effort: ReasoningEffort | None = None
    review: ReviewReady | None = None


class SnapshotItem(BaseModel):
    id: str
    time_ms: int
    text: str
    font_size: int
    color_rgb: int | None
    mode: int


class SourceBatch(BaseModel):
    part: SourcePart
    segment_count: int
    segment: int | None = None
    items: list[SourceItem] = Field(default_factory=list)


class Cue(BaseModel):
    id: str
    start_ms: int
    end_ms: int
    plaintext: str
    style: str
    order: int
    line: int


class SubtitleWarning(BaseModel):
    line: int
    code: Literal["non_text", "empty_text"]


class SubtitleVersion(BaseModel):
    subtitle_version_id: str
    filename: str | None = None
    format: SubtitleFormat
    encoding: SubtitleEncoding
    sha256: str
    parser_version: str
    cue_count: int
    warnings: list[SubtitleWarning]


class ParsedSubtitle(BaseModel):
    format: SubtitleFormat
    cues: list[Cue]
    warnings: list[SubtitleWarning]

class ProjectInputs(BaseModel):
    parts: ProjectParts | None
    snapshots: list[Snapshot]
    subtitles: list[SubtitleVersion]
    send_in_queue: bool
    workflow: Workflow | None = None
