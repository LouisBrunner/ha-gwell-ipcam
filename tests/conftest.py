"""Shared fixtures for gwell_ipcam tests."""

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def _auto_enable_custom_integrations(enable_custom_integrations):  # noqa: ARG001
    """Load custom_components/ instead of only HA core's built-in integrations."""
    return
