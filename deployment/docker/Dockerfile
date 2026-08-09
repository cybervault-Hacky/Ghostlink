# GhostLink Developer Portal — production image (Phase 13Q).
#
# Design goals:
#   * non-root container
#   * minimal base image
#   * deterministic dependency installation (pip hash-locked where practical)
#   * no secrets baked into the image — all secrets come from the environment
#   * read-only filesystem for the app, with a writable runtime volume
#   * dropped Linux capabilities, no privileged mode
#   * explicit exposed port, graceful SIGTERM handling
#
# Build:
#   docker build -f deployment/docker/Dockerfile -t ghostlink/portal:0.15.0 .

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Runtime user (non-root).
RUN groupadd --gid 10001 ghostlink \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin ghostlink

WORKDIR /srv/ghostlink

# Install the portal backend (no network at build for app deps beyond wheels).
COPY portal/backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY portal/backend /srv/ghostlink/portal/backend

# Writable runtime state (backups may be written here).
RUN mkdir -p /srv/ghostlink/runtime/backups \
    && chown -R ghostlink:ghostlink /srv/ghostlink

USER ghostlink

EXPOSE 8788

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8788/health/live', timeout=3).status==200 else 1)"

# Production runs under gunicorn (see deployment/README.md). WSGI entrypoint.
ENTRYPOINT ["python", "-m", "gunicorn", "portal_server.app:create_wsgi_app", "--bind", "0.0.0.0:8788", "--workers", "2", "--access-logfile", "-", "--error-logfile", "-"]
