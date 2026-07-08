#!/usr/bin/env python3

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "reconstruct_srt_from_log.py"


def run_reconstruct(log_text):
    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "mlx_stdout.log"
        srt_path = Path(tmp) / "rebuilt.srt"
        if isinstance(log_text, bytes):
            log_path.write_bytes(log_text)
        else:
            log_path.write_text(log_text, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(log_path), str(srt_path)],
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        return result, srt_path.read_text(encoding="utf-8")


class ReconstructSrtFromLogTest(unittest.TestCase):
    def test_interleaved_traceback_does_not_truncate_or_pollute_output(self):
        result, srt = run_reconstruct(
            "\n".join(
                [
                    "subtitle.sh banner that must not enter subtitles",
                    "[00:01.000 --> 00:02.000] first cue has 3.5% and http://a.b/c",
                    "[00:02.000 --> 00:03.000] ",
                    "[00:03.000 --> 00:04.000] ♪",
                    "[00:04.000 --> 00:05.000] keep lyric ♪ with words",
                    "[00:05.000 --> 00:06.000] 我來示範 Traceback (most recent call last) 這個字面用法",
                    "[01:44:11.200 --> 01:44:15.400] AI手機賣得很好Traceback (most recent call last):",
                    "KeyError: 'words'",
                    "Skipping /x.wav due to KeyError: 'words'",
                    "  File \"/tmp/mlx_whisper.py\", line 1, in <module>",
                    "[01:44:15.400 --> 01:44:18.000] after traceback",
                ]
            )
            + "\n"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("KeyError", srt)
        self.assertNotIn("Skipping", srt)
        self.assertIn("我來示範 Traceback (most recent call last) 這個字面用法", srt)
        self.assertEqual(
            srt,
            "\n".join(
                [
                    "1",
                    "00:00:01,000 --> 00:00:02,000",
                    "first cue has 3.5% and http://a.b/c",
                    "",
                    "2",
                    "00:00:04,000 --> 00:00:05,000",
                    "keep lyric ♪ with words",
                    "",
                    "3",
                    "00:00:05,000 --> 00:00:06,000",
                    "我來示範 Traceback (most recent call last) 這個字面用法",
                    "",
                    "4",
                    "01:44:11,200 --> 01:44:15,400",
                    "AI手機賣得很好",
                    "",
                    "5",
                    "01:44:15,400 --> 01:44:18,000",
                    "after traceback",
                    "",
                    "",
                ]
            ),
        )

    def test_comma_milliseconds_input_is_normalized(self):
        result, srt = run_reconstruct("[00:01,000 --> 00:02,000] x\n")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(srt, "1\n00:00:01,000 --> 00:00:02,000\nx\n\n")

    def test_bom_and_crlf_input(self):
        result, srt = run_reconstruct(
            "\ufeff[00:01.000 --> 00:02.000] cue\r\n".encode("utf-8")
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(srt, "1\n00:00:01,000 --> 00:00:02,000\ncue\n\n")
        self.assertNotIn("\r", srt)

    def test_empty_input_returns_one_and_writes_zero_cues(self):
        result, srt = run_reconstruct("KeyError: 'words'\nSkipping /x.wav due to KeyError: 'words'\n")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(srt, "")

    def test_return_code_contract(self):
        one_arg = subprocess.run(
            [sys.executable, str(SCRIPT), "only-one"],
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        three_args = subprocess.run(
            [sys.executable, str(SCRIPT), "one", "two", "three"],
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        normal, _ = run_reconstruct("[00:01.000 --> 00:02.000] ok\n")

        self.assertEqual(one_arg.returncode, 2)
        self.assertEqual(three_args.returncode, 2)
        self.assertEqual(normal.returncode, 0)


if __name__ == "__main__":
    unittest.main()
