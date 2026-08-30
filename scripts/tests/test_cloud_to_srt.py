from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "cloud_to_srt.py"


def _run(script: Path, mode: str, src: Path, dst: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), mode, str(src), str(dst)],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _write_json(tmp_path: Path, name: str, payload: object) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_vv_json_is_lowercase_only_and_unconditionally_s2twp(tmp_path: Path) -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'OpenCC("s2twp")' in source

    src = _write_json(
        tmp_path,
        "vv_raw.json",
        [
            {"Start": 0, "End": 60, "Content": " 汉字里面 Hello ", "Speaker": "A"},
            {"Start": 60, "End": 61, "Content": ""},
        ],
    )
    dst = tmp_path / "vv.json"

    proc = _run(SCRIPT, "vv", src, dst)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("vv: 1 條 → ")
    assert proc.stderr == ""
    assert json.loads(dst.read_text(encoding="utf-8")) == [{"start": 0.0, "end": 60.0, "text": "漢字裡面 Hello"}]
    assert dst.read_text(encoding="utf-8").count("Speaker") == 0


def test_vv_s2twp_preserves_traditional_text(tmp_path: Path) -> None:
    src = _write_json(
        tmp_path,
        "vv_traditional.json",
        [{"start": 0, "end": 1, "text": "這是臺灣繁體中文"}],
    )
    dst = tmp_path / "vv.json"
    proc = _run(SCRIPT, "vv", src, dst)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(dst.read_text(encoding="utf-8"))[0]["text"] == "這是臺灣繁體中文"


@pytest.mark.parametrize(
    "name,payload",
    [
        (
            "list_shape.json",
            [
                {"timestamp": [0, 1], "Content": " hello "},
                {"timestamp": [1, 2], "Content": ""},
                {"timestamp": [2, 3], "text": "world"},
            ],
        ),
        (
            "segments_shape.json",
            {"segments": [{"Start": 3, "End": 4, "Content": "one"}, {"Start": 4, "End": 5, "Content": "two"}]},
        ),
        (
            "chunks_shape.json",
            {"chunks": [{"start": 5.5, "end": 6.5, "text": "three"}]},
        ),
    ],
)
def test_breeze_regression_matches_git_show_fallback(tmp_path: Path, name: str, payload: object) -> None:
    try:
        old_source = subprocess.run(
            ["git", "show", "HEAD:scripts/cloud_to_srt.py"],
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout
    except subprocess.CalledProcessError as exc:  # pragma: no cover - skip reason should be explicit
        pytest.skip(f"git show fallback unavailable: {exc.stderr.strip() or exc}")

    old_script = tmp_path / "cloud_to_srt_old.py"
    old_script.write_text(old_source, encoding="utf-8")

    src = _write_json(tmp_path, name, payload)
    current_dst = tmp_path / f"current_{name}.srt"
    old_dst = tmp_path / f"old_{name}.srt"

    current = _run(SCRIPT, "breeze", src, current_dst)
    legacy = _run(old_script, "breeze", src, old_dst)

    assert current.returncode == 0, current.stderr
    assert legacy.returncode == 0, legacy.stderr
    assert current.stdout.startswith("breeze: ")
    assert legacy.stdout.startswith("breeze: ")
    assert current.stderr == ""
    assert legacy.stderr == ""
    assert current_dst.read_bytes() == old_dst.read_bytes()
