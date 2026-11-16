# 0.165.1 has a source release but no official container tag (2026-11-16 audit).
# Consume it only after upstream publishes and the image/config tests pass.
FROM otel/opentelemetry-collector-contrib:0.165.0
COPY --from=busybox:1.38.0-musl /bin/busybox /usr/local/bin/busybox
COPY config.yaml /etc/otelcol-contrib/config.yaml
