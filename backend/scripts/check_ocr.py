import asyncio
from sqlalchemy import func, select, text
from app.db.session import get_session_factory
from app.db.models import OcrPage, Media, DriveFile


async def main() -> None:
    factory = get_session_factory()
    async with factory() as s:
        total = (await s.execute(select(func.count()).select_from(OcrPage))).scalar()
        nonempty = (
            await s.execute(select(func.count()).select_from(OcrPage).where(OcrPage.text != ""))
        ).scalar()
        print("ocr_pages total:", total)
        print("non-empty:", nonempty)

        stmt = (
            select(OcrPage.page_number, func.length(OcrPage.text), OcrPage.text)
            .join(Media)
            .join(DriveFile)
            .where(DriveFile.name.ilike("%wimpy%"))
            .order_by(OcrPage.page_number)
            .limit(5)
        )
        for page_number, length, sample in (await s.execute(stmt)).all():
            print(f"page {page_number} len {length} sample: {sample[:200]!r}")

        boy = (
            await s.execute(
                text(
                    "SELECT page_number, left(text, 120) FROM ocr_pages op "
                    "JOIN media m ON m.id = op.media_id "
                    "JOIN drive_files df ON df.id = m.drive_file_id "
                    "WHERE lower(op.text) LIKE '%boy%' LIMIT 5"
                )
            )
        ).fetchall()
        print("boy matches:", len(boy))
        for row in boy:
            print(row)


asyncio.run(main())
