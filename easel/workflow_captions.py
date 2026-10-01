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
        if max(len(source), len(revised)) > 8 and (SequenceMatcher(None, source, revised).ratio() < .45
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
