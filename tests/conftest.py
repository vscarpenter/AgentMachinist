"""Shared fixtures for the AgentMachinist suite.

The suite never touches the network.  The advisory update check is the one
code path that would reach a package index on its own, so it is disabled for
every test; the tests that cover it inject their own probe or opener.
"""

import pytest

from machinist.updates import SKIP_UPDATE_CHECK_ENV


@pytest.fixture(autouse=True)
def _disable_update_checks(monkeypatch):
    monkeypatch.setenv(SKIP_UPDATE_CHECK_ENV, "1")
