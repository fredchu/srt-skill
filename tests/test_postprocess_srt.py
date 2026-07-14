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


class TestSrtCorrectPostprocessTermsCli:
    """Test --terms for scripts/srt_correct/srt_postprocess.py."""

    def test_terms_path_protects_split_terms(self, tmp_path):
        import subprocess
        import sys

        script = Path(__file__).resolve().parents[1] / "scripts" / "srt_correct" / "srt_postprocess.py"
        srt = tmp_path / "in.srt"
        srt.write_text(textwrap.dedent("""\
            1
            00:00:01,000 --> 00:00:03,000
            使用 Open

            2
            00:00:03,000 --> 00:00:05,000
            AI 很方便
        """), encoding="utf-8")
        out = tmp_path / "out.srt"
        terms = tmp_path / "terms.txt"
        terms.write_text("OpenAI\n", encoding="utf-8")

        proc = subprocess.run(
            [sys.executable, str(script), str(srt), str(out), "--terms", str(terms)],
            text=True,
            capture_output=True,
        )

        assert proc.returncode == 0
        result = out.read_text(encoding="utf-8")
        assert "使用 OpenAI" in result
        assert "AI 很方便" not in result

    def test_missing_terms_path_warns_and_does_not_crash(self, tmp_path):
        import subprocess
        import sys

        script = Path(__file__).resolve().parents[1] / "scripts" / "srt_correct" / "srt_postprocess.py"
        srt = tmp_path / "in.srt"
        srt.write_text(textwrap.dedent("""\
            1
            00:00:01,000 --> 00:00:03,000
            使用 Open

            2
            00:00:03,000 --> 00:00:05,000
            AI 很方便
        """), encoding="utf-8")
        out = tmp_path / "out.srt"

        proc = subprocess.run(
            [sys.executable, str(script), str(srt), str(out), "--terms", str(tmp_path / "missing.txt")],
            text=True,
            capture_output=True,
        )

        assert proc.returncode == 0
        assert "WARNING" in proc.stderr
        assert out.exists()


class TestSrtCorrectToolCallResidueCli:
    """Test fail-loud tool-call residue handling in srt_correct postprocessing."""

    @staticmethod
    def run_cli(srt, out, *args):
        import subprocess
        import sys

        script = Path(__file__).resolve().parents[1] / "scripts" / "srt_correct" / "srt_postprocess.py"
        return subprocess.run(
            [sys.executable, str(script), str(srt), str(out), *args],
            text=True,
            capture_output=True,
        )

    def test_strips_residue_lines_from_multiline_cue(self, tmp_path):
        srt = tmp_path / "in.srt"
        out = tmp_path / "out.srt"
        srt.write_text(textwrap.dedent("""\
            1
            00:00:01,000 --> 00:00:03,000
            好
            </INVOKE>
            </content>
            <parameter name="some_really_long_parameter_name">
        """), encoding="utf-8")

        proc = self.run_cli(srt, out, "--strict")

        assert proc.returncode == 0
        assert "⚠ TOOL-CALL RESIDUE: stripped 3 lines, 0 cues" in proc.stderr
        assert "好\n" in out.read_text(encoding="utf-8")
        assert "</INVOKE>" not in out.read_text(encoding="utf-8")

    def test_warns_and_keeps_residue_only_cue_empty(self, tmp_path):
        srt = tmp_path / "in.srt"
        out = tmp_path / "out.srt"
        srt.write_text(textwrap.dedent("""\
            1
            00:00:04,000 --> 00:00:06,000
            <antml:function_calls>
        """), encoding="utf-8")

        proc = self.run_cli(srt, out)

        assert proc.returncode == 0
        assert "stripped 1 lines, 1 cues with residue-only text" in proc.stderr
        assert "00:00:04,000 --> 00:00:06,000" in proc.stderr
        assert out.read_text(encoding="utf-8") == (
            "1\n00:00:04,000 --> 00:00:06,000\n\n\n"
        )

    def test_does_not_match_normal_text_or_other_angle_brackets(self, tmp_path):
        srt = tmp_path / "in.srt"
        out = tmp_path / "out.srt"
        content = textwrap.dedent("""\
            1
            00:00:01,000 --> 00:00:03,000
            This is (normal)

            2
            00:00:04,000 --> 00:00:06,000
            <speaker>hello</speaker>

        """)
        srt.write_text(content, encoding="utf-8")

        proc = self.run_cli(srt, out)

        assert proc.returncode == 0
        assert "TOOL-CALL RESIDUE" not in proc.stderr
        assert out.read_text(encoding="utf-8") == content

    def test_strict_exits_nonzero_for_residue_only_cue(self, tmp_path):
        srt = tmp_path / "in.srt"
        out = tmp_path / "out.srt"
        srt.write_text(textwrap.dedent("""\
            1
            00:00:07,000 --> 00:00:09,000
            <parameter name="query">
        """), encoding="utf-8")

        proc = self.run_cli(srt, out, "--strict")

        assert proc.returncode != 0
        assert "stripped 1 lines, 1 cues with residue-only text" in proc.stderr
        assert "00:00:07,000 --> 00:00:09,000" in proc.stderr
        assert not out.exists()
