# syntax=docker/dockerfile:1.2

FROM ghcr.io/home-assistant/homeassistant-base:2026.07.0
RUN apk add --no-cache just~1.48.1
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY --from=alexxit/go2rtc:1.9.14 /usr/local/bin/go2rtc /usr/local/bin/go2rtc
RUN addgroup -S app && adduser -S -G app app
WORKDIR /project
RUN touch /.indocker
RUN mkdir -p /home/app/.cache/uv && chown -R app:app /home/app/.cache/uv
# hadolint ignore=DL3066
USER app
# hadolint ignore=DL3025
HEALTHCHECK CMD true
ENTRYPOINT ["go2rtc", "-c", "/tmp/go2rtc.yaml"]
