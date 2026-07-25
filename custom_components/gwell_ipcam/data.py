"""Custom types for the Gwell IP Camera integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.config_entries import ConfigEntry

    from .api import CameraIdentity, GwellIPCamClient
    from .coordinator import GwellIPCamCoordinator, GwellIPCamRecordingsCoordinator


type GwellIPCamConfigEntry = ConfigEntry[GwellIPCamData]


@dataclass
class GwellIPCamData:
    """Runtime data for a configured camera."""

    client: GwellIPCamClient
    identity: CameraIdentity
    coordinator: GwellIPCamCoordinator
    recordings_coordinator: GwellIPCamRecordingsCoordinator
    known_recording_ids: set[str] = field(default_factory=set)
    # Only recordings started after this are eligible to fire a motion event, however they were first noticed.
    recordings_since: datetime | None = None
