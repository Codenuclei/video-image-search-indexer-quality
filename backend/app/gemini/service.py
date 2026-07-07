from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass
class SearchCitation:
    file_name: str | None = None
    source: str | None = None
    drive_file_id: str | None = None
    drive_path: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class SearchResult:
    answer: str
    citations: list[SearchCitation]


_CLARIFYING_MARKERS = (
    "need more specific",
    "need more details",
    "could you clarify",
    "could you provide more",
    "please specify",
    "too broad",
    "more specific details",
    "i need more",
    "can you be more specific",
)


class GeminiFileSearchService:
    """Managed multimodal RAG via Gemini File Search stores."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client = None
        self._store_name: str | None = None

    def _client_or_raise(self):
        if not self._settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured")
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self._settings.gemini_api_key)
        return self._client

    def ensure_store(self) -> str:
        if self._store_name:
            return self._store_name

        client = self._client_or_raise()
        display_name = self._settings.gemini_file_search_store_display_name

        for store in client.file_search_stores.list():
            if store.display_name == display_name:
                self._store_name = store.name
                logger.info("Using existing Gemini File Search store: %s", store.name)
                return store.name

        store = client.file_search_stores.create(
            config={
                "display_name": display_name,
                "embedding_model": self._settings.gemini_embedding_model,
            }
        )
        self._store_name = store.name
        logger.info("Created Gemini File Search store: %s", store.name)
        return store.name

    def delete_document(self, document_name: str | None) -> None:
        if not document_name:
            return
        client = self._client_or_raise()
        try:
            client.file_search_stores.documents.delete(name=document_name, config={"force": True})
            logger.info("Deleted Gemini document %s", document_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not delete Gemini document %s: %s", document_name, exc)

    def purge_store(self) -> dict[str, object]:
        """Delete the entire File Search store to reclaim its storage quota.

        The store is recreated lazily on the next upload, so only fresh
        (PDF/doc) content repopulates it.
        """
        client = self._client_or_raise()
        display_name = self._settings.gemini_file_search_store_display_name
        deleted: list[str] = []
        for store in client.file_search_stores.list():
            if store.display_name == display_name:
                try:
                    client.file_search_stores.delete(name=store.name, config={"force": True})
                    deleted.append(store.name)
                    logger.info("Purged Gemini File Search store %s", store.name)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Could not purge store %s: %s", store.name, exc)
        self._store_name = None
        return {"deleted_stores": deleted}

    def upload_file(
        self,
        *,
        local_path: str,
        display_name: str,
        drive_file_id: str,
        drive_path: str,
        mime_type: str,
        person_names: list[str] | None = None,
        extra_metadata: dict[str, str] | None = None,
    ) -> str | None:
        client = self._client_or_raise()
        store_name = self.ensure_store()

        custom_metadata = [
            {"key": "drive_file_id", "string_value": drive_file_id},
            {"key": "drive_path", "string_value": drive_path},
            {"key": "mime_type", "string_value": mime_type},
        ]
        tagged = sorted({name.strip().lower() for name in person_names or [] if name.strip()})
        if tagged:
            custom_metadata.append(
                {"key": "tagged_people", "string_list_value": {"values": tagged}}
            )
        for key, value in (extra_metadata or {}).items():
            if value is not None and str(value).strip():
                custom_metadata.append({"key": key, "string_value": str(value)})

        operation = client.file_search_stores.upload_to_file_search_store(
            file=local_path,
            file_search_store_name=store_name,
            config={
                "display_name": display_name,
                "custom_metadata": custom_metadata,
            },
        )

        deadline = time.monotonic() + self._settings.gemini_upload_timeout_seconds
        while not operation.done:
            if time.monotonic() > deadline:
                raise TimeoutError(f"Gemini indexing timed out for {display_name}")
            time.sleep(self._settings.gemini_upload_poll_seconds)
            operation = client.operations.get(operation)

        if getattr(operation, "error", None):
            raise RuntimeError(f"Gemini indexing failed for {display_name}: {operation.error}")

        response = getattr(operation, "response", None)
        document_name = None
        if response is not None:
            document_name = getattr(response, "name", None) or (
                response.get("name") if isinstance(response, dict) else None
            )
        logger.info("Indexed %s into Gemini (%s)", display_name, document_name or "unknown doc id")
        return document_name

    def search(self, query: str, person_name: str | None = None) -> SearchResult:
        user_query = query.strip()
        retrieval_query = self._retrieval_query(user_query)
        try:
            return self._search_via_generate_content(retrieval_query, person_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("generate_content search failed, falling back to interactions: %s", exc)
            try:
                return self._search_via_interactions(retrieval_query, person_name)
            except Exception:  # noqa: BLE001
                logger.exception("Gemini search failed")
                return SearchResult(answer="", citations=[])

    def search_video_frames(self, query: str, person_name: str | None = None) -> list[SearchCitation]:
        """Retrieve indexed video keyframes (metadata content_kind=video_frame)."""
        retrieval_query = (
            f"Find video frames showing: {query.strip()}. "
            "Only cite indexed video frame images with visible scenes matching the query."
        )
        try:
            result = self._search_via_generate_content(retrieval_query, person_name)
        except Exception:  # noqa: BLE001
            result = self._search_via_interactions(retrieval_query, person_name)
        return [
            c
            for c in result.citations
            if c.metadata.get("content_kind") == "video_frame" and c.metadata.get("timestamp_sec")
        ]

    def describe_image(self, image_path: str, *, timestamp_sec: float) -> str:
        """Short VLM caption for a video keyframe."""
        from google.genai import types

        client = self._client_or_raise()
        with open(image_path, "rb") as fh:
            image_bytes = fh.read()
        response = client.models.generate_content(
            model=self._settings.gemini_model,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                types.Part.from_text(
                    text=(
                        f"This is a frame at {timestamp_sec:.1f}s from a video. "
                        "Describe what is visible in one concise sentence for search indexing."
                    )
                ),
            ],
            config=types.GenerateContentConfig(temperature=0.2),
        )
        return (response.text or "").strip()[:500]

    @staticmethod
    def _retrieval_query(user_query: str) -> str:
        """Short query tuned for Gemini File Search visual retrieval."""
        q = user_query.strip()
        q_lower = q.lower()
        if any(
            w in q_lower
            for w in ("party", "partying", "dancing", "celebration", "birthday", "rooftop", "event")
        ):
            return (
                f"Find indexed photos showing: {q}. "
                "Match the scene, activity, or celebration described."
            )
        if len(q.split()) >= 2:
            return (
                f"Find indexed photos showing: {q}. "
                "Match objects, actions, poses, facial expressions, and activities."
            )
        return (
            f"Find indexed photos where {q} is clearly visible as the main subject. "
            "Only include files that actually show this."
        )

    @staticmethod
    def _dedupe_citations(citations: list[SearchCitation]) -> list[SearchCitation]:
        seen: set[str] = set()
        unique: list[SearchCitation] = []
        for citation in citations:
            key = citation.drive_file_id or citation.file_name or ""
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(citation)
        return unique

    @staticmethod
    def _build_retrieval_prompt(query: str) -> str:
        q = query.strip()
        instructions = (
            "Search the indexed files and list what you find. "
            "Only cite files from the store. Do not ask clarifying questions. "
            'If no relevant files exist, respond with exactly: "No matching files in your Drive index."'
        )
        q_lower = q.lower()
        if q_lower in ("people", "person", "persons", "faces", "face") or any(
            w in q_lower for w in ("people", "person", "face", "portrait", "selfie")
        ):
            instructions += (
                f" For the query '{q}', describe people visible in indexed photos and PDFs, "
                "listing each file name and what you see."
            )
        elif any(w in q_lower for w in ("party", "partying", "dancing", "celebration", "birthday", "rooftop")):
            instructions += (
                f" For the query '{q}', find indexed photos showing this scene or activity. "
                "List each matching file name and cite it."
            )
        else:
            instructions += (
                f" For the query '{q}', find indexed photos and documents that match visually or by content. "
                "Search for objects (e.g. wine glass, cake, microphone), actions (dancing, drinking, hugging), "
                "poses, facial expressions (smiling, laughing), activities, and scenes. "
                "Include party and event photos if they contain the requested object, action, pose, or expression. "
                "List each matching file name and cite it."
            )
        return instructions

    @staticmethod
    def _is_clarifying_response(answer: str) -> bool:
        lower = answer.lower()
        return any(marker in lower for marker in _CLARIFYING_MARKERS)

    def _finalize_answer(self, answer: str, citations: list[SearchCitation], original_query: str) -> str:
        text = (answer or "").strip()
        if not text or text == "No answer returned.":
            if citations:
                return f"Found {len(citations)} matching file(s) in your Drive index for: {original_query}"
            return "No matching files in your Drive index"
        if self._is_clarifying_response(text):
            if citations:
                return f"Found {len(citations)} matching file(s) in your Drive index for: {original_query}"
            return "No matching files in your Drive index"
        return text

    def _metadata_filter(self, person_name: str | None) -> str | None:
        if person_name and person_name.strip():
            escaped = person_name.strip().lower().replace('"', '\\"')
            return f'tagged_people:"{escaped}"'
        return None

    def _search_via_interactions(
        self,
        retrieval_query: str,
        person_name: str | None = None,
    ) -> SearchResult:
        client = self._client_or_raise()
        store_name = self.ensure_store()

        tool: dict = {
            "type": "file_search",
            "file_search_store_names": [store_name],
        }
        metadata_filter = self._metadata_filter(person_name)
        if metadata_filter:
            tool["metadata_filter"] = metadata_filter

        interaction = client.interactions.create(
            model=self._settings.gemini_model,
            input=retrieval_query,
            tools=[tool],
        )

        citations: list[SearchCitation] = []

        for step in interaction.steps or []:
            if getattr(step, "type", None) != "model_output":
                continue
            for block in step.content or []:
                if getattr(block, "type", None) != "text":
                    continue
                for annotation in getattr(block, "annotations", None) or []:
                    citations.append(self._citation_from_annotation(annotation))

        return SearchResult(answer="", citations=self._dedupe_citations(citations))

    def _search_via_generate_content(
        self,
        retrieval_query: str,
        person_name: str | None = None,
    ) -> SearchResult:
        from google.genai import types

        client = self._client_or_raise()
        store_name = self.ensure_store()

        file_search_kwargs: dict = {"file_search_store_names": [store_name]}
        metadata_filter = self._metadata_filter(person_name)
        if metadata_filter:
            file_search_kwargs["metadata_filter"] = metadata_filter

        response = client.models.generate_content(
            model=self._settings.gemini_model,
            contents=retrieval_query,
            config=types.GenerateContentConfig(
                temperature=0.1,
                tools=[types.Tool(file_search=types.FileSearch(**file_search_kwargs))],
            ),
        )

        citations: list[SearchCitation] = []

        for candidate in response.candidates or []:
            grounding = getattr(candidate, "grounding_metadata", None)
            if grounding is None:
                continue
            for chunk in getattr(grounding, "grounding_chunks", None) or []:
                ctx = getattr(chunk, "retrieved_context", None)
                if ctx is None:
                    continue
                meta: dict[str, str] = {}
                drive_file_id = None
                drive_path = None
                for item in getattr(ctx, "custom_metadata", None) or []:
                    key = getattr(item, "key", None)
                    value = getattr(item, "string_value", None)
                    if key and value:
                        meta[key] = value
                        if key == "drive_file_id":
                            drive_file_id = value
                        if key == "drive_path":
                            drive_path = value
                citations.append(
                    SearchCitation(
                        file_name=getattr(ctx, "title", None),
                        source=getattr(ctx, "text", None),
                        drive_file_id=drive_file_id,
                        drive_path=drive_path,
                        metadata=meta,
                    )
                )

        return SearchResult(answer="", citations=self._dedupe_citations(citations))

    @staticmethod
    def _citation_from_annotation(annotation) -> SearchCitation:
        meta: dict[str, str] = {}
        drive_file_id = None
        drive_path = None
        for item in getattr(annotation, "custom_metadata", None) or []:
            key = getattr(item, "key", None)
            value = getattr(item, "string_value", None)
            if key and value:
                meta[key] = value
                if key == "drive_file_id":
                    drive_file_id = value
                if key == "drive_path":
                    drive_path = value
        return SearchCitation(
            file_name=getattr(annotation, "file_name", None),
            source=getattr(annotation, "source", None),
            drive_file_id=drive_file_id,
            drive_path=drive_path,
            metadata=meta,
        )


_service: GeminiFileSearchService | None = None


def get_gemini_service() -> GeminiFileSearchService:
    global _service
    if _service is None:
        _service = GeminiFileSearchService()
    return _service
