"""Shared test setup.

Run tests as ``python -m pytest``.  With ``E4G_OUT`` set, a bare ``pytest``
does not put the repository root on ``sys.path`` and the conditional E4g
import raises ``ImportError``.
"""

from __future__ import annotations

import os
import sys

import pytest


def pytest_configure(config):
    """Load E4g only under ``E4G_OUT``; invoke pytest as ``python -m pytest``."""

    if os.environ.get("E4G_OUT") and not config.pluginmanager.hasplugin("tests.tools.e4g_plugin"):
        config.pluginmanager.import_plugin("tests.tools.e4g_plugin")


@pytest.fixture(autouse=True)
def disc_radio(monkeypatch):
    """Unit tests check protocol mechanics on the exact disc model.

    Simulations default to the link-budget model (``mesh_sim.DEFAULT_SIGNAL``,
    task B1), whose shadowing and fading would make every geometry-based
    assertion probabilistic. Tests of the link budget pass ``signal=``.
    """

    from cairnmesh import mesh_sim

    monkeypatch.setattr(mesh_sim, "DEFAULT_SIGNAL", None)


@pytest.fixture(autouse=True)
def volatile_epoch(monkeypatch, tmp_path):
    """Unit tests build controllers without a state file (review D1 refuses that in use).

    The tests of the refusal itself set ``VOLATILE_EPOCH_ALLOWED`` back. A
    serial session's default controller state goes to the test's own directory.
    """

    from cairnmesh import controller

    monkeypatch.setattr(controller, "VOLATILE_EPOCH_ALLOWED", True)
    monkeypatch.setenv("CAIRNMESH_STATE_DIR", str(tmp_path / "state"))


@pytest.fixture(autouse=True)
def no_test_filter_leaks(request):
    """The test receive filter (tests/testbed) must not outlive the test that made it.

    Only looks if some test already loaded it. A test that builds filters on purpose uses the
    ``testbed_env`` fixture (tests/unit/test_testbed_filter.py); every other test, whatever network
    it ran, ends with ``assert_no_active_filters()``. Leftovers are closed either way.
    """

    yield
    loaded = [m for name, m in list(sys.modules.items()) if name.endswith("testbed.rxfilter") and m is not None]
    for module in loaded:
        try:
            if "testbed_env" not in request.fixturenames:
                module.assert_no_active_filters()
        finally:
            module.close_all()
