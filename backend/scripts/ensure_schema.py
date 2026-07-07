"""Ensure schema tables exist (idempotent). Run manually if needed."""
import asyncio

from app.config import get_settings
from app.db.schema import ensure_schema
from app.db.session import get_engine


async def main() -> None:
    await ensure_schema(get_engine())
    print("schema ready")


if __name__ == "__main__":
    asyncio.run(main())
