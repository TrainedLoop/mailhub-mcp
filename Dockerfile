FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.12 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ ./src/
RUN uv sync --frozen --no-dev

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin mcp
USER mcp

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    FASTMCP_HOME=/data \
    ACCOUNTS_DIR=/secrets/accounts \
    JOURNAL_PATH=/data/changes.jsonl

EXPOSE 8742
CMD ["mailhub-mcp"]
