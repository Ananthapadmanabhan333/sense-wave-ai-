"""Idempotent bootstrap: an admin user, and optionally a demo site/room.

Run by the container entrypoint on every boot. Safe to run repeatedly.

    python -m sensewave.seed
    python -m sensewave.seed --demo
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from sensewave.config import get_settings
from sensewave.db import get_sessionmaker
from sensewave.logging import configure_logging, get_logger
from sensewave.models import Room, Site, User
from sensewave.security import hash_password

log = get_logger(__name__)


async def seed(demo: bool = False) -> None:
    settings = get_settings()
    sm = get_sessionmaker()

    async with sm() as session:
        existing = (
            await session.execute(select(User).where(User.email == settings.admin_email))
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                User(
                    email=settings.admin_email,
                    password_hash=hash_password(settings.admin_password),
                    is_admin=True,
                )
            )
            log.info("seed.admin_created", email=settings.admin_email)
        else:
            log.info("seed.admin_exists", email=settings.admin_email)

        if demo:
            site = (await session.execute(select(Site).limit(1))).scalar_one_or_none()
            if site is None:
                site = Site(name="Demo Site")
                session.add(site)
                await session.flush()
                log.info("seed.site_created", site_id=site.id)

            room = (await session.execute(select(Room).limit(1))).scalar_one_or_none()
            if room is None:
                room = Room(site_id=site.id, name="Room 1")
                session.add(room)
                await session.flush()
                log.info("seed.room_created", room_id=room.id)

        await session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed SenseWave's database.")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="also create a demo site and room, so a simulated upstream has somewhere to land",
    )
    args = parser.parse_args()
    configure_logging(get_settings().log_level)
    asyncio.run(seed(demo=args.demo))


if __name__ == "__main__":
    main()
