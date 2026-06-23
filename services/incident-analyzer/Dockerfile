# Multi-stage: install deps into a venv, then copy into a slim non-root runtime
# (no build toolchain in the final image). Base pinned by digest for reproducible
# rebuilds; bump periodically (docker buildx imagetools inspect python:3.12-slim) —
# the runtime apt upgrade still pulls Debian security fixes at build time.
FROM python:3.12-slim@sha256:d764629ce0ddd8c71fd371e9901efb324a95789d2315a47db7e4d27e78f1b0e9 AS build
WORKDIR /app
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"
COPY requirements.txt requirements.lock ./
# Install the hash-verified lock. --require-hashes rejects any wheel whose sha256
# isn't pinned; --no-deps stops pip re-resolving (the lock is the full closure).
# Regenerate: uv pip compile requirements.txt --python-version 3.12 --generate-hashes -o requirements.lock
RUN pip install --require-hashes --no-deps -r requirements.lock

FROM python:3.12-slim@sha256:d764629ce0ddd8c71fd371e9901efb324a95789d2315a47db7e4d27e78f1b0e9
WORKDIR /app
ENV PATH="/venv/bin:$PATH" PYTHONUNBUFFERED=1
# The official slim base lags Debian security updates by days/weeks; pull them
# in so the Trivy gate in CI stays green without waiting on a base refresh.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /venv /venv
COPY app ./app
# Non-root user (uid 65532 mirrors distroless nonroot).
RUN useradd --uid 65532 --no-create-home --shell /usr/sbin/nologin app
USER 65532:65532
EXPOSE 8080
# Liveness for plain `docker run` / docker-desktop (K8s uses the manifest probes
# instead). Uses the bundled python — the slim image has no curl/wget.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/healthz', timeout=2).status == 200 else 1)"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
