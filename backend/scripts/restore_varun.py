"""One-off: restore Varun Mayya person from face 5774 / its cluster."""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.db.models import ClusterStatus, Face, FaceCluster
from app.db.session import get_session_factory
from app.matching.service import name_cluster, update_person


async def main() -> int:
    factory = get_session_factory()
    async with factory() as session:
        face = await session.get(Face, 5774)
        cluster = None
        if face and face.cluster_id:
            cluster = await session.get(FaceCluster, face.cluster_id)
            print(f"face=5774 cluster={face.cluster_id} person={face.person_id}", flush=True)
        if cluster is None:
            cluster = (
                await session.execute(
                    select(FaceCluster).where(FaceCluster.representative_face_id == 5774)
                )
            ).scalar_one_or_none()
            print(f"cluster_by_rep={cluster.id if cluster else None}", flush=True)
        if cluster is None:
            print("ERROR: could not find Varun Mayya cluster", flush=True)
            return 1

        print(
            f"cluster={cluster.id} status={cluster.status} members={cluster.member_count}",
            flush=True,
        )
        if cluster.status == ClusterStatus.NAMED and cluster.person_id:
            person_id = cluster.person_id
            print(f"already named as person {person_id}", flush=True)
        else:
            person = await name_cluster(session, cluster.id, "Varun Mayya")
            await session.commit()
            person_id = person.id
            print(f"created/named person={person_id}", flush=True)

        person = await update_person(session, person_id, set_role=True, role="non_student")
        await session.commit()
        print(
            f"OK id={person.id} name={person.name!r} role={person.role} rep={person.representative_face_id}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
