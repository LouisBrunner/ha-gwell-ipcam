FROM ghcr.io/home-assistant/homeassistant-base:2026.05.0
RUN apk add --no-cache make~4.4.1 curl~8.14.1
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ARG GO2RTC_VERSION=v1.9.14
ARG TARGETARCH
RUN curl -fsSL -o /usr/local/bin/go2rtc \
  "https://github.com/AlexxIT/go2rtc/releases/download/${GO2RTC_VERSION}/go2rtc_linux_${TARGETARCH}" \
  && chmod +x /usr/local/bin/go2rtc
RUN addgroup -S app && adduser -S -G app app
WORKDIR /project
RUN touch /.indocker
RUN mkdir -p /home/app/.cache/uv && chown -R app:app /home/app/.cache/uv
USER app
HEALTHCHECK CMD true
ENTRYPOINT ["go2rtc", "-c", "/tmp/go2rtc.yaml"]
