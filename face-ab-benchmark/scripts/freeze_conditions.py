#!/usr/bin/env python3
"""Freeze hardware / software / thread conditions before A/B runs."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"
RESULTS.mkdir(parents=True, exist_ok=True)

# Match OpenCV zoo / production-like CPU threading for fair comparison.
DEFAULT_THREADS = "4"
for key in (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
):
    os.environ.setdefault(key, DEFAULT_THREADS)


def _sysctl(name: str) -> str | None:
    try:
        out = subprocess.check_output(["sysctl", "-n", name], text=True).strip()
        return out or None
    except Exception:
        return None


def _pkg_versions() -> dict[str, str]:
    versions: dict[str, str] = {"python": sys.version.split()[0]}
    for mod_name, attr in (
        ("onnxruntime", "__version__"),
        ("cv2", "__version__"),
        ("numpy", "__version__"),
        ("insightface", "__version__"),
    ):
        try:
            mod = __import__(mod_name)
            versions[mod_name] = str(getattr(mod, attr, "unknown"))
        except Exception as exc:  # noqa: BLE001
            versions[mod_name] = f"unavailable: {exc}"
    return versions


def main() -> None:
    mem_bytes = _sysctl("hw.memsize")
    mem_gb = round(int(mem_bytes) / (1024**3), 1) if mem_bytes and mem_bytes.isdigit() else None
    payload = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "cpu_brand": _sysctl("machdep.cpu.brand_string"),
        "physical_cores": _sysctl("hw.physicalcpu"),
        "logical_cores": _sysctl("hw.logicalcpu"),
        "ram_gb": mem_gb,
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "mac_ver": platform.mac_ver(),
        },
        "packages": _pkg_versions(),
        "onnx_runtime_threads": int(os.environ.get("OMP_NUM_THREADS", DEFAULT_THREADS)),
        "env_thread_vars": {
            k: os.environ.get(k)
            for k in (
                "OMP_NUM_THREADS",
                "MKL_NUM_THREADS",
                "OPENBLAS_NUM_THREADS",
                "NUMEXPR_NUM_THREADS",
                "VECLIB_MAXIMUM_THREADS",
            )
        },
        "protocol": {
            "input_resolution_note": "Images used as-is; timing includes decode unless noted.",
            "detection_size": {"ours_buffalo_l": [640, 640], "yunet_default": [320, 320]},
            "timing_includes": [
                "image_decode",
                "preprocess",
                "detection",
                "alignment",
                "embedding",
                "postprocess",
            ],
            "warmup_excluded": True,
            "warmup_runs": 20,
            "measured_runs_speed": 200,
            "opencv_zoo_method": "mean after warm-up; full preprocess+forward+postprocess",
            "our_architecture": {
                "pack": "buffalo_l",
                "detector": "SCRFD-10G (det_10g.onnx)",
                "recognizer": "ArcFace R50 (w600k_r50.onnx)",
                "det_size": [640, 640],
                "person_match_threshold": 0.6,
                "providers": ["CPUExecutionProvider"],
            },
        },
    }
    out = RESULTS / "conditions.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
