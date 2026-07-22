# Gwell IP Camera

Integrate Gwell IP cameras into Home Assistant (compatible with HACS).

## Installation

1. Add this repository (`https://github.com/LouisBrunner/ha-gwell-ipcam`) as a custom repository in the HACS menu.

2. Install by clicking this button:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=LouisBrunner&repository=ha-gwell-ipcam)

## Protocol

See [`docs/PROTOCOL.md`](docs/PROTOCOL.md) for the reverse-engineered wire protocol reference.

## Known limitations

- TODO: recording playbacks

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

then start running HA (accessible [locally](http://localhost:8123)) with the integration:

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

This integration was mostly reverse-engineered and fully developed by Claude, caveat emptor.
