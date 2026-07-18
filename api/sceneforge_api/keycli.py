"""Mint/revoke API keys (no auth UI in v1 — brief non-goal).

  python -m sceneforge_api.keycli create --name "acme-realty"
  python -m sceneforge_api.keycli list
  python -m sceneforge_api.keycli revoke key_abc123def456
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from .db import ApiKey, Database, mint_api_key
from .settings import get_settings


async def _create(name: str, is_worker: bool = False) -> int:
    db = Database(get_settings().database_url)
    await db.create_all()
    key_id, plain, key_hash = mint_api_key()
    async with db.sessionmaker() as session:
        session.add(ApiKey(id=key_id, key_hash=key_hash, name=name, is_worker=is_worker))
        await session.commit()
    await db.dispose()
    role = "worker" if is_worker else "customer"
    print(f"key_id: {key_id}\nname:   {name}\nrole:   {role}\napi_key: {plain}")
    print("\nStore this key now — it is hashed server-side and cannot be shown again.")
    return 0


async def _list() -> int:
    db = Database(get_settings().database_url)
    await db.create_all()
    async with db.sessionmaker() as session:
        keys = (await session.execute(select(ApiKey))).scalars().all()
        for k in keys:
            state = "active" if k.active else "revoked"
            print(f"{k.id}  {state:8s}  {k.name}  ({k.created_at:%Y-%m-%d})")
    await db.dispose()
    return 0


async def _revoke(key_id: str) -> int:
    db = Database(get_settings().database_url)
    async with db.sessionmaker() as session:
        key = (
            await session.execute(select(ApiKey).where(ApiKey.id == key_id))
        ).scalar_one_or_none()
        if key is None:
            print(f"no such key: {key_id}", file=sys.stderr)
            return 1
        key.active = False
        await session.commit()
    await db.dispose()
    print(f"revoked {key_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sceneforge-keys", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("create")
    c.add_argument("--name", required=True)
    c.add_argument(
        "--worker", action="store_true",
        help="mint a worker key (may post pipeline results; give to GPU workers only)",
    )
    sub.add_parser("list")
    r = sub.add_parser("revoke")
    r.add_argument("key_id")
    args = p.parse_args(argv)
    if args.command == "create":
        return asyncio.run(_create(args.name, is_worker=args.worker))
    if args.command == "list":
        return asyncio.run(_list())
    return asyncio.run(_revoke(args.key_id))


if __name__ == "__main__":
    sys.exit(main())
