from __future__ import annotations

import socket
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _no_real_aws_or_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests away from the host's AWS config, and fail any real network call."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)

    real_connect = socket.socket.connect

    def guarded_connect(self: socket.socket, address: Any) -> None:
        if self.family == socket.AF_UNIX:
            real_connect(self, address)
            return
        raise AssertionError(f"Test tried to open a network connection to {address}")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
