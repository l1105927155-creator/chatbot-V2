"""Regression tests for a narrow, explicit reply-channel contamination class."""
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins/v2_dsh_router"))
from reply_safety import ReplyContentError, validate_reply


def test_rejects_mixed_language_self_deliberation_after_tool_send():
    # Synthetic paraphrase of the observed shape; no production chat text stored.
    leaked = (
        "刚才有人问了相关话题。 The final message should be the second line. "
        "I already sent the first message.\n"
        "Let me reconsider and make the wording natural. The final text I output "
        "will be the second message.\nNow output final: 这里是看起来正常的中文回复。"
    )
    with pytest.raises(ReplyContentError) as error:
        validate_reply(leaked)
    assert error.value.code == "OUTPUT_CHANNEL_MISMATCH"
    assert "刚才有人问" not in str(error.value)


def test_rejects_markers_split_across_stream_chunks_after_join():
    joined = "I already sent first. " + "Now output final: here is the answer."
    with pytest.raises(ReplyContentError):
        validate_reply(joined)


@pytest.mark.parametrize("text", [
    "The final message should mention the launch date.",  # one family only
    "I already sent the first message. The train leaves at six.",
    "Let me make it natural. The final message should say: 'The meeting starts at 3.'",
    "I already sent the first message. The final message should include the tracking number.",
    "Let me reconsider the wording. The final answer should explain the tradeoff.",
    "I already sent the report. The final answer will arrive tomorrow.",
    "English is fine: the image has a blue circle and a red square.",
    "Use `I already sent first` in the test fixture and print(result).",
    "```text\nI already sent first. The final message should be short.\n```\nDone.",
    "> I already sent first. The final message should be short.\nHere is the answer.",
    "The phrase ‘I already sent first’ translates as 已经先发了一条。",
    'The phrase "The final message should be the second line" is an example.',
    "The bug report quotes “I already sent first” and “Now output final”; both are examples.",
    "```xml\n<think>private content</think>\n```\nThe answer is 42.",
    "Translate this sentence: ‘Let me reconsider the wording.’",
])
def test_allows_ordinary_english_code_and_quoted_discussion(text):
    assert validate_reply(text) is None


@pytest.mark.parametrize("text", [
    "<think>considering the answer</think>42",
    "<analysis>internal notes</analysis>Answer: 42",
    "<|DSML|>assistant internal payload",
    "<|analysis|>hidden channel",
])
def test_rejects_explicit_internal_protocol_wrappers(text):
    with pytest.raises(ReplyContentError):
        validate_reply(text)


def test_does_not_rewrite_or_truncate_valid_text():
    text = "你好。The final message should mention English.\n第二段包含 `print('hello')`。"
    assert validate_reply(text) is None



def test_fullwidth_dsml_control_wrapper_is_rejected():
    with pytest.raises(ReplyContentError):
        validate_reply("</｜DSML｜function_calls>正常正文")
