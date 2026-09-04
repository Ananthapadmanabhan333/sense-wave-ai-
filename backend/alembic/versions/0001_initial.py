"""Initial schema: sites, rooms, nodes, readings, events, users.

``readings`` becomes a TimescaleDB hypertable when the extension is available
and a plain table with a BRIN index on ``time`` when it is not. Both are
supported; docker-compose provides Timescale, and a local server without it
still runs. The chosen path is logged by the backend at startup.

Revision ID: 0001
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _timescale_available(bind: sa.engine.Connection) -> bool:
    return (
        bind.execute(
            sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'")
        ).first()
        is not None
    )


def upgrade() -> None:
    op.create_table(
        "sites",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "rooms",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "site_id", sa.Integer(), sa.ForeignKey("sites.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("name", sa.String(120), nullable=False),
        # Consent controls -- enforced at ingest, not in the UI (rule 7).
        sa.Column(
            "monitoring_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("privacy_mode", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "nodes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("node_id", sa.Integer(), nullable=False),
        sa.Column("room_id", sa.Integer(), sa.ForeignKey("rooms.id", ondelete="SET NULL")),
        sa.Column("name", sa.String(120)),
        sa.Column("pos_x", sa.Float()),
        sa.Column("pos_y", sa.Float()),
        sa.Column("pos_z", sa.Float()),
        sa.Column("last_seen", sa.DateTime(timezone=True)),
        sa.Column("rssi_dbm", sa.Float()),
        sa.Column("frame_rate_hz", sa.Float()),
        sa.Column("novelty_score", sa.Float()),
        sa.Column("upstream_stale", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.UniqueConstraint("node_id", name="uq_nodes_node_id"),
    )
    op.create_index("ix_nodes_node_id", "nodes", ["node_id"])

    op.create_table(
        "readings",
        sa.Column("time", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "room_id", sa.Integer(), sa.ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("presence", sa.Boolean()),
        sa.Column("presence_confidence", sa.Float()),
        sa.Column("motion_level", sa.String(32)),
        sa.Column("motion_band_power", sa.Float()),
        sa.Column("breathing_band_power", sa.Float()),
        sa.Column("mean_rssi", sa.Float()),
        sa.Column("estimated_persons", sa.Integer()),
        # Rate and confidence are written together or not at all (rule 1).
        sa.Column("breathing_rate_bpm", sa.Float()),
        sa.Column("breathing_confidence", sa.Float()),
        sa.Column("heart_rate_bpm", sa.Float()),
        sa.Column("heartbeat_confidence", sa.Float()),
        sa.Column("signal_quality", sa.Float()),
        sa.Column(
            "vitals_suppressed", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.PrimaryKeyConstraint("time", "room_id", name="pk_readings"),
    )

    bind = op.get_bind()
    if _timescale_available(bind):
        op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        # migrate_data is unnecessary on an empty table but harmless, and keeps
        # this safe if the migration is ever re-run against seeded data.
        op.execute(
            "SELECT create_hypertable('readings', 'time', "
            "chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE, "
            "migrate_data => TRUE)"
        )
    else:
        # Plain-Postgres fallback. BRIN suits append-mostly time-ordered data at
        # a fraction of a btree's size.
        op.execute("CREATE INDEX ix_readings_time_brin ON readings USING BRIN (time)")

    op.create_index("ix_readings_room_time", "readings", ["room_id", sa.text("time DESC")])

    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("time", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("room_id", sa.Integer(), sa.ForeignKey("rooms.id", ondelete="SET NULL")),
        sa.Column("node_id", sa.Integer()),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("detail", sa.Text()),
    )
    op.create_index("ix_events_time", "events", ["time"])

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )


def downgrade() -> None:
    op.drop_table("users")
    op.drop_index("ix_events_time", table_name="events")
    op.drop_table("events")
    op.drop_index("ix_readings_room_time", table_name="readings")
    op.drop_table("readings")
    op.drop_index("ix_nodes_node_id", table_name="nodes")
    op.drop_table("nodes")
    op.drop_table("rooms")
    op.drop_table("sites")
