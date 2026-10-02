"""Validated render revisions; source clocks and reusable preferences stay separate."""
from __future__ import annotations

import copy
import math
import re


RENDER_NODES = {"build", "review", "deliver"}
NUMBERS = {"subtitle_size": (36, 96), "subtitle_bottom": (.025, .30),
           "playback_rate": (.5, 2), "bgm_volume": (0, .20),
           "bgm_fade_in": (0, 5), "bgm_fade_out": (0, 5)}
COLORS = {"background", "accent", "text_color"}
FIELDS = set(NUMBERS) | COLORS | {"bgm_enabled", "bgm_track", "card_position", "title_case"}


def validate_preferences(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) - FIELDS:
        raise ValueError("制作参数包含不支持的字段")
    for key, item in value.items():
        if key in NUMBERS:
            lower, upper = NUMBERS[key]
            if type(item) not in (int, float) or not math.isfinite(item) or not lower <= item <= upper:
                raise ValueError(f"{key} 必须是 {lower}–{upper} 的有限数值")
        elif key in COLORS:
            if not isinstance(item, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", item):
                raise ValueError(f"{key} 必须为六位 HEX 颜色")
        elif key == "bgm_enabled" and type(item) is not bool:
            raise ValueError("bgm_enabled 必须为布尔值")
        elif key == "bgm_track" and (not isinstance(item, str) or
                not (item == "auto" or re.fullmatch(r"[\w .-]+\.(mp3|wav|m4a|flac|ogg)", item))):
            raise ValueError("背景音乐只能选 auto 或已登记曲目的文件名，不能猜路径")
        elif key == "card_position" and (not isinstance(item, str) or item not in {"left", "right"}):
            raise ValueError("card_position 仅支持 left/right")
        elif key == "title_case" and (not isinstance(item, str) or item not in {"normal", "bold"}):
            raise ValueError("title_case 仅支持 normal/bold")
    return copy.deepcopy(value)


def preferences_for(base: dict, settings: dict) -> dict:
    visual = base["visual"]
    defaults = {"subtitle_size": visual["subtitleSize"],
        "subtitle_bottom": min(base["width"], base["height"]) * .065 / base["height"],
        "playback_rate": 1, "bgm_enabled": False, "bgm_track": "auto",
        "bgm_volume": .10, "bgm_fade_in": 1, "bgm_fade_out": 2,
        "background": visual["background"], "accent": visual["accent"],
        "text_color": visual["textColor"], "card_position": visual["cardPosition"],
        "title_case": visual["titleCase"]}
    return validate_preferences({**defaults, **validate_preferences(settings.get("render_preferences", {}))})


def pending_render_requests(project: dict) -> list[dict]:
    """Failed chat replies cannot consume real human requirements."""
    result = []
    for node in project.get("nodes", []):
        if node["id"] not in RENDER_NODES:
            continue
        for message in node.get("chat", {}).get("messages", []):
            if message.get("role") == "user" and not message.get("applied_render_run_id"):
                result.append({"id": message["id"], "node": node["id"],
                    "text": message["content"], "at": message.get("created_at", "")})
        for index, feedback in enumerate(node.get("feedback", [])):
            if not feedback.get("applied_run_id"):
                result.append({"id": f"feedback:{node['id']}:{index}", "node": node["id"],
                    "text": feedback["text"], "at": feedback.get("created_at", feedback.get("at", ""))})
    return sorted(result, key=lambda item: item["at"])


def revision_props(base: dict, preferences: dict, music: dict | None) -> dict:
    """Always transform the original clock once, never a previous sped-up revision."""
    if "render_preferences" in base or base.get("playbackRate", 1) != 1:
        raise ValueError("构建基线已包含倍速，请重新构建原始时间轴后再生成版本")
    prefs = validate_preferences(preferences)
    props = copy.deepcopy(base)
    rate = prefs["playback_rate"]
    props.update(duration=base["duration"] / rate, sourceDuration=base["duration"],
                 playbackRate=rate, render_preferences=prefs)
    for caption in props["captions"]:
        caption["startMs"] /= rate
        caption["endMs"] /= rate
    for scene in props["scenes"]:
        scene["start"] /= rate
        scene["end"] /= rate
    props["visual"].update(subtitleSize=prefs["subtitle_size"], subtitleBottom=prefs["subtitle_bottom"],
        background=prefs["background"], accent=prefs["accent"], textColor=prefs["text_color"],
        cardPosition=prefs["card_position"], titleCase=prefs["title_case"])
    if prefs["bgm_enabled"]:
        if not music or not music.get("source"):
            raise ValueError("背景音乐未准备好，不能声称已添加")
        if not music.get("mixed_source"):
            raise ValueError("原 audio-mix 尚未交付人声与音乐混音")
        props["mixedAudio"] = music["mixed_source"]
        props.pop("bgm", None)
    else:
        props.pop("bgm", None)
        props.pop("mixedAudio", None)
    return props


def reusable_settings(settings: dict, receipt: dict) -> dict:
    """A new video inherits style, never the old recording, text, or publication."""
    result = {key: copy.deepcopy(settings[key]) for key in
              ("template", "output_ratio", "visual_style", "subtitle_style", "subtitle_max_chars")
              if key in settings}
    result["render_preferences"] = validate_preferences(receipt["preferences"])
    return result


def production_notes(receipt: dict) -> str:
    prefs, music = receipt["preferences"], receipt.get("music")
    lines = ["# 本版制作要求", "", f"- 版本：v{receipt['version']}",
        f"- 字幕字号：{prefs['subtitle_size']:g} px（1080 基准）",
        f"- 字幕离画面底部：{prefs['subtitle_bottom']:.0%} 画面高度",
        f"- 播放速度：{prefs['playback_rate']:g} 倍，保持人声音调；字幕与分镜同步映射",
        f"- 全片预计时长：{receipt['duration_seconds']:.2f} 秒",
        f"- 背景音乐：{music['title'] if music else '关闭'}"]
    if music:
        lines += [f"- 音乐相对人声音量：{prefs['bgm_volume']:.0%} 以内，按本片实际人声校准并自动闪避；淡入 {prefs['bgm_fade_in']:g}s / 淡出 {prefs['bgm_fade_out']:g}s",
                  f"- 音乐来源：{music.get('source_site', '')}", f"- 授权记录：{music.get('license', '')}"]
    lines += ["", "## 本版累计要求", ""]
    lines += [f"- {item['text']}" for item in receipt.get("requirements", receipt.get("requests", []))]
    lines += ["", "## 复用和技能沉淀", "", "使用本版制作参数新建项目，可继续在节点对话中修改；原片、稿件和发布文案不会带到新项目。",
              "通用做法可在节点的「沉淀到实际 Skill」中预览修改，再确认写入来源 Skill。样片仍需人工确认。", ""]
    return "\n".join(lines)
