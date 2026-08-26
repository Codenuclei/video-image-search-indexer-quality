# Face A/B benchmark (local only)

Isolated workspace for controlled detector/recognizer A/B tests against our
production InsightFace `buffalo_l` stack (SCRFD-10G + ArcFace R50).

**Ignored by git/Docker:** `models/`, `faces/`, `results/` (heavy ONNX + datasets).
Scripts and this README are tracked.

## Our architecture (baseline)

| Piece | Value |
|---|---|
| Pack | `buffalo_l` |
| Detector | SCRFD-10G (`det_10g.onnx`) |
| Recognizer | ArcFace R50 (`w600k_r50.onnx`) |
| `det_size` | 640×640 |
| Match threshold | 0.6 |
| Provider | CPUExecutionProvider |

## Candidates

- YuNet + SFace (OpenCV Zoo)
- SCRFD-0.5GF + SFace
- SCRFD-0.5GF + MobileFaceNet
- SCRFD-2.5GF + MobileFaceNet (or SCRFD-10G + MBF proxy if `det_2.5g` missing)
- InsightFace `buffalo_s` pack reference

## Run (backend venv)

```bash
export OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4
export NUMEXPR_NUM_THREADS=4 VECLIB_MAXIMUM_THREADS=4 FACE_AB_THREADS=4

backend/.venv/bin/python face-ab-benchmark/scripts/freeze_conditions.py
backend/.venv/bin/python face-ab-benchmark/scripts/download_models.py
backend/.venv/bin/python face-ab-benchmark/scripts/prepare_dataset.py
backend/.venv/bin/python -u face-ab-benchmark/scripts/run_benchmark.py
```

Reports land in `results/` (`ab_report_corrected.md` is the accuracy table to trust).
