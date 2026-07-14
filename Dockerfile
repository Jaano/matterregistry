# Stage 1: build wheels
FROM python:3.13-slim@sha256:bffeb7bd6a85767587059c6ba23e1e9122078e3aa3fa836099171b9bb5a9bb00 AS builder
WORKDIR /build

# Build dep wheels first - this layer + the pip cache mount survive
# unless pyproject.toml itself changes, so iterative app edits don't
# re-download/rebuild every transitive dep.
COPY pyproject.toml .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip wheel --wheel-dir /wheels \
      $(python -c "import tomllib; \
        d = tomllib.load(open('pyproject.toml','rb')); \
        print(' '.join(d['project']['dependencies']))")

# Then build the app wheel on top, without re-resolving deps.
COPY app/ app/
RUN --mount=type=cache,target=/root/.cache/pip \
    pip wheel --no-deps --wheel-dir /wheels .

# Stage 2: runtime image
FROM python:3.13-slim@sha256:bffeb7bd6a85767587059c6ba23e1e9122078e3aa3fa836099171b9bb5a9bb00
LABEL org.opencontainers.image.source="https://github.com/Jaano/matterregistry"
LABEL org.opencontainers.image.licenses="Apache-2.0"

RUN mkdir -p /config

WORKDIR /app
COPY --from=builder /wheels /wheels
COPY app/ app/
COPY alembic.ini .
COPY migrations/ migrations/
RUN pip install --no-cache-dir --no-index --find-links /wheels matterregistry && \
    rm -rf /wheels

VOLUME ["/config"]
EXPOSE 5591

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request, sys; urllib.request.urlopen('http://127.0.0.1:5591/healthz'); sys.exit(0)" || exit 1

CMD ["python", "-m", "app"]
