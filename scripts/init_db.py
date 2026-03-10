import asyncio

from core.db import Base, engine
from core import models  # noqa: F401


async def main() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


if __name__ == "__main__":
    asyncio.run(main())
