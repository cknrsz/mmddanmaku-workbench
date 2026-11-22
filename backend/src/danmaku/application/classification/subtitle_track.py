from collections import Counter, defaultdict
from itertools import groupby
from typing import TypedDict
from unicodedata import category, normalize

from danmaku.domain.inputs import NormalizedItem


class SubtitleEntry(TypedDict):
    id: str
    start_ms: int
    end_ms: int
    text: str


def _text_key(text: str) -> str:
    return "".join(char for char in normalize("NFKC", text).casefold() if category(char)[0] in "LN")


def subtitle_track_hints(chinese: list[SubtitleEntry], items: list[NormalizedItem]) -> dict[str, str]:
    """Return source keys and Chinese cue IDs with support from a fixed-position sequence.

    Uses validated inputs and no I/O. Missing matches yield no hint and require no retry.
    """
    if not chinese:
        return {}

    cues_by_text: dict[str, list[SubtitleEntry]] = defaultdict(list)
    for cue in chinese:
        key = _text_key(cue["text"])
        if len(key) >= 5:
            cues_by_text[key].append(cue)

    by_appearance: dict[tuple[int, int, int | None], list[tuple[int, str, str, str]]] = defaultdict(list)
    for item in items:
        if item.mode not in (4, 5):
            continue
        key = _text_key(item.text)
        cue = min(cues_by_text.get(key, ()), key=lambda cue: abs(cue["start_ms"] - item.time_ms), default=None)
        if cue is not None and abs(cue["start_ms"] - item.time_ms) <= 1250:
            by_appearance[item.mode, item.font_size, item.color_rgb].append(
                (cue["start_ms"], key, item.source_key, cue["id"])
            )

    hints: dict[str, str] = {}
    for matches in by_appearance.values():
        groups = [list(group) for _, group in groupby(sorted(matches), key=lambda match: match[0])]
        # Count each subtitle start only once. Prefer longer lines when several start together.
        starts = [group[0][0] for group in groups]
        texts = [max(group, key=lambda match: len(match[1]))[1] for group in groups]
        counts: Counter[str] = Counter()
        spans: list[tuple[int, int]] = []
        first = 0
        for last, key in enumerate(texts):
            counts[key] += 1
            while starts[last] - starts[first] > 45_000:
                expired = texts[first]
                counts[expired] -= 1
                if not counts[expired]:
                    del counts[expired]
                first += 1
            if len(counts) >= 3:
                if spans and first <= spans[-1][1]:
                    spans[-1] = (spans[-1][0], last)
                else:
                    spans.append((first, last))

        for first, last in spans:
            for group in groups[first:last + 1]:
                for _, _, source_key, cue_id in group:
                    hints[source_key] = cue_id
    return hints
