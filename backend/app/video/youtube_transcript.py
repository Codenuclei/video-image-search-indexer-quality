from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from html import unescape

import httpx

from app.video.vtt import VttCue, parse_vtt

logger = logging.getLogger(__name__)

_YT_ID_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]")
_API_KEY_RE = re.compile(r'"INNERTUBE_API_KEY"\s*:\s*"([a-zA-Z0-9_-]+)"')
_PLAYER_MARKER = "ytInitialPlayerResponse"

_INNERTUBE_PLAYER_URL = "https://www.youtube.com/youtubei/v1/player?key={api_key}"
_INNERTUBE_NEXT_URL = "https://www.youtube.com/youtubei/v1/next?key={api_key}"
_INNERTUBE_GET_TRANSCRIPT_URL = "https://www.youtube.com/youtubei/v1/get_transcript?key={api_key}"

_ANDROID_CONTEXT = {
    "client": {
        "clientName": "ANDROID",
        "clientVersion": "20.10.38",
    }
}

_WEB_CONTEXT = {
    "client": {
        "clientName": "WEB",
        "clientVersion": "2.20250312.01.00",
        "hl": "en",
        "gl": "US",
    }
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
}


def youtube_id_from_filename(name: str) -> str | None:
    """Extract YouTube video id from Drive filename, e.g. `Title [j2lcnmLGSxQ].webm`."""
    match = _YT_ID_RE.search(name)
    return match.group(1) if match else None


def _extract_api_key(html: str) -> str | None:
    match = _API_KEY_RE.search(html)
    return match.group(1) if match else None


def _extract_player_response(html: str) -> dict:
    idx = html.find(_PLAYER_MARKER)
    if idx < 0:
        raise ValueError("ytInitialPlayerResponse not found")
    start = html.find("{", idx)
    if start < 0:
        raise ValueError("ytInitialPlayerResponse JSON start not found")
    depth = 0
    for i in range(start, len(html)):
        ch = html[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(html[start : i + 1])
    raise ValueError("ytInitialPlayerResponse JSON incomplete")


def _pick_caption_track(tracks: list[dict], lang: str = "en") -> dict | None:
    if not tracks:
        return None
    lang = lang.lower()
    # Prefer non-ASR (manual) English when both exist.
    exact: list[dict] = []
    for track in tracks:
        code = (track.get("languageCode") or "").lower()
        if code == lang or code.startswith(f"{lang}-"):
            exact.append(track)
    if exact:
        for track in exact:
            if track.get("kind") != "asr":
                return track
        return exact[0]
    for track in tracks:
        if track.get("kind") != "asr":
            return track
    return tracks[0]


def _with_tlang(url: str, tlang: str) -> str:
    clean = re.sub(r"([&?])tlang=[^&]*", r"\1", url or "")
    clean = clean.rstrip("&?")
    sep = "&" if "?" in clean else "?"
    return f"{clean}{sep}tlang={tlang}"


def _caption_tracks_from_player(player: dict) -> list[dict]:
    return (
        player.get("captions", {})
        .get("playerCaptionsTracklistRenderer", {})
        .get("captionTracks", [])
        or []
    )


def _json3_to_cues(data: dict) -> list[VttCue]:
    cues: list[VttCue] = []
    for event in data.get("events", []):
        segs = event.get("segs")
        if not segs:
            continue
        text = unescape("".join(seg.get("utf8", "") for seg in segs)).strip()
        if not text or text == "\n":
            continue
        start_ms = int(event.get("tStartMs", 0))
        dur_ms = int(event.get("dDurationMs", 0) or 0)
        start = start_ms / 1000.0
        end = (start_ms + dur_ms) / 1000.0 if dur_ms > 0 else start + 2.0
        cues.append(VttCue(start_sec=start, end_sec=end, text=text))
    return cues


def _xml_timedtext_to_cues(raw: str) -> list[VttCue]:
    cues: list[VttCue] = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    for elem in root.iter("text"):
        text = unescape((elem.text or "")).strip()
        if not text:
            continue
        start = float(elem.get("start", "0") or 0)
        dur = float(elem.get("dur", "0") or 0)
        end = start + dur if dur > 0 else start + 2.0
        cues.append(VttCue(start_sec=start, end_sec=end, text=text))
    return cues


def _parse_time_text(value: str) -> float:
    parts = value.strip().split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return 0.0
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 1:
        return nums[0]
    return 0.0


def _get_transcript_response_to_cues(data: dict) -> list[VttCue]:
    """Parse /youtubei/v1/get_transcript response (InnerTube JS panel path)."""
    cues: list[VttCue] = []
    try:
        segments = (
            data["actions"][0]["updateEngagementPanelAction"]["content"]["transcriptRenderer"][
                "content"
            ]["transcriptSearchPanelRenderer"]["body"]["transcriptSegmentListRenderer"][
                "initialSegments"
            ]
        )
    except (KeyError, IndexError, TypeError):
        return []

    for segment in segments:
        renderer = segment.get("transcriptSegmentRenderer")
        if not renderer:
            continue
        start_ms = renderer.get("startMs")
        if start_ms is not None:
            start = float(start_ms) / 1000.0
        else:
            start_text = (
                renderer.get("startTimeText", {}).get("simpleText")
                or renderer.get("startTimeText", {}).get("runs", [{}])[0].get("text")
                or "0"
            )
            start = _parse_time_text(str(start_text))

        runs = renderer.get("snippet", {}).get("runs", [])
        text = unescape("".join(run.get("text", "") for run in runs)).strip()
        if not text:
            continue

        end_ms = renderer.get("endMs")
        end = float(end_ms) / 1000.0 if end_ms is not None else start + 2.0
        cues.append(VttCue(start_sec=start, end_sec=end, text=text))
    return cues


def _extract_get_transcript_params(next_data: dict) -> str | None:
    for panel in next_data.get("engagementPanels", []):
        section = panel.get("engagementPanelSectionListRenderer", {})
        if section.get("panelIdentifier") != "engagement-panel-searchable-transcript":
            continue
        try:
            return section["content"]["continuationItemRenderer"]["continuationEndpoint"][
                "getTranscriptEndpoint"
            ]["params"]
        except (KeyError, TypeError):
            return None
    return None


async def _innertube_post(client: httpx.AsyncClient, url: str, body: dict) -> dict:
    resp = await client.post(url, json=body)
    resp.raise_for_status()
    return resp.json()


async def _fetch_player_via_innertube(
    client: httpx.AsyncClient, video_id: str, api_key: str
) -> dict:
    return await _innertube_post(
        client,
        _INNERTUBE_PLAYER_URL.format(api_key=api_key),
        {"context": _ANDROID_CONTEXT, "videoId": video_id},
    )


async def _fetch_cues_from_caption_url(
    client: httpx.AsyncClient, base_url: str, *, video_id: str
) -> list[VttCue]:
    clean_url = re.sub(r"&fmt=\w+$", "", base_url)
    referer = {"Referer": f"https://www.youtube.com/watch?v={video_id}"}

    xml_resp = await client.get(clean_url, headers=referer)
    if xml_resp.status_code == 200 and xml_resp.text.strip():
        cues = _xml_timedtext_to_cues(xml_resp.text)
        if cues:
            return cues

    json_resp = await client.get(f"{clean_url}&fmt=json3", headers=referer)
    if json_resp.status_code == 200 and json_resp.text.strip():
        try:
            cues = _json3_to_cues(json_resp.json())
            if cues:
                return cues
        except Exception:  # noqa: BLE001
            pass

    vtt_resp = await client.get(f"{clean_url}&fmt=vtt", headers=referer)
    if vtt_resp.status_code == 200 and vtt_resp.text.strip():
        cues = parse_vtt(vtt_resp.text)
        if cues:
            return cues
    return []


async def _fetch_cues_via_get_transcript(
    client: httpx.AsyncClient, video_id: str, api_key: str
) -> list[VttCue]:
    next_data = await _innertube_post(
        client,
        _INNERTUBE_NEXT_URL.format(api_key=api_key),
        {"context": _WEB_CONTEXT, "videoId": video_id},
    )
    params = _extract_get_transcript_params(next_data)
    if not params:
        return []

    transcript_data = await _innertube_post(
        client,
        _INNERTUBE_GET_TRANSCRIPT_URL.format(api_key=api_key),
        {"context": _WEB_CONTEXT, "params": params},
    )
    return _get_transcript_response_to_cues(transcript_data)


def _cues_look_like_lang(cues: list[VttCue], lang: str) -> bool:
    """Heuristic: for lang=en, require mostly Latin / non-Indic text."""
    if lang.lower() not in ("en", "en-us", "en-gb"):
        return True
    sample = " ".join((c.text or "") for c in cues[:20])
    if not sample.strip():
        return False
    # Inline check to avoid importing search helpers from video layer.
    if re.search(r"[\u0900-\u097F]", sample):
        return False
    letters = re.findall(r"[^\W\d_]", sample, flags=re.UNICODE)
    if not letters:
        return True
    latin = sum(1 for ch in letters if ("A" <= ch <= "Z") or ("a" <= ch <= "z"))
    return (latin / len(letters)) >= 0.75


async def fetch_youtube_captions(video_id: str, *, lang: str = "en") -> list[VttCue]:
    """
    Fetch public YouTube captions via InnerTube (player + timedtext XML, then get_transcript).
    Prefers the requested language track; when lang=en and only another language exists,
    retries the timedtext URL with tlang=en (YouTube auto-translate).
    No Gemini / no extra Python deps beyond httpx.
    """
    embed_url = f"https://www.youtube.com/embed/{video_id}"
    watch_url = f"https://www.youtube.com/watch?v={video_id}"

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, headers=_HEADERS) as client:
        page = await client.get(embed_url)
        page.raise_for_status()
        api_key = _extract_api_key(page.text)
        if not api_key:
            page = await client.get(watch_url)
            page.raise_for_status()
            api_key = _extract_api_key(page.text)

        player: dict | None = None
        if api_key:
            try:
                player = await _fetch_player_via_innertube(client, video_id, api_key)
            except Exception as exc:  # noqa: BLE001
                logger.warning("YouTube %s: innertube player failed: %s", video_id, exc)

        if player is None:
            try:
                player = _extract_player_response(page.text)
            except Exception as exc:  # noqa: BLE001
                logger.warning("YouTube %s: player response parse failed: %s", video_id, exc)
                player = {}

        tracks = _caption_tracks_from_player(player)
        track = _pick_caption_track(tracks, lang=lang)
        if track and track.get("baseUrl"):
            cues = await _fetch_cues_from_caption_url(client, track["baseUrl"], video_id=video_id)
            if cues and _cues_look_like_lang(cues, lang):
                logger.info("YouTube %s: %d cues via innertube timedtext (%s)", video_id, len(cues), lang)
                return cues
            # English requested but track missing / non-English → translate via tlang.
            if lang.lower().startswith("en"):
                source = track if track.get("baseUrl") else None
                if source is None and tracks:
                    source = tracks[0]
                if source and source.get("baseUrl"):
                    translated = await _fetch_cues_from_caption_url(
                        client,
                        _with_tlang(source["baseUrl"], "en"),
                        video_id=video_id,
                    )
                    if translated and _cues_look_like_lang(translated, "en"):
                        logger.info(
                            "YouTube %s: %d cues via timedtext tlang=en",
                            video_id,
                            len(translated),
                        )
                        return translated
            if cues:
                logger.info(
                    "YouTube %s: %d cues via timedtext (lang may not match %s)",
                    video_id,
                    len(cues),
                    lang,
                )
                return cues

        # No preferred track: try translating any available track to English.
        if lang.lower().startswith("en") and tracks:
            for candidate in tracks:
                base = candidate.get("baseUrl")
                if not base:
                    continue
                translated = await _fetch_cues_from_caption_url(
                    client,
                    _with_tlang(base, "en"),
                    video_id=video_id,
                )
                if translated and _cues_look_like_lang(translated, "en"):
                    logger.info(
                        "YouTube %s: %d cues via alternate track tlang=en",
                        video_id,
                        len(translated),
                    )
                    return translated

        if api_key:
            try:
                cues = await _fetch_cues_via_get_transcript(client, video_id, api_key)
                if cues:
                    logger.info("YouTube %s: %d cues via innertube get_transcript", video_id, len(cues))
                    return cues
            except Exception as exc:  # noqa: BLE001
                logger.warning("YouTube %s: get_transcript failed: %s", video_id, exc)

    logger.info("YouTube %s: caption fetch returned no cues", video_id)
    return []
