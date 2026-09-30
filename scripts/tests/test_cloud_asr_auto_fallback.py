"""Exercise actual cloud driver with fake provider APIs (no rented machines)."""
import json
from pathlib import Path

from test_cloud_asr_vast import _run, OFFERS, RUNNING


def test_default_prefers_vast_and_does_not_fallback_on_install_error(tmp_path: Path) -> None:
    log, calls, rc = _run(tmp_path, {"FAKE_OFFERS_JSON": json.dumps(OFFERS)}, provider=None)
    assert rc == 1, log[-2000:]
    assert any("create instance" in c for c in calls)
    assert "install_env failed" in log
    assert "creating RunPod pod" not in log
    assert "terminated Vast.ai instance" in log
    assert "terminated RunPod pod" not in log


def test_no_offers_falls_back_only_in_auto(tmp_path: Path) -> None:
    log, calls, rc = _run(tmp_path, {"FAKE_OFFERS_JSON": "[]"}, provider=None)
    assert rc != 75, log[-2000:]
    assert any("search offers" in c for c in calls)
    assert not any("create instance" in c for c in calls)
    assert "trying RunPod" in log and "creating RunPod pod" in log


def test_explicit_vast_no_offers_returns_75_without_runpod(tmp_path: Path) -> None:
    log, calls, rc = _run(tmp_path, {"FAKE_OFFERS_JSON": "[]"}, provider="vast")
    assert rc == 75, log[-2000:]
    assert "creating RunPod pod" not in log


def test_vast_auth_denied_swaps_machine(tmp_path: Path) -> None:
    log, calls, rc = _run(tmp_path, {"FAKE_OFFERS_JSON": json.dumps(OFFERS),
                                     "FAKE_INSTANCE_JSON": json.dumps(RUNNING),
                                     "MOCK_PROBE_AUTH_DENIED": "1"}, provider="vast")
    assert rc != 0 and "trying next machine" in log, log[-2000:]
    assert len([c for c in calls if "create instance" in c]) == 2
    assert len([c for c in calls if "destroy instance" in c]) == 2
    assert "creating RunPod pod" not in log


def test_auto_does_not_fallback_when_vast_machine_cannot_be_deleted(tmp_path: Path) -> None:
    # The instance stays listed after destroy, so termination is never confirmed.
    # A machine may still be billing: auto must stop instead of renting on RunPod.
    log, calls, rc = _run(tmp_path, {"FAKE_OFFERS_JSON": json.dumps(OFFERS),
                                     "FAKE_INSTANCE_JSON": json.dumps(RUNNING),
                                     "FAKE_INSTANCES_JSON": json.dumps([RUNNING]),
                                     "MOCK_PROBE_AUTH_DENIED": "1"}, provider=None)
    assert rc not in (0, 75), log[-2000:]
    assert "could not be terminated" in log
    assert "trying RunPod" not in log and "creating RunPod pod" not in log


def test_subtitle_accepts_cloud_engine(tmp_path: Path) -> None:
    import os
    import subprocess
    subtitle = Path(__file__).resolve().parents[1] / "subtitle.sh"
    r = subprocess.run(["bash", str(subtitle), "--engine=cloud", str(tmp_path / "nope.mp4")],
                       capture_output=True, text=True, env=dict(os.environ), timeout=60)
    out = r.stdout + r.stderr
    assert "未知引擎" not in out
    assert "找不到檔案" in out, out[-600:]


def test_auto_falls_back_when_vast_is_not_configured(tmp_path: Path) -> None:
    # RunPod-only users who never set up Vast must keep working without setting a provider.
    for extra in ({"FAKE_SSH_KEYS_JSON": "[]"}, {"VAST_LIB_CLI": str(tmp_path / "no-vastai")}):
        case = tmp_path / str(len(extra) + len(next(iter(extra.values()))))
        case.mkdir()
        log, calls, rc = _run(case, extra, provider=None)
        assert "Vast.ai not configured, falling back" in log, log[-2000:]
        assert "trying RunPod" in log and "creating RunPod pod" in log
        assert not any("create instance" in c for c in calls)


def test_explicit_vast_not_configured_is_plain_error(tmp_path: Path) -> None:
    log, calls, rc = _run(tmp_path, {"FAKE_SSH_KEYS_JSON": "[]"}, provider="vast")
    assert rc == 1, log[-2000:]
    assert "no SSH key on the Vast.ai account" in log
    assert "creating RunPod pod" not in log
