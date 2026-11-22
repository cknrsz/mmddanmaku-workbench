from collections.abc import Iterator
from html import unescape
import re

from pysubs2 import SSAEvent

from danmaku.domain.inputs import Cue, ParsedSubtitle, SubtitleFormat, SubtitleWarning
from danmaku.domain.workbench import WorkbenchError

SRT_TIME = r"([0-9]{1,5}):([0-5][0-9]):([0-5][0-9])[,.]([0-9]{3})"
SRT_RANGE = re.compile(rf"{SRT_TIME}\s+-->\s+{SRT_TIME}(?:\s+.*)?")
SRT_INDEX = re.compile(r"[0-9]+")
SRT_HEADER = re.compile(r"[0-9]+:[0-9]+:[0-9]+[,.][0-9]+\s*-->")
ASS_TIME = re.compile(r"([0-9]{1,5}):([0-5][0-9]):([0-5][0-9])\.([0-9]{2})")
ASS_FIELDS = "layer start end style name marginl marginr marginv effect text".split()
SRT_BLOCK = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)*")
SRT_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)
SRT_STYLE = re.compile(r"</?(?:b|i|u|s|font)(?:[ \t]+[^<>\n]*)?>", re.IGNORECASE)
SRT_ENTITY = re.compile(r"&(?:amp|lt|gt|quot|apos|nbsp|#[0-9]+|#[xX][0-9a-fA-F]+);")


def invalid_line(line: int) -> WorkbenchError:
    return WorkbenchError(422, "subtitle_invalid_cue", f"字幕第 {line} 行的对白格式或时间无效")


def milliseconds(fields: tuple[str, ...], fraction_scale: int = 1) -> int:
    hours, minutes, seconds, fraction = map(int, fields)
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + fraction * fraction_scale


def txt_contains_srt(text: str) -> bool:
    first, _, rest = text.lstrip().partition("\n")
    header = rest.partition("\n")[0] if SRT_INDEX.fullmatch(first.strip()) else first
    return SRT_HEADER.match(header.strip()) is not None


def srt_events(text: str) -> Iterator[tuple[int, SSAEvent]]:
    # Retain offsets for diagnostics even in files with leading or repeated blank lines.
    position, line = 0, 1
    for separator in SRT_BLOCK.finditer(text):
        yield from srt_block(text[position:separator.start()], line)
        line += text.count("\n", position, separator.end())
        position = separator.end()
    yield from srt_block(text[position:], line)


def srt_block(block: str, line: int) -> Iterator[tuple[int, SSAEvent]]:
    lines = block.split("\n")
    if not block.strip():
        return
    while not lines[0].strip():
        lines.pop(0)
        line += 1
    index = int(SRT_INDEX.fullmatch(lines[0].strip()) is not None)
    if index >= len(lines):
        raise invalid_line(line)
    match = SRT_RANGE.fullmatch(lines[index].strip())
    if not match:
        raise invalid_line(line + index)
    dialogue = "\n".join(lines[index + 1:])
    dialogue = SRT_STYLE.sub("", SRT_BREAK.sub("\n", dialogue))
    dialogue = SRT_ENTITY.sub(lambda entity: unescape(entity[0]), dialogue)
    yield line + index, SSAEvent(
        start=milliseconds(match.groups()[:4]), end=milliseconds(match.groups()[4:]),
        text=dialogue,
    )


def ass_events(text: str) -> Iterator[tuple[int, SSAEvent]]:
    fields = ASS_FIELDS
    in_events = False
    for line, raw in enumerate(text.split("\n"), 1):
        value = raw.strip()
        if value.startswith("[") and value.endswith("]"):
            in_events = value.casefold() == "[events]"
            continue
        if not in_events:
            continue
        kind, _, payload = value.partition(":")
        if kind.casefold() == "format":
            fields = [field.strip().casefold() for field in payload.split(",")]
            if (fields[-1] != "text" or len(set(fields)) != len(fields)
                    or not {"start", "end", "text"} <= set(fields)):
                raise invalid_line(line)
        if kind.casefold() not in {"dialogue", "comment"}:
            continue
        values = payload.lstrip().split(",", len(fields) - 1)
        if len(values) != len(fields):
            raise invalid_line(line)
        event = dict(zip(fields, values))
        start = ASS_TIME.fullmatch(event["start"].strip())
        end = ASS_TIME.fullmatch(event["end"].strip())
        if not start or not end:
            raise invalid_line(line)
        yield line, SSAEvent(
            start=milliseconds(start.groups(), 10), end=milliseconds(end.groups(), 10),
            style=event.get("style", "Default").strip(), text=event["text"],
            type="Comment" if kind.casefold() == "comment" else "Dialogue",
        )


def parse(text: str, format: SubtitleFormat, duration_ms: int | None) -> ParsedSubtitle:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    parsed_format = "srt" if format == "txt" and txt_contains_srt(text) else format
    is_ass = parsed_format in {"ass", "txt"}
    events = ass_events(text) if is_ass else srt_events(text)
    cues: list[Cue] = []
    warnings: list[SubtitleWarning] = []
    for order, (line, event) in enumerate(events):
        if is_ass and not event.is_text:
            warnings.append(SubtitleWarning(line=line, code="non_text"))
            continue
        if event.start >= event.end or (duration_ms is not None and event.start >= duration_ms):
            raise invalid_line(line)
        plaintext = (event.plaintext if is_ass else event.text).strip()
        if not plaintext:
            warnings.append(SubtitleWarning(line=line, code="empty_text"))
            continue
        cues.append(Cue(id=f"c{order + 1}", start_ms=event.start, end_ms=event.end,
                        plaintext=plaintext, style=event.style, order=order, line=line))
    if not cues:
        if parsed_format == "txt":
            raise WorkbenchError(422, "subtitle_empty", "TXT 文件不符合要求，请上传包含有效 ASS 或 SRT 对白的文件")
        raise WorkbenchError(422, "subtitle_empty", "字幕没有可用对白")
    cues.sort(key=lambda cue: (cue.start_ms, cue.order))
    return ParsedSubtitle(format=parsed_format, cues=cues, warnings=warnings)
