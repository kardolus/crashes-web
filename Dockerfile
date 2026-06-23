FROM python:3.12-slim

# uv pinned to the minor used to generate uv.lock (flightdeck pattern)
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv

RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV UV_LINK_MODE=copy

# dependency layer first (only invalidated when the lockfile changes)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

RUN useradd --create-home --uid 1000 app
USER app

EXPOSE 8000
CMD ["python", "-m", "crashesweb.server"]
