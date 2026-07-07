import asyncio

from app.dependencies import get_indexing_worker
from app.db.session import get_session_factory
from sqlalchemy import select, func
from app.db.models import DriveFile, DriveFileStatus


async def main() -> None:
    worker = get_indexing_worker()
    seen = await worker.sync_file_list()
    print("synced_files", seen)

    sf = get_session_factory()
    async with sf() as session:
        pending = (
            await session.execute(
                select(func.count()).select_from(DriveFile).where(DriveFile.status == DriveFileStatus.PENDING)
            )
        ).scalar_one()
        print("pending_before_cycle", pending)

    summary = await worker.run_cycle()
    print("cycle", summary)


if __name__ == "__main__":
    asyncio.run(main())
