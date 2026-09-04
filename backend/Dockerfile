FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app

# curl is used by the compose healthcheck for the upstream wait loop.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY sensewave ./sensewave
COPY alembic ./alembic
COPY alembic.ini ./
COPY entrypoint.sh ./

# psycopg is the sync driver Alembic needs; asyncpg cannot run migrations.
RUN pip install --upgrade pip \
 && pip install "." "psycopg[binary]>=3.1" \
 && pip install --pre "ruview[client]==2.0.0a1" \
 && chmod +x entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["./entrypoint.sh"]
