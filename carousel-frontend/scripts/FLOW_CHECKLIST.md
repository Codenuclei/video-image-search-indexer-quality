# Carousel studio flow — verified checklist (CDP/Playwright)

Date: 2026-08-03  
Surface: `carousel-frontend` @ http://localhost:3002/carousel  
Proof: `scripts/e2e-carousel-flow.mjs` → `scripts/.e2e-out/report.json` + screenshots

## Hard gates (user click only)

- [x] Select video → **no themes** until Continue / Load themes
- [x] Continue → themes appear (cached or generated)
- [x] Select theme(s) → Extract → **hooks + topics/subtopics** (phase 3 tree from current extract)
- [x] Select hook/topic → Continue to preview → **direction / intent** (phase 4)
- [x] Generate carousels → **text slides only** (phase 5; `select_images: false`)
- [x] Select & filter images → **frames appear** (last process only on click)

## No fallthrough

- [x] Phase 5 UI gated on `phase >= 5` only (no `outline || generatedCarousels` leak)
- [x] No auto-load themes on video select
- [x] No cached carousel jump-to-end on video select
- [x] Long extract (~2+ min) survives via App Router proxy (`/backend` + `/api/proxy`), not 30s rewrite 500

## How to re-verify

```bash
cd carousel-frontend
npm run dev   # :3002; backend :8000
node scripts/verify-extract-tree.mjs
node scripts/e2e-carousel-flow.mjs
```
