# Built from Alpine's own tinyproxy package rather than an unverified
# third-party image - this is the only network boundary the AI sandbox tier
# has (see docker-compose.yml's egress-proxy comment and docs/AI_PIPELINE.md),
# so what runs it should be auditable from a single Dockerfile, not trusted
# sight-unseen off Docker Hub.
FROM alpine:3.20

RUN apk add --no-cache tinyproxy

COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
COPY filter /etc/tinyproxy/filter

EXPOSE 8888

ENTRYPOINT ["tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy.conf"]
