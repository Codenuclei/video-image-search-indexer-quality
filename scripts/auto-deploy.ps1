# DFI auto-deploy watcher
# Runs on a schedule (every ~22 min). Checks GitHub main for new commits; if the
# local deploy mirror is behind AND clean, fast-forwards and runs `railway up` for
# whichever service(s) actually changed (backend/ and/or frontend/).
# Safe by design: never clobbers uncommitted local edits (skips if working tree dirty).

$ErrorActionPreference = 'Stop'
$repo   = 'C:\Users\MasterUnion\DriveFaceIndexer'
$branch = 'main'
$logDir = Join-Path $repo 'logs'
$log    = Join-Path $logDir 'auto-deploy.log'

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
function Log($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m" | Tee-Object -FilePath $log -Append }

try {
    Set-Location $repo

    # Prefer a cheap remote check via gh; fall back to git fetch.
    git fetch origin $branch --quiet

    $local  = (git rev-parse HEAD).Trim()
    $remote = (git rev-parse "origin/$branch").Trim()

    if ($local -eq $remote) {
        Log "No changes (HEAD=$($local.Substring(0,7))). Skip."
        exit 0
    }

    $dirty = git status --porcelain
    if ($dirty) {
        Log "Working tree dirty - skipping auto-deploy to avoid clobbering local edits."
        exit 0
    }

    # Which services are affected?
    $changed = git diff --name-only $local $remote
    $deployBackend  = [bool]($changed | Where-Object { $_ -like 'backend/*' })
    $deployFrontend = [bool]($changed | Where-Object { $_ -like 'frontend/*' })

    git merge --ff-only "origin/$branch" | Out-Null
    Log "Updated $($local.Substring(0,7)) -> $($remote.Substring(0,7)) | backend=$deployBackend frontend=$deployFrontend"

    if ($deployBackend) {
        Set-Location (Join-Path $repo 'backend')
        Log "Deploying backend (railway up dfi-backend)..."
        railway up --service dfi-backend --detach 2>&1 | Tee-Object -FilePath $log -Append
        Set-Location $repo
    }
    if ($deployFrontend) {
        Set-Location (Join-Path $repo 'frontend')
        Log "Deploying frontend (railway up dfi-frontend)..."
        railway up --service dfi-frontend --detach 2>&1 | Tee-Object -FilePath $log -Append
        Set-Location $repo
    }
    if (-not $deployBackend -and -not $deployFrontend) {
        Log "Changes were outside backend/ and frontend/ - nothing to deploy."
    }
    Log "Done."
}
catch {
    Log "ERROR: $($_.Exception.Message)"
    exit 1
}
