# Clone Fennec Search (sibling repo) and start the Docker sidecar stack.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $Root
$FennecDir = Join-Path (Split-Path -Parent $ProjectRoot) "fennec-search"
$MediaDir = Join-Path $ProjectRoot "data\fennec-media"

Write-Host "=== Fennec Search sidecar setup ===" -ForegroundColor Cyan

if (-not (Test-Path $FennecDir)) {
  Write-Host "Cloning fennec-search to $FennecDir" -ForegroundColor Yellow
  git clone https://github.com/JasonMakes801/fennec-search.git $FennecDir
} else {
  Write-Host "Using existing clone: $FennecDir" -ForegroundColor Green
}

if (-not (Test-Path $MediaDir)) {
  New-Item -ItemType Directory -Path $MediaDir -Force | Out-Null
  Write-Host "Created shared media folder: $MediaDir" -ForegroundColor Green
}

Push-Location $ProjectRoot
Write-Host "Starting Fennec stack (API :8002, UI :8080)..." -ForegroundColor Cyan
docker compose -f docker-compose.fennec.yml up -d --build
Pop-Location

Write-Host ""
Write-Host "Fennec API:  http://localhost:8002" -ForegroundColor Green
Write-Host "Fennec UI:   http://localhost:8080" -ForegroundColor Green
Write-Host "Shared dir:  $MediaDir" -ForegroundColor Green
Write-Host ""
Write-Host "Enable in backend/.env:" -ForegroundColor Yellow
Write-Host "  FENNEC_ENABLED=true"
Write-Host "  FENNEC_BASE_URL=http://127.0.0.1:8002"
Write-Host "  FENNEC_VIDEO_CACHE_DIR=./data/fennec-media"
