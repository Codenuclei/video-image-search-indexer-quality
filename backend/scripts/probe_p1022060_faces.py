"""Find faces/clusters for P1022060 and compare to named people / other clusters."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

OUT = Path(__file__).with_name("_probe_out.txt")


def log(*args: object) -> None:
    line = " ".join(str(a) for a in args)
    with OUT.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


async def main() -> int:
    OUT.write_text("", encoding="utf-8")
    # Prefer public URL when present (private railway host does not resolve locally)
    for key in ("DATABASE_PUBLIC_URL", "POSTGRES_PUBLIC_URL"):
        if os.environ.get(key):
            os.environ["DATABASE_URL"] = os.environ[key]
            log("using", key)
            break
    else:
        url = os.environ.get("DATABASE_URL", "")
        host = url.split("@")[-1].split("/")[0][:60] if url else "MISSING"
        log("using DATABASE_URL host=", host)

    from sqlalchemy import select
    import numpy as np

    from app.db.session import dispose_engine, get_session_factory

    dispose_engine()
    from app.db.models import DriveFile, Face, FaceCluster, FaceEmbedding, Media, Person
    from app.matching.service import cosine_similarity

    async with get_session_factory()() as session:
        df = (
            await session.execute(select(DriveFile).where(DriveFile.name.ilike("%P1022060%")))
        ).scalar_one_or_none()
        if not df:
            log("FILE_NOT_FOUND")
            return 1
        log(f"FILE id={df.id} name={df.name} path={df.path} status={df.status}")

        media = (
            await session.execute(select(Media).where(Media.drive_file_id == df.id))
        ).scalar_one_or_none()
        if not media:
            log("MEDIA_NOT_FOUND")
            return 1
        log(f"MEDIA id={media.id}")

        faces = (
            await session.execute(select(Face).where(Face.media_id == media.id))
        ).scalars().all()
        log(f"FACES count={len(faces)}")

        persons = (await session.execute(select(Person))).scalars().all()
        person_cents: list[tuple[Person, list[float], int]] = []
        for p in persons:
            fems = (
                await session.execute(
                    select(FaceEmbedding)
                    .join(Face, Face.id == FaceEmbedding.face_id)
                    .where(Face.person_id == p.id)
                    .limit(40)
                )
            ).scalars().all()
            if not fems:
                continue
            cent = (sum(np.asarray(x.embedding, dtype=np.float32) for x in fems) / len(fems)).tolist()
            person_cents.append((p, cent, len(fems)))
            log(f"PERSON {p.id} {p.name!r} n={len(fems)}")

        for f in faces:
            cl = await session.get(FaceCluster, f.cluster_id) if f.cluster_id else None
            person = await session.get(Person, f.person_id) if f.person_id else None
            emb = await session.get(FaceEmbedding, f.id)
            area = f.bbox_width * f.bbox_height
            aspect = f.bbox_width / f.bbox_height if f.bbox_height else 0
            status = cl.status.value if cl else None
            log(
                f"FACE {f.id} conf={f.detection_confidence:.3f} "
                f"bbox=({f.bbox_x:.0f},{f.bbox_y:.0f},{f.bbox_width:.0f}x{f.bbox_height:.0f}) "
                f"area={area:.0f} aspect={aspect:.2f} cluster={f.cluster_id} "
                f"status={status} members={getattr(cl, 'member_count', None)} "
                f"person={getattr(person, 'name', None)}"
            )
            if emb is None:
                continue
            scores = sorted(
                ((cosine_similarity(list(emb.embedding), c), p.name, p.id) for p, c, _ in person_cents),
                reverse=True,
            )
            log("  vs_persons " + ", ".join(f"{s:.3f}:{n}" for s, n, _ in scores[:6]))
            if cl is None or cl.centroid is None:
                continue
            others = (
                await session.execute(
                    select(FaceCluster).where(
                        FaceCluster.centroid.isnot(None),
                        FaceCluster.id != cl.id,
                    )
                )
            ).scalars().all()
            scored = []
            for o in others:
                sim = cosine_similarity(list(cl.centroid), list(o.centroid))
                pn = None
                if o.person_id:
                    pp = await session.get(Person, o.person_id)
                    pn = pp.name if pp else None
                scored.append((sim, o.id, o.status.value, o.member_count, pn, o.representative_face_id))
            scored.sort(reverse=True)
            log("  vs_clusters top:")
            for sim, oid, st, n, pn, rep in scored[:15]:
                log(f"    {sim:.4f} cluster={oid} {st} n={n} person={pn} rep={rep}")

            # how many between 0.45 and 0.60 (near-miss zone under threshold 0.6)
            near = [x for x in scored if 0.45 <= x[0] < 0.60]
            log(f"  near_miss_0.45-0.60 count={len(near)}")
            named_near = [x for x in near if x[4]]
            log(f"  near_miss_named count={len(named_near)}")
            for sim, oid, st, n, pn, rep in named_near[:10]:
                log(f"    NEAR {sim:.4f} cluster={oid} person={pn} n={n}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as exc:  # noqa: BLE001
        log("ERROR", type(exc).__name__, exc)
        raise
