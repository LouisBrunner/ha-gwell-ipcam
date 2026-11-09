inside_docker := path_exists("/.indocker")
root := justfile_directory()

[private]
default:
    @just --list

[group("dev")]
setup:
    uv sync

[group("dev")]
dev: && dev-start
    docker compose build

[group("dev")]
[parallel]
[private]
dev-start: dev-start-up dev-start-exec

[group("dev")]
[private]
dev-start-up:
    docker compose up

[group("dev")]
[private]
dev-start-exec: && (docker-exec 'just dev-hass')
    #!/bin/bash -e

    docker compose events --json | while read -r event; do
      if jq -r 'select(.action == "exec_die")' <<< "$event"; then
        break
      fi
    done

[env("UV_LINK_MODE", "copy")]
[group("dev")]
[private]
dev-hass:
    #!/usr/bin/env sh
    uv sync --group hass
    if [ ! -d "{{ root }}/config" ]; then
        mkdir -p "{{ root }}/config"
        hass --config "{{ root }}/config" --script ensure_config
    fi
    hass --config "{{ root }}/config" --debug

[group("dev")]
deploy host="root@homeassistant.local" port="2020":
    scp -rP {{ port }} custom_components/gwell_ipcam {{ host }}:/homeassistant/custom_components/

[group("vet")]
vet: check test

[group("vet")]
check: setup
    uv run ruff check
    uv run ruff format --diff
    uv run ty check
    uv run tombi format --check --diff
    uv run tombi lint --error-on-warnings

[group("vet")]
test: setup
    uv run --group test pytest tests -v

[group("vet")]
format:
    uv run ruff format
    uv run ruff check --fix
    uv run tombi format

[group("dev")]
devcontainer:
    docker compose exec -it devcontainer ash

[group("devcontainer")]
[private]
docker-exec cmd:
    #!/usr/bin/env sh

    if [ "{{ inside_docker }}" = "true" ]; then
      {{ cmd }}
    else
      docker compose exec -T devcontainer just docker-env '{{ cmd }}'
    fi

[group("devcontainer")]
[private]
docker-env cmd:
    #!/usr/bin/env sh
    export PATH="/tmp/.venv/bin:$PATH"
    export PYTHONPATH="${PYTHONPATH:-}:{{ root }}/custom_components"
    export UV_LINK_MODE=symlink
    export UV_FROZEN=1
    export UV_PROJECT_ENVIRONMENT=/tmp/.venv
    {{ cmd }}

[group("vet")]
[script("uv")]
manifest-sync:
    import json
    import re
    import tomllib
    from pathlib import Path

    root = Path("{{ justfile_dir() }}")

    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    deps = sorted(
        pyproject["project"]["dependencies"],
        key=lambda d: re.split(r"[><=!~\[]", d)[0].strip(),
    )

    manifest_path = root / "custom_components" / "gwell_ipcam" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())

    if manifest.get("requirements") == deps:
        print("manifest.json requirements already up to date")
    else:
        manifest["requirements"] = deps
        manifest_path.write_text(json.dumps(manifest, indent="\t") + "\n")
        print(f"Updated manifest.json requirements: {deps}")

wireshark-bin := "/Applications/Wireshark.app/Contents/MacOS/Wireshark"
capture-file := "capture.pcap"
capture-file-remote := "/sdcard" / capture-file

[private]
_tcpdump_clean:
    rm -f {{ capture-file }}

[private]
_tcpdump *args=f"-w {{ capture-file-remote }}":
    adb shell su -c /data/local/tcpdump -s 0 -U -n -i wlan0 {{ args }}

[private]
_tcpdump-pull: _tcpdump_clean
    adb pull {{ capture-file-remote }} {{ capture-file }}

[private]
_wireshark *args="-r":
    {{ wireshark-bin }} {{ args }}  {{ capture-file }}

[group("mitm")]
mitm-pre: _tcpdump

[group("mitm")]
mitm-post: _tcpdump-pull _wireshark

[group("mitm")]
mitm-live: _tcpdump_clean && _mitm-live
    mkfifo {{ capture-file }}

[parallel]
_mitm-live: (_tcpdump "--immediate-mode" "-w" "-" ">" capture-file) (_wireshark "-k" "-i")
