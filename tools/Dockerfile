# Build from repository root with a reviewed Python Alpine image pinned by digest.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
RUN apk add --no-cache ca-certificates docker-cli docker-cli-buildx
COPY config/platform.json /opt/preview-tools/config/platform.json
COPY tools/test_preview_ci.py /opt/preview-tools/tools/test_preview_ci.py
COPY scripts/cluster_reaper_bootstrap.py /opt/preview-tools/scripts/cluster_reaper_bootstrap.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
