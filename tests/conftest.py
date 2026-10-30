"""Automated tests cannot reach providers, even with a developer's exported credentials."""

import os

import httpx
import pytest


@pytest.fixture(autouse=True)
def no_http_requests(monkeypatch):
    for name in os.environ:
        if name.startswith(("TYPESAFE_", "TEXT_MODEL", "SUPERVISOR_")):
            monkeypatch.delenv(name)

    def reject(*args, **kwargs):
        raise AssertionError("Offline tests must mock model requests")

    monkeypatch.setattr(httpx.Client, "send", reject)
