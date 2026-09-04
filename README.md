# SenseWave AI

A live monitoring dashboard for camera-free WiFi sensing: presence, motion,
occupancy and contactless vitals, per room, in real time.

SenseWave is the product layer on top of
[RuView](https://github.com/ruvnet/RuView). It does **no** signal processing —
RuView's `sensing-server` runs as an upstream dependency and SenseWave consumes
it over the network, persists it, reasons over it and presents it.

> **Not a medical device.** Breathing and heart rate are contactless RF
> estimates with no clinical validation. Do not use SenseWave for diagnosis or
> treatment.

## Architecture

```mermaid
flowchart LR
    ESP["ESP32-S3<br/>CSI nodes"] -- "UDP :5005" --> RV

    subgraph up["RuView — upstream, unmodified"]
        RV["sensing-server<br/><i>all CSI signal processing</i>"]
    end

    subgraph sw["SenseWave"]
        ING["Ingest worker<br/><b>the only upstream socket</b>"]
        DB[("Postgres<br/>+ TimescaleDB")]
        API["REST + WS API"]
        UI["Next.js dashboard"]
    end

    RV  -- "WS /ws/sensing @ 10 Hz" --> ING
    ING -- "1 Hz, real frames" --> DB
    ING -- "10 Hz fan-out" --> API
    DB  --> API
    API -- "/api/ws/live" --> UI

    classDef ext fill:#3d2f1f,stroke:#8a6d3b,color:#f0e6d2
    classDef own fill:#1f2d3d,stroke:#3b6d8a,color:#d2e6f0
    classDef store fill:#2a1f3d,stroke:#6d3b8a,color:#e6d2f0
    class ESP,RV ext
    class ING,API,UI own
    class DB store
```

Exactly one process holds the upstream WebSocket. Browser clients connect to
SenseWave's own `/api/ws/live`, never to RuView — which is what gives us one
place for auth, buffering, downsampling and reconnect policy.

Full detail, including the upstream contract discrepancies we found:
**[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

## What it will not do

These are deliberate, enforced in code, and not up for negotiation:

- **No number without its confidence.** Every vitals estimate carries its
  confidence and a server-side low-confidence verdict. Below the floor the UI
  shows "low confidence", never a confident-looking number.
- **No invented data.** No demo values, no interpolation across gaps, no
  smoothing. A gap in the data renders as a gap.
- **No stale value shown as live.** A node past the staleness window greys out.
- **No pose.** RuView's on-device pose checkpoint is PCK@20 = 3.0% and its
  runtime path returns confidence 0. There is no skeleton view.
- **No person identity.** WiFi-only channels cannot separate individuals
  (RuView measured a separation gap of ~0.0005). Occupants are anonymous counts.
- **No monitoring without consent.** Rooms have a monitoring on/off state and a
  privacy mode that suppresses vitals while keeping presence — enforced at
  ingest, not in the UI.

## Quickstart

Requires Docker with the Compose plugin. No sensing hardware needed: RuView runs
with `--source simulate`.

```bash
cp .env.example .env
docker compose -f infra/docker-compose.yml --env-file .env up --build
```

That brings up four services:

| Service | Port | What it is |
|---|---|---|
| `postgres` | 5432 | TimescaleDB (PostgreSQL 16) |
| `sensing-server` | 8080 / 8765 / 5005-udp | RuView upstream, simulated source |
| `backend` | 8000 | SenseWave API + the single upstream consumer |
| `frontend` | 3000 | SenseWave web UI |

Then:

- **API docs** — <http://localhost:8000/docs>
- **Health, including upstream state** — <http://localhost:8000/api/health>
- **Latest reading for a room** — <http://localhost:8000/api/rooms/1/latest>
- **Live stream** — `wscat -c ws://localhost:8000/api/ws/live`

The backend migrates and seeds on boot. With `SENSEWAVE_SEED_DEMO=true` (the
default) it creates a `Room 1`, and `SENSEWAVE_AUTO_MAP_ROOM_ID=1` lets nodes
discovered from the simulated source land there so the demo has visible data.
**Unset `SENSEWAVE_AUTO_MAP_ROOM_ID` in a real deployment** — SenseWave does not
otherwise guess which room a sensor is in.

## Local development without Docker

The backend runs against any PostgreSQL. The test suite needs neither Docker nor
a system Postgres: it starts a real PostgreSQL 16 from the `pgserver` wheel.

```bash
cd backend
python -m venv .venv
.venv/bin/pip install -e ".[dev]"          # Windows: .venv\Scripts\pip
.venv/bin/pip install --pre "ruview[client]==2.0.0a1"
.venv/bin/pytest -q
```

The `--pre` and the exact pin are required: `ruview` is only published as a
pre-release. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for that and the
other upstream contract discrepancies.

To run the API against your own Postgres:

```bash
export SENSEWAVE_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/sensewave
alembic upgrade head
python -m sensewave.seed --demo
uvicorn sensewave.main:app --reload
```

Without TimescaleDB the migration falls back to a plain table with a BRIN index
on `time`. The backend logs which storage mode it chose at startup and reports
it on `/api/health`.

## Layout

```
backend/    FastAPI app, ingest worker, migrations, tests
frontend/   Next.js 15 App Router UI
infra/      docker-compose
docs/       architecture, API, runbook
```

## Configuration

Every variable lives in [`.env.example`](.env.example) with a comment. No
secrets are committed. The ones worth knowing:

| Variable | Default | Meaning |
|---|---|---|
| `SENSEWAVE_UPSTREAM_WS_URL` | `ws://sensing-server:8765/ws/sensing` | The one upstream socket |
| `RUVIEW_API_TOKEN` | — | Bearer token for the upstream; `SENSEWAVE_UPSTREAM_TOKEN` also works |
| `SENSEWAVE_INGEST_HZ` | `1.0` | Persist rate; upstream ticks at 10 Hz |
| `SENSEWAVE_STALE_AFTER_SECONDS` | `10.0` | Past this with no frame, a node is STALE |
| `SENSEWAVE_MIN_VITALS_CONFIDENCE` | `0.5` | Below this, vitals render as "low confidence" |
| `SENSEWAVE_AUTO_MAP_ROOM_ID` | unset | Demo only: land new nodes in this room |

## Status

- **Phase 1 — backend + live ingest:** complete; 34 tests passing against a real
  Postgres and a fake upstream replaying recorded frames.
- **Phase 2 — dashboard:** not started. The frontend is a scaffold that reports
  backend health and nothing else.
- **Phases 3–4 — alerts, operations:** not started.
