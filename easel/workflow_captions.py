"""Validate semantic subtitle proposals against immutable ASR word timing.

The Agent chooses phrases and spelling. This module owns time and coverage; it
never estimates word timings from text length or silently drops spoken words.
"""
from __future__ import annotations

from difflib import SequenceMatcher
import math
import re
import unicodedata


class CaptionPlanError(ValueError):
    pass


def clean_caption_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    while text and (text[-1].isspace() or unicodedata.category(text[-1]).startswith("P")):
        text = text[:-1]
    return text.strip()


def spoken_text(text: str) -> str:
    return "".join(c.lower() for c in text if c.isalnum())


def timed_words(data: dict, duration: float) -> list[dict]:
    words, previous_start = [], 0.0
    for segment in data.get("segments", []):
        entries = segment.get("words")
        if not isinstance(entries, list) or not entries:
            return []  # Partial word coverage cannot support safe segmentation.
        for word in entries:
            try:
                start, end = float(word["start"]), float(word["end"])
                text = word["word"]
            except (TypeError, KeyError, ValueError):
                raise CaptionPlanError("原始 ASR 词级时间戳格式无效。") from None
            if (not isinstance(text, str) or not text.strip() or not math.isfinite(start + end)
                    or start < 0 or end < start or start < previous_start - .001 or end > duration + .1):
                raise CaptionPlanError("原始 ASR 词级时间戳为空、倒序或超出原片。")
            words.append({"id": len(words), "word": text, "start": start, "end": end})
            previous_start = start
    return words


def align_caption_lines(proposal: dict, words: list[dict], max_chars: int | None = None) -> dict:
    """Align natural-language lines to observed word boundaries, never time.

    Character matching finds which existing words belong to each phrase. An
    internal-word boundary has no observed timestamp and is rejected rather
    than interpolated. The returned plan is checked by validate_caption_plan.
    """
    if not isinstance(proposal, dict) or set(proposal) != {"lines", "unsupported_requests"}:
        raise CaptionPlanError("自然分句结果须包含 lines 和 unsupported_requests。")
    lines = proposal["lines"]
    if not isinstance(lines, list) or not lines or any(not isinstance(line, str) or not spoken_text(line) for line in lines):
        raise CaptionPlanError("lines 须为非空的字幕短句文字列表。")
    normalized = [spoken_text(line) for line in lines]
    issues = []
    if max_chars is not None:
        for index, line in enumerate(lines):
            text = clean_caption_text(line)
            if len(text) > max_chars:
                issues.append(f"第 {index + 1} 条「{text}」共 {len(text)} 字，超过 {max_chars} 字（英文、空格也计数）；在真实词边界换句，英文短语可在单词间分开。")
    source, edges, spans = "", {0: 0}, []
    for index, word in enumerate(words):
        start = len(source)
        source += spoken_text(word["word"])
        spans.append((start, len(source), word["word"]))
        edges[len(source)] = index + 1
    revised = "".join(normalized)
    matcher = SequenceMatcher(None, source, revised, autojunk=False)
    if len(source) > 32 and matcher.ratio() < .65:
        raise CaptionPlanError("分句文字与原录音差异过大；只校正错词，保留全部实际说出的内容。")
    operations = matcher.get_opcodes()
    line_edges, position = [], 0
    for line in normalized:
        position += len(line)
        line_edges.append(position)
    for kind, first, last, revised_first, _ in operations:
        if kind == "delete":
            omitted = [text for start, end, text in spans if start < end and first <= start and end <= last]
            if omitted:
                line_index = next((i for i, end in enumerate(line_edges) if revised_first < end), len(lines) - 1)
                context = source[max(0, first - 12):min(len(source), last + 12)]
                issues.append(f"第 {line_index + 1} 条「{lines[line_index]}」附近漏掉录音词「{'、'.join(omitted)}」；原录音邻近文字「{context}」。保留实际说出的词，不按主稿删词。")

    def boundary(position, caption_index):
        candidates = []
        for kind, first, last, revised_first, revised_last in operations:
            if kind == "equal" and revised_first <= position <= revised_last:
                candidates.append(first + position - revised_first)
            elif position == revised_first:
                candidates.append(first)
            elif position == revised_last:
                candidates.append(last)
        # Only existing ASR word boundaries carry real timing evidence.
        valid = [value for value in candidates if value in edges]
        if valid:
            return edges[max(valid)]
        near = candidates[0] if candidates else None
        word = next((text for start, end, text in spans if near is not None and start < near < end), "校对后的专名")
        issues.append(f"第 {caption_index + 1} 条「{lines[caption_index]}」末尾切在词「{word}」内部，没有真实词边界时间；移动这处换句，勿拆该词。")
        return None

    captions, previous, position = [], 0, 0
    for index, (line, normalized_line) in enumerate(zip(lines, normalized)):
        position += len(normalized_line)
        end = len(words) if index == len(lines) - 1 else boundary(position, index)
        if end is None:
            continue
        if end <= previous:
            issues.append(f"第 {index + 1} 条「{line}」没有匹配到新的录音词，不能插入额外台词。")
            continue
        captions.append({"from": previous, "to": end, "text": line})
        previous = end
    if issues:
        raise CaptionPlanError("请一次修正以下分句问题并交付完整 JSON：\n" + "\n".join(issues))
    return {"captions": captions, "unsupported_requests": proposal["unsupported_requests"]}


def validate_caption_plan(plan: dict, words: list[dict], duration: float, max_chars: int = 12) -> list[dict]:
    if not isinstance(plan, dict) or set(plan) != {"captions", "unsupported_requests"}:
        raise CaptionPlanError("分句结果须包含 captions 和 unsupported_requests 两个列表。")
    captions, unsupported = plan["captions"], plan["unsupported_requests"]
    if not isinstance(unsupported, list) or any(not isinstance(item, str) for item in unsupported):
        raise CaptionPlanError("unsupported_requests 格式无效。")
    if unsupported:
        raise CaptionPlanError("字幕节点尚不能执行：" + "；".join(unsupported)[:1200])
    if not isinstance(captions, list) or not captions or len(captions) > len(words):
        raise CaptionPlanError("分句结果为空或字幕条数超过真实词数。")
    result, expected, previous_end = [], 0, 0.0
    for index, item in enumerate(captions):
        if not isinstance(item, dict) or set(item) != {"from", "to", "text"}:
            raise CaptionPlanError(f"第 {index + 1} 条仅允许 from/to/text；时间戳由宿主按原始词级时间计算。")
        first, last, text = item["from"], item["to"], item["text"]
        if type(first) is not int or type(last) is not int or first != expected or not first < last <= len(words):
            raise CaptionPlanError(f"第 {index + 1} 条词序号不连续、重复或越界；必须从 0 完整覆盖到 {len(words)}（to 不包含）。")
        if not isinstance(text, str):
            raise CaptionPlanError(f"第 {index + 1} 条字幕文字无效。")
        text = clean_caption_text(text)
        if not text or len(text) > max_chars:
            raise CaptionPlanError(f"第 {index + 1} 条超过 {max_chars} 字或为空，请按自然短语继续拆分：{text[:60]}")
        start = words[first]["start"]
        end = max(word["end"] for word in words[first:last])
        if last < len(words) and words[last]["start"] < end:
            # Some ASR words overlap at a boundary. Use the observed next-word
            # start rather than inventing an interpolated boundary.
            end = words[last]["start"]
        if start < previous_end - .001 or end <= start or end > duration + .1:
            raise CaptionPlanError(f"第 {index + 1} 条分界产生重叠或零时长，请合并边界附近的词。")
        source = spoken_text("".join(w["word"] for w in words[first:last]))
        revised = spoken_text(text)
        # Allow ASR spelling/term corrections, but reject whole-span rewriting
        # or extra manuscript sentences. Short proper names can differ wholly.
        if len(source) > 8 and (SequenceMatcher(None, source, revised).ratio() < .45
                or abs(len(source) - len(revised)) > max(6, len(source) * .6)):
            raise CaptionPlanError(f"第 {index + 1} 条与对应录音词差异过大，只能校正错词，不能插入稿件或漏掉台词。")
        result.append({"start": start, "end": end, "text": text, "word_from": first, "word_to": last})
        expected, previous_end = last, end
    if expected != len(words):
        raise CaptionPlanError(f"分句仅覆盖 {expected}/{len(words)} 个词，末尾录音内容未交付。")
    return result


def to_srt(segments: list[dict]) -> str:
    def timestamp(seconds):
        value = round(seconds * 1000)
        hours, value = divmod(value, 3600000)
        minutes, value = divmod(value, 60000)
        seconds, millis = divmod(value, 1000)
        return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"
    return "\n\n".join(f"{i + 1}\n{timestamp(s['start'])} --> {timestamp(s['end'])}\n{s['text']}"
                       for i, s in enumerate(segments)) + "\n"
