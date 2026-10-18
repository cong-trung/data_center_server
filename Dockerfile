# syntax=docker/dockerfile:1

# Both stages must use the same Python image: the virtualenv links to its interpreter.
ARG PYTHON_IMAGE=python:3.11-slim-trixie

FROM ${PYTHON_IMAGE} AS builder
COPY --from=ghcr.io/astral-sh/uv:0.7.13 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_HTTP_TIMEOUT=300
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --locked --no-dev --no-install-project


FROM ${PYTHON_IMAGE}
ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility
RUN useradd --system --create-home --uid 10001 app
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY app ./app
USER app
EXPOSE 8000
# Bypass proxies: Docker may inject HTTP(S)_PROXY into the container, and urllib ignores CIDR no_proxy entries.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request as r; r.build_opener(r.ProxyHandler({})).open('http://127.0.0.1:8000/api/v1/health', timeout=4)"]
CMD ["fastapi", "run", "app/main.py", "--port", "8000"]
