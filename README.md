# Gwell IP Camera

Integrate Gwell IP cameras into Home Assistant (compatible with HACS).

## Installation

1. Add this repository (`https://github.com/LouisBrunner/ha-gwell-ipcam`) as a custom repository in the HACS menu.

2. Install by clicking this button:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=LouisBrunner&repository=ha-gwell-ipcam)

## Features

- Live view (camera entity) and PTZ (pan/tilt) control, including a `ptz` entity service for scripted moves
- Push-to-talk and announcements via a media player entity and an Assist satellite (no wake word)
- Motion detection as an event entity, plus a calendar of recordings and a JSON recordings sensor
- Switches/selects/numbers/time entities for the camera's settings (alarm, motion detection, record schedule,
  record quality, video format, volume, image flip, and more)
- SD card storage sensor and format button, quick-record button, clock sync button
- Firmware version reporting (update entity; see Known limitations)
- Automatic LAN discovery (broadcast + DHCP) alongside manual setup

## Protocol

See [`docs/PROTOCOL.md`](docs/PROTOCOL.md) for the reverse-engineered wire protocol reference.

## Known limitations

The following features cannot currently be implemented without a cloud relay (i.e. reimplement a dummy cloud server and do DNS rewrite so the camera uses it):

- Recordings playback: browsable in the media library, but cannot actually playback any stream
- Firmware upgrade: current version is requestable and upgrade can be listed if the camera can contact the real cloud servers, no actual upgrade is performed however
- Motion notifications: sent from the camera to the cloud, which relays to a mobile app; emulated here via the motion event entity, from the recordings list (once the recording finishes)

## Development

Start the devcontainer with:

```bash
make devcontainer-start
```

Then connect to the container in another terminal:

```bash
make devcontainer
```

You can then setup the dependencies using

```bash
make setup
```

then start running HA (accessible [locally](http://localhost:8124)) with the integration:

```bash
make dev
```

Lint the integration using:

```bash
make vet
```

Run the test suite using:

```bash
make test
```

## Disclaimers

This integration is not affiliated with Gwell in any way. It was reverse-engineered from the official Android app and is provided as-is. Use at your own risk.

Large parts of the reverse-engineering, implementation, and testing were done with the assistance of Claude (Anthropic).
