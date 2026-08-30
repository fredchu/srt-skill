"""Shared pytest safety guards for cloud-ASR mocks."""

from __future__ import annotations

import os

import pytest

MOCK_TIMEOUTS = {
    "POLL_INTERVAL_SECONDS": "1",
    "SSH_READY_TIMEOUT_SECONDS": "5",
    "SSH_PROBE_TIMEOUT_SECONDS": "2",
}
LIVE_DEFAULTS = {
    "POLL_INTERVAL_SECONDS": "15",
    "SSH_READY_TIMEOUT_SECONDS": "420",
    "SSH_PROBE_TIMEOUT_SECONDS": "25",
}


def apply_fast_mock_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shorten waits only when a cloud-ASR mock hook is explicitly active."""
    if os.environ.get("CLOUD_ASR_TEST_HOOK"):
        for name, value in MOCK_TIMEOUTS.items():
            monkeypatch.setenv(name, value)


@pytest.fixture(autouse=True)
def _fast_mock_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock tests must not inherit production provisioning waits.

    The condition is essential: applying these values to live execution would
    turn a real 3.7-minute direct-port startup into false timeouts and pod churn.
    """
    apply_fast_mock_timeouts(monkeypatch)
