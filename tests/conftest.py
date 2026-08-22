"""Shared fixtures for gwell_ipcam tests."""

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def _auto_enable_custom_integrations(enable_custom_integrations):  # noqa: ARG001
    """Load custom_components/ instead of only HA core's built-in integrations."""
    return


class FakeStore:
    """Stands in for `homeassistant.helpers.storage.Store`: in-memory, no real HA storage plumbing."""

    def __init__(self) -> None:
        self.saved: dict | None = None

    async def async_load(self) -> dict | None:
        return self.saved

    async def async_save(self, data: dict) -> None:
        self.saved = data

    async def async_remove(self) -> None:
        self.saved = None


@pytest.fixture(autouse=True)
def mock_store(monkeypatch) -> dict[str, FakeStore]:
    """Replace `Store` everywhere it's constructed with an in-memory `FakeStore`, one per key suffix."""
    stores: dict[str, FakeStore] = {}

    def _make_store(*_args: object, key: str, **_kwargs: object) -> FakeStore:
        return stores.setdefault(key.rsplit(".", 1)[-1], FakeStore())

    monkeypatch.setattr("custom_components.gwell_ipcam.api.Store", _make_store)
    monkeypatch.setattr("custom_components.gwell_ipcam.fallback_stream.Store", _make_store)
    return stores
