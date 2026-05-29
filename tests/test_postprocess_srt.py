import pytest
import textwrap
from pathlib import Path
from unittest.mock import patch, MagicMock


class TestParseSrt:
    """Test SRT parsing and writing."""

    def test_parse_simple_srt(self, tmp_path):
        from postprocess_srt import parse_srt
        srt = tmp_path / "test.srt"
        srt.write_text(textwrap.dedent("""\
            1
            00:00:01,000 --> 00:00:03,000
            你好世界

            2
            00:00:04,000 --> 00:00:06,000
            測試字幕
        """), encoding="utf-8")
        entries = parse_srt(str(srt))
        assert len(entries) == 2
        assert entries[0]["text"] == "你好世界"
        assert entries[0]["start_ms"] == 1000
        assert entries[0]["end_ms"] == 3000
        assert entries[1]["text"] == "測試字幕"

    def test_write_srt_roundtrip(self, tmp_path):
        from postprocess_srt import parse_srt, write_srt
        srt_in = tmp_path / "in.srt"
        srt_out = tmp_path / "out.srt"
        content = textwrap.dedent("""\
            1
            00:00:01,000 --> 00:00:03,000
            你好世界

            2
            00:00:04,000 --> 00:00:06,000
            測試字幕

        """)
        srt_in.write_text(content, encoding="utf-8")
        entries = parse_srt(str(srt_in))
        write_srt(entries, str(srt_out))
        result = parse_srt(str(srt_out))
        assert len(result) == 2
        assert result[0]["text"] == "你好世界"
        assert result[1]["text"] == "測試字幕"


class TestHalfToFullWidth:
    """Test half-width to full-width CJK punctuation conversion."""

    def test_comma(self):
        from postprocess_srt import half_to_full_punct
        assert half_to_full_punct("你好,世界") == "你好，世界"

    def test_period(self):
        from postprocess_srt import half_to_full_punct
        assert half_to_full_punct("結束了.") == "結束了。"

    def test_question(self):
        from postprocess_srt import half_to_full_punct
        assert half_to_full_punct("是嗎?") == "是嗎？"

    def test_exclamation(self):
        from postprocess_srt import half_to_full_punct
        assert half_to_full_punct("太棒了!") == "太棒了！"

    def test_preserve_english_context(self):
        from postprocess_srt import half_to_full_punct
        # Between ASCII chars, keep half-width
        assert half_to_full_punct("hello, world") == "hello, world"

    def test_mixed(self):
        from postprocess_srt import half_to_full_punct
        assert half_to_full_punct("你好,world") == "你好，world"


class TestTerminologyIntegration:
    """Test that terminology rules are applied to SRT entries."""

    def test_terminology_applied(self, tmp_path):
        from postprocess_srt import process_srt
        srt = tmp_path / "test.srt"
        srt.write_text(textwrap.dedent("""\
            1
            00:00:01,000 --> 00:00:03,000
            用 Git Hub 很方便

            2
            00:00:04,000 --> 00:00:06,000
            Open AI 的模型
        """), encoding="utf-8")
        out = tmp_path / "out.srt"
        process_srt(str(srt), str(out), do_punctuation=False, do_terminology=True)
        from postprocess_srt import parse_srt
        result = parse_srt(str(out))
        assert result[0]["text"] == "用 GitHub 很方便"
        assert result[1]["text"] == "OpenAI 的模型"


class TestPunctuationWithMock:
    """Test punctuation flow with mocked sherpa-onnx model."""

    def test_punctuation_applied_to_entries(self, tmp_path):
        from postprocess_srt import process_srt, parse_srt

        srt = tmp_path / "test.srt"
        srt.write_text(textwrap.dedent("""\
            1
            00:00:01,000 --> 00:00:03,000
            你好世界

        """), encoding="utf-8")
        out = tmp_path / "out.srt"

        with patch("postprocess_srt._get_punct_engine") as mock_engine:
            engine = MagicMock()
            engine.add_punctuation.return_value = "你好，世界。"
            mock_engine.return_value = engine

            process_srt(str(srt), str(out), do_punctuation=True, do_terminology=False)

        result = parse_srt(str(out))
        assert "你好" in result[0]["text"]
        engine.add_punctuation.assert_called_once()
