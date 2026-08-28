"""Text post-processing shared by live captions and generated SRT files."""
from __future__ import annotations

import re
from typing import Iterable


Row = tuple[float, float, str, str]


def _norm(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", text.casefold())


def deduplicate_rows(rows: Iterable[Row]) -> list[Row]:
    """Collapse hallucinated repeats and adjacent identical dialogue lines."""
    result: list[Row] = []
    for row in sorted(rows):
        if not result:
            result.append(row)
            continue
        prev = result[-1]
        same_text = _norm(prev[2]) == _norm(row[2])
        same_translation = _norm(prev[3]) == _norm(row[3])
        adjacent = row[0] <= prev[1] + 1.0
        if same_text and same_translation and adjacent:
            result[-1] = (
                prev[0], max(prev[1], row[1]),
                prev[2] or row[2], prev[3] or row[3],
            )
            continue
        result.append(row)
    return result


def merge_short_rows(rows: Iterable[Row]) -> list[Row]:
    """Merge whisper fragments into readable subtitle lines."""
    result: list[Row] = []
    for row in deduplicate_rows(rows):
        if result:
            prev = result[-1]
            gap = row[0] - prev[1]
            original_len = len(prev[2]) + len(row[2])
            translated_len = len(prev[3]) + len(row[3])
            if 0 <= gap <= 1.5 and original_len <= 90 and translated_len <= 90:
                result[-1] = (
                    prev[0], max(prev[1], row[1]),
                    (prev[2] + " " + row[2]).strip(),
                    (prev[3] + row[3]).strip(),
                )
                continue
        result.append(row)
    return result


def apply_glossary(text: str, glossary: dict[str, str]) -> str:
    if not glossary:
        return text
    for source, target in glossary.items():
        if not source:
            continue
        text = re.sub(re.escape(source), target, text, flags=re.IGNORECASE)
    return text


def format_bilingual(original: str, translated: str, ratio: float) -> str:
    """Control how much of the original language is shown beside the target."""
    ratio = max(0.0, min(1.0, float(ratio)))
    if ratio >= 0.75:
        return (original + "\n" + translated).strip() if translated else original
    if ratio <= 0.25:
        return translated or original
    return original if not translated else translated
