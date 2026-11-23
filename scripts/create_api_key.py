"""Create an API key and store its hash in the database.

Usage:
    python -m scripts.create_api_key --owner "me" --role admin
    python -m scripts.create_api_key --owner "friend" --role user

The raw key is printed once and never stored; keep it somewhere safe.
"""

import argparse
import asyncio

from app.core.security import generate_api_key, hash_api_key
from app.db.models import ApiKey
from app.db.session import get_sessionmaker


async def main(owner: str, role: str) -> None:
    maker = get_sessionmaker()
    if maker is None:
        raise SystemExit("DATABASE_URL is not set")
    raw = generate_api_key()
    async with maker() as session:
        session.add(
            ApiKey(key_prefix=raw[:8], key_hash=hash_api_key(raw), owner_name=owner, role=role)
        )
        await session.commit()
    print(f"Created {role} key for {owner}:\n{raw}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True)
    parser.add_argument("--role", choices=["admin", "user"], default="user")
    args = parser.parse_args()
    asyncio.run(main(args.owner, args.role))
