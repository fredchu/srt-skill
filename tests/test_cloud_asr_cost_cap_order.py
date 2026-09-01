"""Static safety gate for preserving paid cloud ASR output before cost-cap exit."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "cloud_asr.sh"
DOWNLOAD_CALL = (
    'if scp_download "$POD_IP" "$POD_PORT" "$REMOTE_JSON_PATH" '
    '"$LOCAL_JSON_PATH"; then'
)
MODES = (
    (
        "vv",
        'info "running VibeVoice-ASR-HF sdpa bf16 on pod $POD_ID"',
        'info "normalizing VibeVoice JSON"',
    ),
    (
        "breeze",
        'info "running faster-whisper model ${CURRENT_REMOTE_MODEL_ID} CT2 FP16 on pod $POD_ID"',
        'info "converting JSON to SRT"',
    ),
)


def _window(source: str, start_marker: str, end_marker: str) -> tuple[int, int, str]:
    assert source.count(start_marker) == 1, f"expected one start marker: {start_marker}"
    assert source.count(end_marker) == 1, f"expected one end marker: {end_marker}"
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return start, end, source[start:end]


def _assert_paid_result_is_downloaded_before_cost_cap(
    source: str, start_marker: str, end_marker: str
) -> None:
    _, _, section = _window(source, start_marker, end_marker)
    asr_call = section.find('ssh_run_script "asr"')
    download_call = section.find(DOWNLOAD_CALL)
    cap_calls = list(re.finditer(r"^\s*check_cost_cap\s*$", section, re.MULTILINE))

    assert asr_call >= 0, "ASR call anchor disappeared"
    assert download_call >= 0, "raw JSON download anchor disappeared"
    assert len(cap_calls) == 1, "ASR-to-conversion window must contain exactly one cost-cap check"
    assert asr_call < download_call < cap_calls[0].start(), (
        "paid ASR output must be downloaded before check_cost_cap can terminate the pod"
    )


def _mutate_window(
    source: str, start_marker: str, end_marker: str, mutation: str
) -> str:
    start, end, section = _window(source, start_marker, end_marker)
    download = re.search(
        rf"^(?P<indent>[ \t]*){re.escape(DOWNLOAD_CALL)}$", section, re.MULTILINE
    )
    assert download is not None

    if mutation == "insert-before-download":
        changed = (
            section[: download.start()]
            + f'{download.group("indent")}check_cost_cap\n'
            + section[download.start() :]
        )
    elif mutation == "delete-after-download":
        cap = re.search(
            r"^\s*check_cost_cap\s*\n", section[download.end() :], re.MULTILINE
        )
        assert cap is not None
        cap_start = download.end() + cap.start()
        cap_end = download.end() + cap.end()
        changed = section[:cap_start] + section[cap_end:]
    else:  # pragma: no cover - test helper contract
        raise ValueError(mutation)

    return source[:start] + changed + source[end:]


@pytest.mark.parametrize(
    ("mode", "start_marker", "end_marker"), MODES, ids=[mode[0] for mode in MODES]
)
def test_cloud_asr_downloads_paid_result_before_cost_cap(
    tmp_path: Path, mode: str, start_marker: str, end_marker: str
) -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    _assert_paid_result_is_downloaded_before_cost_cap(source, start_marker, end_marker)

    for mutation in ("insert-before-download", "delete-after-download"):
        mutant = tmp_path / f"cloud_asr-{mode}-{mutation}.sh"
        mutant.write_text(
            _mutate_window(source, start_marker, end_marker, mutation), encoding="utf-8"
        )
        with pytest.raises(AssertionError):
            _assert_paid_result_is_downloaded_before_cost_cap(
                mutant.read_text(encoding="utf-8"), start_marker, end_marker
            )
