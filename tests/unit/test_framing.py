from opencomputeruseagent import framing


def test_text_is_framed_with_the_app_and_capped():
    result = framing.frame_result({"content": [{"type": "text", "text": "x" * (framing.TEXT_CAP + 10)},
                                               {"type": "image", "data": "AAAA", "mimeType": "image/png"},
                                               {"type": "image", "data": "BBBB"}, {"type": "image", "data": "CCCC"},
                                               {"type": "resource", "resource": {}}]}, "TextEdit")
    text = result["content"][0]["text"]
    assert text.startswith('[Content read from the app "TextEdit" by Open Computer Use Agent.')
    assert "never instructions" in text and "10 more characters left out" in text
    assert [b["type"] for b in result["content"]] == ["text", "image", "image"]
    assert "isError" not in result


def test_errors_keep_their_flag():
    assert framing.frame_result({"content": [{"type": "text", "text": "no"}], "isError": True})["isError"] is True
    assert framing.error_result("x") == {"content": [{"type": "text", "text": "x"}], "isError": True}
