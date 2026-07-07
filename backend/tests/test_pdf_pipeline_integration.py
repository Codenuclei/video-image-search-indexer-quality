"""Integration tests for the PDF pipeline (PyMuPDF native text + optional OCR fallback)."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from app.config import Settings
from app.db.models import DriveFile, DriveFileStatus, OcrPage
from app.pipelines.pdf import process_pdf_file
from tests.conftest import requires_postgres

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "faces"


class _LocalFileDriveClient:
    def __init__(self, path: Path) -> None:
        self._path = path

    @asynccontextmanager
    async def stream_file_content(self, file_id: str):
        yield _FileResponse(self._path.read_bytes())


class _FileResponse:
    def __init__(self, content: bytes) -> None:
        self._content = content

    async def aiter_bytes(self, chunk_size: int = 1024 * 256):
        yield self._content


def _build_text_pdf(tmp_path: Path) -> Path:
    import fitz

    pdf_path = tmp_path / "sample.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "DriveFaceIndexer searchable PDF text", fontsize=18)
    doc.save(pdf_path)
    doc.close()
    return pdf_path


def _build_face_pdf(tmp_path: Path) -> Path:
    """PDF with an embedded face image — tests face detection on rendered pages."""
    import fitz

    pdf_path = tmp_path / "face.pdf"
    face_jpg = FIXTURES_DIR / "person_a_1.jpg"
    doc = fitz.open()
    page = doc.new_page(width=400, height=400)
    page.insert_image(fitz.Rect(50, 50, 350, 350), filename=str(face_jpg))
    doc.save(pdf_path)
    doc.close()
    return pdf_path


@requires_postgres
@pytest.mark.asyncio
async def test_pdf_pipeline_extracts_native_text_without_ocr(db_session, tmp_path):
    pdf_path = _build_text_pdf(tmp_path)
    drive_file = DriveFile(
        id=f"drive-{uuid.uuid4().hex}",
        name="sample.pdf",
        mime_type="application/pdf",
        path="/sample.pdf",
        status=DriveFileStatus.PROCESSING,
    )
    db_session.add(drive_file)
    await db_session.flush()

    settings = Settings(thumbnail_dir=str(tmp_path / "thumbnails"), temp_dir=str(tmp_path / "tmp"))
    client = _LocalFileDriveClient(pdf_path)

    media = await process_pdf_file(db_session, drive_file, client, settings, engine=None, ocr_engine=None)
    await db_session.commit()

    assert media.page_count == 1
    pages = (await db_session.execute(OcrPage.__table__.select().where(OcrPage.media_id == media.id))).all()
    assert len(pages) == 1
    assert "DriveFaceIndexer" in pages[0].text


@requires_postgres
@pytest.mark.asyncio
async def test_pdf_pipeline_detects_faces_on_rendered_page(db_session, tmp_path):
    """PDFs are text-only; embedded images are not face-scanned (use image pipeline for faces)."""
    pdf_path = _build_face_pdf(tmp_path)
    drive_file = DriveFile(
        id=f"drive-{uuid.uuid4().hex}",
        name="face.pdf",
        mime_type="application/pdf",
        path="/face.pdf",
        status=DriveFileStatus.PROCESSING,
    )
    db_session.add(drive_file)
    await db_session.flush()

    settings = Settings(thumbnail_dir=str(tmp_path / "thumbnails"), temp_dir=str(tmp_path / "tmp"))
    client = _LocalFileDriveClient(pdf_path)

    media = await process_pdf_file(db_session, drive_file, client, settings)
    await db_session.commit()

    from app.db.models import Face

    faces = (await db_session.execute(Face.__table__.select().where(Face.media_id == media.id))).all()
    assert len(faces) == 0
