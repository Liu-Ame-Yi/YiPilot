FROM ghcr.io/astral-sh/uv:0.8.22 AS uv
FROM python:3.11.13-slim-bookworm

COPY --from=uv /uv /uvx /bin/
RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 yipilot

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --group dev --frozen --no-install-project
COPY src ./src
RUN uv sync --group dev --frozen

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER yipilot
ENTRYPOINT ["yipilot"]
