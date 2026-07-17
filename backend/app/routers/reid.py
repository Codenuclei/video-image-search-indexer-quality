"""
Append-only re-identification API:

- Body/clothing signatures (prominent + full-body only) and the extra
  identification layer matching unlabeled faces to persons via body structure.
- Reverse image search on face thumbnails with LinkedIn profile discovery.

Nothing here mutates the existing face pipeline, clusters, or persons.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.reid.body import (
    backfill_body_signatures,
    body_gallery,
    body_identification_candidates,
    proof_path,
    prove_media_bodies,
    refresh_signature_person_links,
)
from app.reid.person_detect import yolov8_available
from app.pipelines.common import body_crop_path
from app.config import get_settings
from app.db.models import BodySignature, FaceWebMatch, Media
from app.db.session import get_db
from app.reid.reverse_search import (
    ReverseSearchNotConfigured,
    linkedin_map,
    provider_configured,
    reverse_search_face,
    web_matches_for_face,
)
from app.reid.google_vision import (
    GoogleVisionApiError,
    GoogleVisionNotConfigured,
    official_image_search_by_url,
    official_image_search_face,
    official_image_search_status,
)

router = APIRouter(prefix="/reid", tags=["reid"])


class OfficialImageSearchRequest(BaseModel):
    face_id: int | None = None
    image_url: str | None = None
    max_results: int = Field(default=10, ge=1, le=50)


@router.get("/status")
async def reid_status(session: AsyncSession = Depends(get_db)) -> dict:
    total = (await session.execute(select(func.count(BodySignature.id)))).scalar_one()
    labeled = (
        await session.execute(
            select(func.count(BodySignature.id)).where(BodySignature.person_id.is_not(None))
        )
    ).scalar_one()
    full_body = (
        await session.execute(
            select(func.count(BodySignature.id)).where(BodySignature.is_full_body.is_(True))
        )
    ).scalar_one()
    web_matches = (await session.execute(select(func.count(FaceWebMatch.id)))).scalar_one()
    linkedin = (
        await session.execute(
            select(func.count(FaceWebMatch.id)).where(FaceWebMatch.linkedin_url.is_not(None))
        )
    ).scalar_one()
    return {
        "body_signatures": {
            "total": total,
            "labeled": labeled,
            "unlabeled": total - labeled,
            "full_body": full_body,
        },
        "web_matches": {"total": web_matches, "with_linkedin": linkedin},
        "reverse_search_configured": provider_configured(),
        "person_detector": "yolov8n" if yolov8_available() else "opencv_hog",
        "yolov8_available": yolov8_available(),
    }


@router.post("/prove/{media_id}")
async def reid_prove(media_id: int, embed: bool = True, session: AsyncSession = Depends(get_db)) -> dict:
    """
    Prove person detection on one media: draw YOLO/HOG boxes, save annotated JPEG,
    optionally Gemini-embed body crops linked to faces.
    """
    media = await session.get(Media, media_id)
    if media is None:
        raise HTTPException(status_code=404, detail="Media not found")
    try:
        return await prove_media_bodies(session, media_id, embed=embed)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Prove failed: {exc}") from exc


@router.get("/proof/{media_id}")
async def reid_proof_image(media_id: int) -> FileResponse:
    path = proof_path(media_id, get_settings())
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Proof image not found — POST /reid/prove/{media_id} first")
    return FileResponse(path, media_type="image/jpeg")


@router.post("/backfill")
async def reid_backfill(limit: int = 200, session: AsyncSession = Depends(get_db)) -> dict:
    """Build body signatures for faces that don't have one yet (append-only)."""
    relinked = await refresh_signature_person_links(session)
    stats = await backfill_body_signatures(session, limit=limit)
    return {**stats, "relinked": relinked}


@router.get("/gallery")
async def reid_gallery(limit: int = 48, session: AsyncSession = Depends(get_db)) -> list[dict]:
    """Experimental visual feed — body signatures with match hints."""
    return await body_gallery(session, limit=limit)


@router.get("/body-crop/{face_id}")
async def reid_body_crop(face_id: int) -> FileResponse:
    path = body_crop_path(face_id, get_settings())
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Body crop not found — run backfill first")
    return FileResponse(path, media_type="image/jpeg")


@router.get("/candidates")
async def reid_candidates(
    limit: int = 50,
    threshold: float | None = None,
    session: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Unlabeled faces matched to persons by clothing/body-structure similarity."""
    await refresh_signature_person_links(session)
    return await body_identification_candidates(session, limit=limit, threshold=threshold)


@router.post("/faces/{face_id}/reverse-search")
async def reid_reverse_search(face_id: int, session: AsyncSession = Depends(get_db)) -> dict:
    try:
        return await reverse_search_face(session, face_id)
    except ReverseSearchNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/faces/{face_id}/web-matches")
async def reid_web_matches(face_id: int, session: AsyncSession = Depends(get_db)) -> list[dict]:
    matches = await web_matches_for_face(session, face_id)
    return [
        {
            "id": m.id,
            "provider": m.provider,
            "status": m.status,
            "title": m.result_title,
            "url": m.result_url,
            "linkedin_url": m.linkedin_url,
            "score": m.score,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in matches
    ]


@router.get("/official-image-search/status")
async def reid_official_image_search_status() -> dict:
    """Configuration probe for official Google Cloud Vision reverse-image search."""
    return official_image_search_status()


@router.post("/official-image-search")
async def reid_official_image_search(
    request: OfficialImageSearchRequest,
    session: AsyncSession = Depends(get_db),
) -> dict:
    """
    Experimental official Google reverse-image search via Cloud Vision WEB_DETECTION.
    Use face_id for indexed face thumbnails, or image_url for any public image.
    """
    if request.face_id is None and not request.image_url:
        raise HTTPException(status_code=400, detail="Provide face_id or image_url")
    if request.face_id is not None and request.image_url:
        raise HTTPException(status_code=400, detail="Provide only one of face_id or image_url")

    try:
        if request.face_id is not None:
            return await official_image_search_face(session, request.face_id, max_results=request.max_results)
        return await official_image_search_by_url(request.image_url or "", max_results=request.max_results)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except GoogleVisionNotConfigured as exc:
        raise HTTPException(
            status_code=424,
            detail=(
                f"{exc} Enable Cloud Vision API at https://console.cloud.google.com/apis/library/vision.googleapis.com. "
                "API keys have no OAuth scopes; if using OAuth/service account, use scope "
                "https://www.googleapis.com/auth/cloud-platform."
            ),
        ) from exc
    except GoogleVisionApiError as exc:
        raise HTTPException(
            status_code=424,
            detail=(
                f"Google Cloud Vision failed ({exc.status_code or 'response'}): {exc}. "
                "Turn on Cloud Vision API for the key's Google Cloud project and make sure billing is enabled. "
                "API keys do not need OAuth scopes; OAuth/service accounts need "
                "https://www.googleapis.com/auth/cloud-platform."
            ),
        ) from exc


@router.get("/linkedin-map")
async def reid_linkedin_map(session: AsyncSession = Depends(get_db)) -> dict[str, str]:
    """person_name → LinkedIn URL (drives profile links on the search page)."""
    return await linkedin_map(session)
