"""Lightweight pixelation / blockiness detector (numpy + OpenCV only).

Screens out both **intentional block censorship** (mosaic over faces/text) and
**severe full-frame pixelation** / macroblock artifacts. Standard Laplacian
blur detectors alone are not enough — harsh geometric grid lines can keep
variance high while looking obviously pixelated.

Signals (no ML / no TensorFlow):

1. **NEAREST-tile self-similarity** — pixels that share an OpenCV
   ``INTER_NEAREST`` upscale source cell must be nearly constant on mosaics
   (exact flat tiles ⇒ sim≈1, MAD≈0).
2. **Block-boundary excess** — energy on N×N grid borders vs intra-block
   edges (classic JPEG/MPEG blockiness).
3. **Offset-decimation uniformity** — phase-shifted subsamples match when
   the frame is a mosaic of flat blocks.
4. **Macroblock edge discontinuity** — frequency of sharp horizontal and
   vertical edge jumps on candidate block grids (the CV approach recommended
   for intentional block pixelation; blur detectors miss this).
5. **Phase-aware block flatness / localized mosaic coverage** — sliding-window
   scan over candidate block phases that flags a frame when a contiguous
   region has flat mosaic cells with diverse neighbor means (censorship
   patches over faces/text). Laplacian variance remains an upstream soft-blur
   gate in ``score_frame_quality``.

Rejection is gated conservatively (prefer false negatives over rejecting
good carousel frames). Soft / low-detail frames are also handled upstream by
the Laplacian sharpness gate in ``score_frame_quality``.
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

# Reject when combined score >= this AND multi-signal gates pass.
PIXELATION_SCORE_THRESHOLD = 0.72

# Primary mosaic gate (tile self-similarity). Clean sharp thumbs peak ~0.98;
# mosaics (even after JPEG q70–85) typically sit ≥0.985 with low MAD.
_TILE_SIM_REJECT = 0.985
_TILE_MAD_REJECT = 4.0

# Macroblock / localized-censorship gates (OpenCV edge discontinuity).
# Require strong mosaic self-similarity so heavy JPEG compression alone
# (high block_excess, moderate macroblock lift) does not false-positive.
_MACROBLOCK_REJECT = 0.62
_MACROBLOCK_TILE_SIM_MIN = 0.94
_LOCAL_MOSAIC_FRAC_REJECT = 0.14
_LOCAL_MOSAIC_MIN_TILES = 4
_LOCAL_RESIDUAL_REJECT = 0.28

_ANALYZE_MAX_DIM = 960  # only integer-stride below; avoids smearing mosaics
_BLOCK_SIZES = (6, 8, 10, 12, 16)
_TILE_FACTORS = (4, 5, 6, 7, 8, 10, 12, 16)
_OFFSET_SKIPS = (3, 4, 5, 6, 8)
_MACRO_BLOCK_SIZES = (8, 10, 12, 16, 20, 24)
_LOCAL_WINDOW = 40
_LOCAL_STRIDE = 20
_RESIDUAL_FACTORS = (6, 8, 10, 12, 16)


def _to_gray(image: np.ndarray) -> np.ndarray:
    if image is None or getattr(image, "size", 0) == 0:
        raise ValueError("empty image")
    if image.ndim == 2:
        gray = image
    elif image.ndim == 3 and image.shape[2] >= 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        raise ValueError("unsupported image shape")
    if gray.dtype != np.uint8:
        gray = np.clip(gray, 0, 255).astype(np.uint8)
    return gray


def _downscale_for_speed(gray: np.ndarray, max_dim: int = _ANALYZE_MAX_DIM) -> np.ndarray:
    """Optional integer-stride shrink. Non-integer resize destroys mosaic tiles."""
    h, w = gray.shape[:2]
    m = max(h, w)
    if m <= max_dim:
        return gray
    factor = int(np.ceil(m / float(max_dim)))
    if factor <= 1:
        return gray
    return gray[::factor, ::factor]


def _block_excess(gray: np.ndarray, block_size: int) -> float:
    """Excess border/intra energy vs chance baseline for a fixed grid."""
    gf = gray.astype(np.float64)
    h, w = gf.shape
    bs = int(block_size)
    if h < bs * 2 or w < bs * 2:
        return 0.0

    border = 0.0
    for x in range(bs, w, bs):
        border += float(np.sum(np.abs(gf[:, x - 1] - gf[:, x])))
    for y in range(bs, h, bs):
        border += float(np.sum(np.abs(gf[y - 1, :] - gf[y, :])))

    dh = np.abs(np.diff(gf, axis=1))
    dv = np.abs(np.diff(gf, axis=0))
    mask_h = np.ones_like(dh, dtype=bool)
    for x in range(bs, w, bs):
        if x - 1 < mask_h.shape[1]:
            mask_h[:, x - 1] = False
    mask_v = np.ones_like(dv, dtype=bool)
    for y in range(bs, h, bs):
        if y - 1 < mask_v.shape[0]:
            mask_v[y - 1, :] = False

    intra = float(dh[mask_h].sum() + dv[mask_v].sum()) + 1e-6
    ratio = border / intra
    baseline = 1.0 / max(bs - 1, 1)
    return float(min(20.0, max(0.0, ratio / baseline - 1.0)))


def _max_block_excess(gray: np.ndarray) -> float:
    return float(max((_block_excess(gray, bs) for bs in _BLOCK_SIZES), default=0.0))


def _tile_self_similarity(gray: np.ndarray) -> tuple[float, float, int | None]:
    """Best constant-per-NEAREST-tile self-similarity.

    Uses OpenCV ``INTER_NEAREST`` dest→src mapping ``floor(x * sw / w)``.
    Returns ``(similarity, mean_abs_dev, best_factor)``.
    """
    g = gray.astype(np.float64)
    h, w = gray.shape
    var = float(np.var(g)) + 1e-6
    best_sim = 0.0
    best_mad = 999.0
    best_f: int | None = None
    for f in _TILE_FACTORS:
        if min(h, w) < f * 3:
            continue
        sh, sw = max(1, h // f), max(1, w // f)
        ix = np.minimum(sw - 1, (np.arange(w) * sw) // w)
        iy = np.minimum(sh - 1, (np.arange(h) * sh) // h)
        labels = iy[:, None] * sw + ix[None, :]
        flat_g = g.ravel()
        flat_l = labels.ravel().astype(np.int64)
        nlab = sh * sw
        counts = np.bincount(flat_l, minlength=nlab).astype(np.float64) + 1e-9
        sums = np.bincount(flat_l, weights=flat_g, minlength=nlab)
        means = sums / counts
        recon = means[labels]
        err = np.abs(g - recon)
        mse = float(np.mean(err ** 2))
        mad = float(np.mean(err))
        sim = 1.0 / (1.0 + 4.0 * mse / var)
        if sim > best_sim:
            best_sim = sim
            best_mad = mad
            best_f = int(f)
    return float(best_sim), float(best_mad), best_f


def _offset_uniformity(gray: np.ndarray) -> float:
    """High when phase-shifted decimations match (flat mosaic blocks)."""
    g = gray.astype(np.float64)
    h, w = g.shape
    contrast = float(np.std(g)) + 1e-6
    best = 0.0
    for s in _OFFSET_SKIPS:
        if min(h, w) < s * 4:
            continue
        a = g[::s, ::s]
        b = g[::s, 1::s]
        mh, mw = min(a.shape[0], b.shape[0]), min(a.shape[1], b.shape[1])
        a, b = a[:mh, :mw], b[:mh, :mw]
        c = g[::s, ::s]
        d = g[1::s, ::s]
        mh2, mw2 = min(c.shape[0], d.shape[0]), min(c.shape[1], d.shape[1])
        c, d = c[:mh2, :mw2], d[:mh2, :mw2]
        if a.size < 16 or c.size < 16:
            continue
        mad = 0.5 * (
            float(np.mean(np.abs(a - b))) + float(np.mean(np.abs(c - d)))
        )
        score = max(0.0, 1.0 - mad / (0.25 * contrast))
        if score > best:
            best = score
    return float(best)


def _macroblock_edge_score(gray: np.ndarray) -> tuple[float, int | None]:
    """Score sharp H/V discontinuities on candidate macroblock grids.

    Intentional block censorship and MPEG-style mosaics produce strong
    aligned edge jumps at block borders while interiors stay flat. Returns
    ``(score in [0,1], best_block_size)``.
    """
    g = gray.astype(np.float64)
    h, w = g.shape
    dh = np.abs(np.diff(g, axis=1))
    dv = np.abs(np.diff(g, axis=0))
    if dh.size == 0 or dv.size == 0:
        return 0.0, None

    # Strong edges only — soft photo gradients should not dominate.
    thr_h = float(np.percentile(dh, 85)) + 1e-6
    thr_v = float(np.percentile(dv, 85)) + 1e-6
    strong_h = dh >= max(thr_h, 12.0)
    strong_v = dv >= max(thr_v, 12.0)
    best = 0.0
    best_bs: int | None = None
    for bs in _MACRO_BLOCK_SIZES:
        if h < bs * 3 or w < bs * 3:
            continue
        # Fraction of strong edges that land exactly on the block grid.
        grid_h_cols = list(range(bs - 1, w - 1, bs))
        grid_v_rows = list(range(bs - 1, h - 1, bs))
        if not grid_h_cols or not grid_v_rows:
            continue
        on_h = float(sum(strong_h[:, x].mean() for x in grid_h_cols) / len(grid_h_cols))
        on_v = float(sum(strong_v[y, :].mean() for y in grid_v_rows) / len(grid_v_rows))
        # Off-grid strong-edge rate (chance baseline).
        off_h_mask = np.ones(w - 1, dtype=bool)
        off_h_mask[grid_h_cols] = False
        off_v_mask = np.ones(h - 1, dtype=bool)
        off_v_mask[grid_v_rows] = False
        off_h = float(strong_h[:, off_h_mask].mean()) if off_h_mask.any() else 1e-6
        off_v = float(strong_v[off_v_mask, :].mean()) if off_v_mask.any() else 1e-6
        # How much more often strong edges sit on the grid vs elsewhere.
        lift_h = on_h / (off_h + 1e-6)
        lift_v = on_v / (off_v + 1e-6)
        lift = 0.5 * (lift_h + lift_v)
        # Also require interiors relatively flat vs borders (mosaic tiles).
        border = 0.0
        for x in range(bs, w, bs):
            border += float(np.mean(np.abs(g[:, x - 1] - g[:, x])))
        for y in range(bs, h, bs):
            border += float(np.mean(np.abs(g[y - 1, :] - g[y, :])))
        n_borders = (w // bs) + (h // bs)
        border_mean = border / max(1, n_borders)
        # Sample a few tile interiors.
        interior = []
        for y0 in range(2, h - bs, bs):
            for x0 in range(2, w - bs, bs):
                tile = g[y0 : y0 + bs - 2, x0 : x0 + bs - 2]
                if tile.size:
                    interior.append(float(np.std(tile)))
        interior_std = float(np.mean(interior)) if interior else 999.0
        interior_flat = max(0.0, 1.0 - interior_std / 18.0)
        score = float(
            min(
                1.0,
                0.45 * min(1.0, (lift - 1.0) / 2.5)
                + 0.35 * min(1.0, border_mean / 28.0)
                + 0.20 * interior_flat,
            )
        )
        if score > best:
            best = score
            best_bs = int(bs)
    return best, best_bs


def _block_flatness_score(gray: np.ndarray) -> tuple[float, int | None]:
    """Phase-aware mosaic score: flat cells + diverse cell means.

    Unlike origin-aligned NEAREST checks, this tries block-grid phases so
    a censorship patch not aligned to the crop origin still scores high.
    """
    g = gray.astype(np.float64)
    h, w = g.shape
    best = 0.0
    best_bs: int | None = None
    for bs in (8, 10, 12, 16):
        if min(h, w) < bs * 3:
            continue
        # Coarser phase sampling for speed; still catches mosaic grids.
        step = 2 if bs <= 10 else max(2, bs // 4)
        for oy in range(0, bs, step):
            for ox in range(0, bs, step):
                flats: list[float] = []
                means: list[float] = []
                y = oy
                while y + bs <= h:
                    x = ox
                    while x + bs <= w:
                        cell = g[y : y + bs, x : x + bs]
                        flats.append(float(np.std(cell)))
                        means.append(float(np.mean(cell)))
                        x += bs
                    y += bs
                if len(flats) < 6:
                    continue
                flat_mean = float(np.mean(flats))
                mean_std = float(np.std(means))
                # Mosaic: nearly constant cells, but neighboring cells differ.
                if mean_std < 8.0:
                    continue
                flat_term = max(0.0, 1.0 - flat_mean / 6.0)
                contrast_term = min(1.0, mean_std / 25.0)
                score = float(0.65 * flat_term + 0.35 * contrast_term)
                if score > best:
                    best = score
                    best_bs = int(bs)
                    if best >= 0.88:
                        return best, best_bs
    return best, best_bs


def _censorship_patch_score(gray: np.ndarray) -> float:
    """Score intentional block-censorship regions (faces/text mosaics).

    Scans windows with a phase-aware block-flatness check so mosaic patches
    still trigger when the crop origin is not on the mosaic grid.
    """
    h, w = gray.shape
    win = 56
    stride = 28
    if h < win or w < win:
        score, _ = _block_flatness_score(gray)
        return float(score)

    hits = 0
    checked = 0
    best_local = 0.0
    for y in range(0, h - win + 1, stride):
        for x in range(0, w - win + 1, stride):
            tile = gray[y : y + win, x : x + win]
            if float(np.std(tile)) < 10.0:
                continue
            checked += 1
            score, _ = _block_flatness_score(tile)
            best_local = max(best_local, score)
            if score >= 0.70:
                hits += 1
                if hits >= 3 and best_local >= 0.75:
                    # Enough evidence of a mosaic patch — stop early.
                    return float(min(1.0, 0.55 + 0.45 * best_local + 0.05 * hits))
    if checked == 0:
        return float(best_local)
    frac = hits / checked
    if hits >= 1 and best_local >= 0.70:
        return float(min(1.0, 0.55 + 0.45 * best_local + 0.05 * max(0, hits - 1)))
    if hits >= 2 and 0.05 <= frac <= 0.55:
        return float(min(1.0, 0.40 + 0.85 * frac + 0.1 * hits))
    if frac >= 0.55:
        return float(min(1.0, 0.35 + 0.5 * frac))
    return float(max(best_local * 0.5, frac))


def _localized_mosaic_fraction(gray: np.ndarray) -> tuple[float, int, float, float]:
    """Detect intentional mosaic patches (censorship) on otherwise clean frames.

    Returns
    ``(flagged_tile_fraction, flagged_count, residual_patch_score, censorship_score)``.

    Uses the phase-aware censorship scan as the primary local signal; avoids a
    second heavy per-window pass of tile/macroblock features.
    """
    censorship = _censorship_patch_score(gray)
    # Map censorship score into the legacy local-frac fields for callers/stats.
    if censorship >= 0.50:
        frac = min(1.0, 0.15 + 0.5 * censorship)
        tiles = max(3, int(round(frac * 12)))
        return frac, tiles, censorship, censorship
    return 0.0, 0, 0.0, censorship


def _laplacian_var(gray: np.ndarray) -> float:
    """Variance of Laplacian — low on soft blur / low-quality pixel mush."""
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _combine_score(
    tile_sim: float,
    block_excess: float,
    offset_u: float,
    macroblock: float,
    local_frac: float,
) -> float:
    return float(
        0.38 * tile_sim
        + 0.18 * min(1.0, block_excess / 4.0)
        + 0.18 * offset_u
        + 0.16 * macroblock
        + 0.10 * min(1.0, local_frac / 0.35)
    )


def pixelation_details(image: np.ndarray) -> dict[str, Any]:
    """Compute pixelation signals and a combined score in ``[0, 1]``.

    Higher score ⇒ more pixelated / blocky. Flat / tiny images return score 0
    (not rejected here — upstream exposure/contrast filters handle those).
    """
    gray = _downscale_for_speed(_to_gray(image))
    h, w = gray.shape
    empty = {
        "score": 0.0,
        "tile_similarity": 0.0,
        "tile_mad": 0.0,
        "block_excess": 0.0,
        "offset_uniformity": 0.0,
        "macroblock_score": 0.0,
        "macroblock_size": None,
        "local_mosaic_frac": 0.0,
        "local_mosaic_tiles": 0,
        "residual_patch_score": 0.0,
        "censorship_score": 0.0,
        "laplacian_var": 0.0,
        "scale_similarity": 0.0,  # alias of tile_similarity
        "best_scale_factor": None,
        "skipped": None,
    }
    if h < 48 or w < 48:
        empty["skipped"] = "tiny"
        return empty

    contrast = float(np.std(gray.astype(np.float64)))
    if contrast < 6.0:
        empty["skipped"] = "flat"
        return empty

    tile_sim, tile_mad, best_f = _tile_self_similarity(gray)
    block_ex = _max_block_excess(gray)
    offset_u = _offset_uniformity(gray)
    macro_s, macro_bs = _macroblock_edge_score(gray)
    local_frac, local_n, residual_patch, censorship = _localized_mosaic_fraction(gray)
    lap = _laplacian_var(gray)
    score = _combine_score(tile_sim, block_ex, offset_u, macro_s, max(local_frac, censorship))
    return {
        "score": score,
        "tile_similarity": tile_sim,
        "tile_mad": tile_mad,
        "block_excess": block_ex,
        "offset_uniformity": offset_u,
        "macroblock_score": macro_s,
        "macroblock_size": macro_bs,
        "local_mosaic_frac": local_frac,
        "local_mosaic_tiles": local_n,
        "residual_patch_score": residual_patch,
        "censorship_score": censorship,
        "laplacian_var": lap,
        "scale_similarity": tile_sim,
        "best_scale_factor": best_f,
        "skipped": None,
    }


def pixelation_score(image: np.ndarray) -> float:
    """Return a pixelation score in ``[0, 1]`` (higher = more pixelated)."""
    return float(pixelation_details(image)["score"])


def _passes_pixelation_gates(
    details: dict[str, Any],
    *,
    threshold: float = PIXELATION_SCORE_THRESHOLD,
) -> bool:
    """Multi-signal reject gate (conservative — prefer FN over FP)."""
    if details.get("skipped"):
        return False
    score = float(details["score"])
    tile_sim = float(details.get("tile_similarity") or details.get("scale_similarity") or 0.0)
    tile_mad = float(details.get("tile_mad") or 999.0)
    be = float(details["block_excess"])
    ou = float(details["offset_uniformity"])
    mb = float(details.get("macroblock_score") or 0.0)
    local_frac = float(details.get("local_mosaic_frac") or 0.0)
    local_n = int(details.get("local_mosaic_tiles") or 0)
    thr = float(threshold)

    # Primary: near-perfect constant NEAREST tiles (true full-frame mosaics).
    if tile_sim >= _TILE_SIM_REJECT and tile_mad <= _TILE_MAD_REJECT:
        return True

    # Intentional censorship / macroblock grid (sharp H/V discontinuities).
    # Require mosaic-like tile self-similarity so harsh JPEG grids alone do not trip.
    if (
        mb >= _MACROBLOCK_REJECT
        and tile_sim >= _MACROBLOCK_TILE_SIM_MIN
        and (be >= 1.0 or ou >= 0.65)
    ):
        return True

    # Localized mosaic patch (face/text censorship) on an otherwise clean frame.
    residual_patch = float(details.get("residual_patch_score") or 0.0)
    censorship = float(details.get("censorship_score") or 0.0)
    if local_frac >= _LOCAL_MOSAIC_FRAC_REJECT and local_n >= _LOCAL_MOSAIC_MIN_TILES:
        return True
    if residual_patch >= _LOCAL_RESIDUAL_REJECT and local_n >= 2:
        return True
    if censorship >= 0.50:
        return True

    # Secondary: classic aligned block grid + offset lock.
    return bool(
        score >= thr
        and (
            (be >= 1.2 and ou >= 0.75 and tile_sim >= 0.90)
            or (be >= 2.5 and ou >= 0.90 and tile_sim >= 0.92)
            or (mb >= 0.55 and be >= 1.2 and tile_sim >= 0.94)
        )
    )


def evaluate_pixelation(
    image: np.ndarray,
    *,
    threshold: float = PIXELATION_SCORE_THRESHOLD,
) -> tuple[bool, dict[str, Any]]:
    """Return ``(is_pixelated, details)`` with a single analysis pass."""
    details = pixelation_details(image)
    flag = _passes_pixelation_gates(details, threshold=threshold)
    details["pixelated"] = flag
    return flag, details


def is_pixelated(
    image: np.ndarray,
    *,
    threshold: float = PIXELATION_SCORE_THRESHOLD,
) -> bool:
    """True when the image looks clearly pixelated / block-mosaic."""
    flag, _ = evaluate_pixelation(image, threshold=threshold)
    return flag


def is_pixelated_bytes(
    jpeg_bytes: bytes | None,
    *,
    threshold: float = PIXELATION_SCORE_THRESHOLD,
) -> tuple[bool, dict[str, Any]]:
    """Decode JPEG/PNG bytes and run ``is_pixelated``; returns ``(flag, details)``."""
    details: dict[str, Any] = {
        "score": 0.0,
        "tile_similarity": 0.0,
        "tile_mad": 0.0,
        "block_excess": 0.0,
        "offset_uniformity": 0.0,
        "macroblock_score": 0.0,
        "macroblock_size": None,
        "local_mosaic_frac": 0.0,
        "local_mosaic_tiles": 0,
        "residual_patch_score": 0.0,
        "censorship_score": 0.0,
        "laplacian_var": 0.0,
        "scale_similarity": 0.0,
        "best_scale_factor": None,
        "skipped": None,
        "pixelated": False,
    }
    if not jpeg_bytes:
        details["skipped"] = "missing"
        return False, details
    try:
        arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if bgr is None or bgr.size == 0:
            details["skipped"] = "decode"
            return False, details
        return evaluate_pixelation(bgr, threshold=threshold)
    except Exception as exc:  # noqa: BLE001
        details["skipped"] = f"error:{str(exc)[:40]}"
        return False, details
