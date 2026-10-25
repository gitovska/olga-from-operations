FROM ghcr.io/astral-sh/uv:0.12.23 AS uv
FROM python:3.13-slim

COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

RUN groupadd --gid 1000 olga \
    && useradd --uid 1000 --gid olga --no-create-home olga \
    && mkdir /data && chown olga:olga /data
COPY olga ./olga
USER olga
CMD ["/app/.venv/bin/python", "-m", "olga", "run"]
