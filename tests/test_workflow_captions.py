import pytest

from easel.workflow_captions import CaptionPlanError, clean_caption_text, timed_words, validate_caption_plan, to_srt


def recording():
    words = [{"word": word, "start": start, "end": end} for word, start, end in
             [("今天", 1.12, 1.55), ("我", 1.55, 1.69), ("分享", 1.69, 2.14),
              ("一个", 2.32, 2.62), ("省钱", 2.62, 3.0), ("方法。", 3.0, 3.58),
              ("先", 5.23, 5.56), ("用", 5.56, 5.68), ("ChatGPT", 5.68, 6.35),
              ("写稿！", 6.35, 6.99)]]
    return {"segments": [{"start": 1.12, "end": 6.99, "text": "今天我分享一个省钱方法。先用ChatGPT写稿！", "words": words}]}


def plan():
    return {"captions": [{"from": 0, "to": 3, "text": "今天我分享"},
        {"from": 3, "to": 6, "text": "一个省钱方法。"},
        {"from": 6, "to": 10, "text": "先用ChatGPT写稿！"}], "unsupported_requests": []}


def test_phrases_keep_observed_word_boundaries_pauses_and_all_words():
    raw = recording()
    words = timed_words(raw, 8)
    result = validate_caption_plan(plan(), words, 8)
    assert [(s["start"], s["end"]) for s in result] == [(1.12, 2.14), (2.32, 3.58), (5.23, 6.99)]
    assert [s["text"] for s in result] == ["今天我分享", "一个省钱方法", "先用ChatGPT写稿"]
    assert raw["segments"][0]["words"][-1]["word"] == "写稿！", "Raw ASR is immutable"
    assert "00:00:05,230 --> 00:00:06,990\n先用ChatGPT写稿" in to_srt(result)


@pytest.mark.parametrize("change", ["omission", "duplicate", "guessed_time", "overlong", "unspoken", "wrong_end"])
def test_invalid_model_plans_never_become_final_subtitles(change):
    p = plan()
    if change == "omission":
        p["captions"] = p["captions"][:-1]
    elif change == "duplicate":
        p["captions"][1]["from"] = 2
    elif change == "guessed_time":
        p["captions"][0]["start"] = 0
    elif change == "overlong":
        p["captions"] = [{"from": 0, "to": 10, "text": "今天我分享一个省钱方法先用ChatGPT写稿"}]
    elif change == "unspoken":
        p["captions"][2]["text"] = "这个产品已经全面上线"
    else:
        p["captions"][-1]["to"] = 11
    with pytest.raises(CaptionPlanError):
        validate_caption_plan(p, timed_words(recording(), 8), 8)


def test_partial_word_timestamps_cannot_split_a_recording():
    data = recording()
    data["segments"].append({"start": 7, "end": 8, "text": "末尾没有词时间"})
    assert timed_words(data, 8) == []


@pytest.mark.parametrize("text,expected", [("一句话。", "一句话"), ("没错！？”,", "没错"), ("  ChatGPT  ", "ChatGPT"), ("3.14元，", "3.14元")])
def test_only_trailing_punctuation_is_removed(text, expected):
    assert clean_caption_text(text) == expected
