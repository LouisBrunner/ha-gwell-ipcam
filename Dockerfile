# syntax=docker/dockerfile:1.2

FROM ghcr.io/home-assistant/homeassistant-base:2026.07.0
RUN apk add --no-cache just~1.48.1
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ARG GO2RTC_VERSION=v1.9.14
ARG TARGETARCH
# FIXME: as usual basic docker features refuse to work, e.g. --chmod=+x
ADD "https://github.com/AlexxIT/go2rtc/releases/download/${GO2RTC_VERSION}/go2rtc_linux_${TARGETARCH}" /usr/local/bin/go2rtc
RUN chmod +x /usr/local/bin/go2rtc
RUN addgroup -S app && adduser -S -G app app
WORKDIR /project
RUN touch /.indocker
RUN mkdir -p /home/app/.cache/uv && chown -R app:app /home/app/.cache/uv
USER app
HEALTHCHECK CMD true
ENTRYPOINT ["go2rtc", "-c", "/tmp/go2rtc.yaml"]
