import pytest

from easel.workflow_captions import CaptionPlanError, align_caption_lines, clean_caption_text, timed_words, validate_caption_plan, to_srt


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


def test_plain_natural_lines_align_to_existing_word_times_without_model_index_arithmetic():
    words = timed_words(recording(), 8)
    aligned = align_caption_lines({"lines": ["今天我分享", "一个省钱方法", "先用ChatGPT写稿"], "unsupported_requests": []}, words)
    result = validate_caption_plan(aligned, words, 8)
    assert [(s["word_from"], s["word_to"]) for s in result] == [(0, 3), (3, 6), (6, 10)]
    assert result[2]["start"] == 5.23 and result[2]["end"] == 6.99


def test_spelling_correction_changes_text_only_and_maps_whole_observed_words():
    words = timed_words({"segments": [{"words": [
        {"word": "先用", "start": 2.6, "end": 2.99}, {"word": "瑞莫神", "start": 2.99, "end": 4.27},
        {"word": "写稿", "start": 4.27, "end": 5.13}]}]}, 6)
    p = align_caption_lines({"lines": ["先用Remotion", "写稿"], "unsupported_requests": []}, words)
    s = validate_caption_plan(p, words, 6)
    assert s[0]["word_to"] == 2 and s[0]["end"] == 4.27


@pytest.mark.parametrize("lines", [["今天我分", "享一个省钱方法", "先用ChatGPT写稿"],
                                  ["今天分享", "一个省钱方法", "先用ChatGPT写稿"]])
def test_alignment_rejects_boundaries_inside_words_and_dropped_spoken_words(lines):
    with pytest.raises(CaptionPlanError):
        align_caption_lines({"lines": lines, "unsupported_requests": []}, timed_words(recording(), 8))


def test_alignment_reports_all_omissions_with_phrase_context_and_length_in_one_receipt():
    entries = ["如果", "你", "也", "正在", "用", "Codex", "with", "ChatGPT", "它", "的", "效果", "很好"]
    words = [{"id": i, "word": word, "start": i, "end": i + 1} for i, word in enumerate(entries)]
    with pytest.raises(CaptionPlanError) as caught:
        align_caption_lines({"lines": ["如果你也用", "Codex with ChatGPT", "它效果很好"], "unsupported_requests": []}, words, 12)
    receipt = str(caught.value)
    assert "正在" in receipt and "的" in receipt
    assert "第 1 条「如果你也用」" in receipt and "第 3 条「它效果很好」" in receipt
    assert "原录音邻近文字" in receipt
    assert "第 2 条" in receipt and "超过 12 字" in receipt


def test_long_english_phrase_can_split_between_observed_words():
    words = [{"id": i, "word": word, "start": i + .1, "end": i + .8}
             for i, word in enumerate(["叫做", "Codex", "with", "ChatGPT"])]
    aligned = align_caption_lines({"lines": ["叫做Codex", "with ChatGPT"], "unsupported_requests": []}, words, 12)
    result = validate_caption_plan(aligned, words, 5, 12)
    assert [(s["start"], s["end"]) for s in result] == [(.1, 1.8), (2.1, 3.8)]


@pytest.mark.parametrize("text,expected", [("一句话。", "一句话"), ("没错！？”,", "没错"), ("  ChatGPT  ", "ChatGPT"), ("3.14元，", "3.14元")])
def test_only_trailing_punctuation_is_removed(text, expected):
    assert clean_caption_text(text) == expected
