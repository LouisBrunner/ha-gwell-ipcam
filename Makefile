CAPTURE_FILE = capture.pcap
REMOTE_CAPTURE_FILE = /sdcard/$(CAPTURE_FILE)
TCPDUMP = adb shell su -c /data/local/tcpdump -s 0 -U -n -i wlan0

all:
.PHONY: all

requirements.txt: pyproject.toml uv.lock
	uv sync
	uv pip freeze > requirements.txt

dev:
	docker compose up --build
.PHONY: dev

dev-exec:
	docker compose exec -it devcontainer bash
.PHONY: dev-exec

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
