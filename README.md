# videoipcamera.cn/com IP cameras integration

Integrate videoipcamera.cn/com IP cameras into Home Assistant (compatible with HACS).

## Installation

1. Add this repository (`https://github.com/LouisBrunner/ha-videoipcamera`) as a custom repository in the HACS menu.

2. Install by clicking this button:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_reposity/?owner=LouisBrunner&repository=ha-videoipcamera)

## Development

Start the devcontainer with:

```bash
docker compose up --build
```

Then connect to the container in another terminal:

```bash
docker compose exec -it devcontainer bash
```

You can then setup the dependencies using

```bash
./scripts/setup
```

then start running HA (available at http://localhost:8123) with the integration:

```bash
./scripts/develop
```

Finally you can lint the integration using:

```bash
./scripts/lint
```
