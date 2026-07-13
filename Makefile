INSIDE_DOCKER = $(shell stat /.indocker 2>&1 >/dev/null && echo 1 || echo 0)
ifeq ($(INSIDE_DOCKER),1)
	export UV_PROJECT_ENVIRONMENT=/tmp/.venv
	export UV_LINK_MODE=symlink
	PATH := /tmp/.venv/bin:$(PATH)
	export PATH
endif

BIOME = biome
ifeq ($(shell which biome),)
	BIOME = bunx biome
endif

all:
.PHONY: all

setup:
ifeq ($(INSIDE_DOCKER),1)
	uv sync --group hass --group docker --no-dev
else
	uv sync --group hass
endif
.PHONY: setup

setup-devcontainer:
ifeq ($(INSIDE_DOCKER),1)
	make setup
else
	docker compose exec -T devcontainer make setup
endif
.PHONY: setup-devcontainer

deploy-local:
	scp -rP 2020 custom_components/gwell_ipcam root@homeassistant.local:/homeassistant/custom_components/
.PHONY: deploy-local

devcontainer-start:
	docker compose up --build
.PHONY: devcontainer-start

devcontainer:
	docker compose exec -it devcontainer ash
.PHONY: devcontainer

dev:
ifeq ($(INSIDE_DOCKER),1)
	@if [ ! -d "$(PWD)/config" ]; then \
		mkdir -p "$(PWD)/config"; \
		hass --config "$(PWD)/config" --script ensure_config; \
	fi
	PYTHONPATH="$(PYTHONPATH):$(PWD)/custom_components" hass --config "$(PWD)/config" --debug
else
	docker compose exec -it devcontainer ash -c 'make dev'
endif
.PHONY: dev

vet:
	uv run ruff check
	uv run ruff format --diff
	uv run ty check
.PHONY: vet

vet-toml:
	uv run tombi format --check --diff
	uv run tombi lint --error-on-warnings
.PHONY: vet-toml

test:
	uv run --group test pytest tests -v
.PHONY: test

format-fix:
	uv run ruff format
	uv run ruff check --fix
	uv run tombi format
.PHONY: format-fix

manifest-sync:
	uv run scripts/manifest_sync.py
	$(BIOME) format --write custom_components/gwell_ipcam/manifest.json
.PHONY: manifest-sync


CAPTURE_FILE = capture.pcap
REMOTE_CAPTURE_FILE = /sdcard/$(CAPTURE_FILE)
TCPDUMP = adb shell su -c /data/local/tcpdump -s 0 -U -n -i wlan0

tcpdump-live:
	rm -f $(CAPTURE_FILE)
	mkfifo $(CAPTURE_FILE)
	$(TCPDUMP) --immediate-mode -w - > $(CAPTURE_FILE)
.PHONY: tcpdump-live

tcpdump:
	$(TCPDUMP) -w $(REMOTE_CAPTURE_FILE)
.PHONY: tcpdump

tcpdump-pull:
	rm -f $(CAPTURE_FILE)
	adb pull $(REMOTE_CAPTURE_FILE)
.PHONY: tcpdump-pull

wireshark-live:
	/Applications/Wireshark.app/Contents/MacOS/Wireshark -k -i $(CAPTURE_FILE)
.PHONY: wireshark-live

wireshark:
	/Applications/Wireshark.app/Contents/MacOS/Wireshark -r $(CAPTURE_FILE)
.PHONY: wireshark
