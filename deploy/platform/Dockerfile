# Target Linux only; exact patch tags and frozen uv.lock, no host .venv copied.
FROM ghcr.io/astral-sh/uv:0.12.5 AS uv
FROM python:3.12.14-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/src
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE /app/
COPY src /app/src
COPY vendor/wheels /app/vendor/wheels
COPY workers/tradingagents/vendor/wheels /app/workers/tradingagents/vendor/wheels
RUN uv sync --locked --no-dev --no-editable --python /usr/local/bin/python \
    && groupadd --gid 10001 platform \
    && useradd --uid 10001 --gid 10001 --no-create-home platform \
    && mkdir -p /app/.local/platform /app/.local/audit /app/data/development \
    && chown -R 10001:10001 /app/.local /app/data/development
COPY configs/access_policy.example.json /app/configs/access_policy.example.json
COPY samples/public /app/samples/public
COPY resources /app/resources
COPY ["configs/protocol.json", "configs/protocol.calendar.json", "configs/calendar.lock.json", "configs/split_policy.json", "configs/data_sources.lock.json", "/app/configs/"]
USER 10001:10001
ENV PATH="/app/.venv/bin:$PATH"
ENTRYPOINT ["python", "-P", "-m", "finance_platform.operations.launch"]
