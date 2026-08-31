# AGENTS.md — DriveFaceIndexer

## Railway deploy (production)

Project: `drivefaceindexer` · services: `dfi-backend`, `dfi-frontend`, `dfi-face-worker`, `dfi-carousel`.

### Pre-deploy git sync (required for every service)

Before **any** `railway up` (backend, frontend, face-worker, or carousel), agents **must**:

1. Be on branch **`main`** (never deploy from a feature branch).
2. **`git pull`** (or `git pull --ff-only origin main`) so local `main` has **no unpulled commits** left versus `origin/main`.
3. **Commit finished work** so deployable fixes are not stuck only in the working tree (and **push to `origin/main`** when the user wants production/GitHub in sync — unpushed commits are wiped if Railway redeploys from remote).
4. Confirm sync before uploading:

```bash
git checkout main
git pull --ff-only origin main
git status -sb   # expect: ## main...origin/main  (not "behind" / "ahead" with undeployed intent left uncommitted)
```

Do **not** deploy if:

- the current branch is not `main`
- `main` is behind `origin/main` (pull first)
- `git pull` fails or would require merge/rebase conflict resolution you have not finished
- search/object/deploy-critical changes are still uncommitted (commit first; do not rely on a one-off local `railway up` archive alone)

Uncommitted deploy-critical work has already caused production to miss files (e.g. `query_concepts.py`) when an env-var or GitHub redeploy rebuilt from older `origin/main`. **Keep committing** as you go so pull + deploy stay safe.

### Pre-deploy guard (required for backend / face-worker)

Before `railway up` for **`dfi-backend`** or **`dfi-face-worker`**, agents **must** run the import / unbound-name guards and only deploy if they pass:

```bash
cd backend && python -m pytest tests/test_import_guards.py -q
```

These catch runtime `NameError`s (missing imports used only inside functions) that plain module imports miss. Do **not** skip this for “small” fixes. If the tests fail, fix them first — do not deploy.

Frontend / carousel deploys do not require this pytest file, but still follow the git sync above and the upload commands below.

### Correct commands

Always `cd` into the service directory first (matches `scripts/auto-deploy.sh`). Do **not** use `--path-as-root`.

```bash
# Backend
cd backend && railway up --service dfi-backend --detach -y

# Frontend (Next.js app under frontend/)
cd frontend && railway up --service dfi-frontend --detach -y

# Face worker (same image/tree as backend unless configured otherwise)
cd backend && railway up --service dfi-face-worker --detach -y

# Carousel: upload from **repo root** (service Root Directory = carousel-frontend)
cd /path/to/repo && railway up --service dfi-carousel --detach -y
```

Or from repo root:

```bash
(cd backend && railway up --service dfi-backend --detach -y)
(cd frontend && railway up --service dfi-frontend --detach -y)
(cd backend && railway up --service dfi-face-worker --detach -y)
railway up --service dfi-carousel --detach -y   # from repo root only
```

### Wrong (breaks the build)

```bash
# BAD — from repo root with --path-as-root frontend
railway up --path-as-root frontend --service dfi-frontend --detach -y
```

That uploads `frontend/` **contents** as the archive root, while the Railway service still expects a nested `frontend/` directory → build fails with:

`lstat .../snapshot-target-unpack/frontend: no such file or directory`

Same class of mistake: `--path-as-root backend` for `dfi-backend` when the service root directory is already `backend`.

### Check status

```bash
railway deployment list --service dfi-frontend
railway service status --service dfi-frontend
```

### Optional: scripted deploy

```bash
scripts/auto-deploy.sh --force   # backend + frontend + carousel for current HEAD
```
