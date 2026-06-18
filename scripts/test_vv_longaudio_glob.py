import tempfile
import unittest
from pathlib import Path

from vv_longaudio import find_unique_json_for_stems, find_unique_part_json


class VibeVoiceGlobTest(unittest.TestCase):
    def test_find_unique_part_json_treats_brackets_as_literal_path_chars(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "foo [62H1wkUQJ04]_vvpart1"
            expected = base.with_name(base.name + "_vibevoice.json")
            expected.write_text("{}", encoding="utf-8")

            self.assertEqual(find_unique_part_json(base.with_suffix(".wav")), expected)

    def test_find_unique_json_for_stems_treats_brackets_as_literal_path_chars(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "foo [62H1wkUQJ04]_vvpart1"
            expected = base.with_name(base.name + "_vibevoice.json")
            expected.write_text("{}", encoding="utf-8")

            self.assertEqual(find_unique_json_for_stems([base.with_suffix(".wav")]), expected)


if __name__ == "__main__":
    unittest.main()
