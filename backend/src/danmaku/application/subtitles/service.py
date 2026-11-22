from codecs import BOM_UTF16_BE, BOM_UTF16_LE
from collections.abc import Mapping
from hashlib import sha256
from typing import Protocol

from danmaku.application.ports import UnitOfWork
from danmaku.application.projects import require_project
from danmaku.domain.inputs import (
    MAX_SUBTITLE_BYTES, ParsedSubtitle, SubtitleEncoding, SubtitleFormat, SubtitleLanguage, SubtitleVersion, Workflow,
)
from danmaku.domain.workbench import Principal, WorkbenchError


class SubtitleParser(Protocol):
    """Parse decoded text into ordered cues and line-based warnings.

    Invalid time ranges or starts outside the video raise a field error. Retry with a corrected file.
    """

    def __call__(self, text: str, format: SubtitleFormat, duration_ms: int | None) -> ParsedSubtitle: ...


class Subtitles:
    def __init__(self, database: UnitOfWork, parse: SubtitleParser, parser_versions: Mapping[SubtitleFormat, str]):
        self.database = database
        self.parse = parse
        self.parser_versions = parser_versions

    def upload(
        self, actor: Principal, project_id: str, original: bytes,
        format: SubtitleFormat, encoding: SubtitleEncoding, filename: str | None = None,
        language: SubtitleLanguage | None = None,
    ) -> SubtitleVersion:
        with self.database() as repository:
            require_project(repository, actor, project_id)
            parts = repository.video_parts(project_id)
        if len(original) > MAX_SUBTITLE_BYTES:
            raise WorkbenchError(413, "subtitle_too_large", "字幕文件超过 2 MiB")
        if format in {"srt", "txt"} and encoding == "utf-8" and original.startswith((BOM_UTF16_LE, BOM_UTF16_BE)):
            encoding = "utf-16"
        try:
            text = original.decode("utf-8-sig" if encoding == "utf-8" else encoding)
        except UnicodeError:
            raise WorkbenchError(422, "subtitle_bad_encoding", "无法解码字幕，请指定文件编码后重试") from None
        duration = parts.target.duration_ms if parts else None
        if parts and parts.source and language is None:
            duration = min(parts.source.duration_ms, parts.target.duration_ms)
        parsed = self.parse(text, format, duration)
        digest = sha256(original).hexdigest()
        parser_version = self.parser_versions[parsed.format]
        version_id = sha256(f"{project_id}:{format}:{encoding}:{parser_version}:{digest}".encode()).hexdigest()
        if filename:
            filename = filename.replace("\\", "/").rsplit("/", 1)[-1].strip() or None
        version = SubtitleVersion(subtitle_version_id=version_id, format=parsed.format, encoding=encoding, filename=filename,
                                  sha256=digest, parser_version=parser_version,
                                  cue_count=len(parsed.cues), warnings=parsed.warnings)
        with self.database(write=True) as repository:
            require_project(repository, actor, project_id)
            version = repository.save_subtitle(project_id, version, original, parsed.cues)
            if language:
                workflow = repository.workflow(project_id) or Workflow()
                repository.save_workflow(project_id, workflow.model_copy(update={
                    f"{language}_subtitle_id": version.subtitle_version_id, "analysis_id": None, "review": None,
                }))
        return version

    def clear_language(self, actor: Principal, project_id: str, language: SubtitleLanguage) -> Workflow:
        with self.database(write=True) as repository:
            require_project(repository, actor, project_id)
            workflow = repository.workflow(project_id) or Workflow()
            field = f"{language}_subtitle_id"
            if getattr(workflow, field) is not None:
                workflow = workflow.model_copy(update={field: None, "analysis_id": None, "review": None})
                repository.save_workflow(project_id, workflow)
            return workflow

    def delete(self, actor: Principal, project_id: str, version_id: str) -> None:
        """Remove an unused project version and its cues in one write transaction."""
        with self.database(write=True) as repository:
            require_project(repository, actor, project_id)
            if repository.subtitle(project_id, version_id) is None:
                raise WorkbenchError(404, "subtitle_not_found", "找不到字幕版本")
            if repository.subtitle_in_use(project_id, version_id):
                raise WorkbenchError(409, "subtitle_in_use", "该字幕已被分析任务引用，需保留供历史审核使用")
            repository.delete_subtitle(project_id, version_id)
