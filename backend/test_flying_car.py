import asyncio, sys
sys.path.insert(0, '.')

async def test():
    from app.gemini.video_embeddings import embed_text_sync
    from app.qdrant.client import search_frames_sync
    vec = await asyncio.to_thread(embed_text_sync, "flying car")
    hits = await asyncio.to_thread(search_frames_sync, vec, limit=5, min_score=0.30)
    print(f"Qdrant hits: {len(hits)}")
    for h in hits:
        print(f"  ts={h['timestamp']:.0f}s score={h['score']:.4f}")

asyncio.run(test())
