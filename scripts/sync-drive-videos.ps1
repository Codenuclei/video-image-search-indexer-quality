# Sync Drive videos into Fennec's watch folder, then start Fennec Search.
$ErrorActionPreference = "Stop"

$CacheDir = "c:\Users\MasterUnion\fennec-search\media-cache"
$FennecRoot = "c:\Users\MasterUnion\fennec-search"

New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null

Write-Host "=== Index images + PDF text via DriveFaceIndexer ===" -ForegroundColor Cyan
Write-Host "POST http://127.0.0.1:8000/index" -ForegroundColor Gray
try {
    Invoke-RestMethod -Uri "http://127.0.0.1:8000/index" -Method POST -TimeoutSec 10 | Out-Null
} catch {
    Write-Host "Backend not running or index already active — start backend first if needed." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== Set Fennec watch folder to cache ===" -ForegroundColor Cyan
Write-Host "Cache: $CacheDir"
Write-Host "Add to backend\.env: FENNEC_VIDEO_CACHE_DIR=$CacheDir"
Write-Host ""
Write-Host "=== Start Fennec (video + action search) ===" -ForegroundColor Cyan
Write-Host "cd $FennecRoot"
Write-Host "docker compose up -d"
Write-Host "Open http://localhost:8080 for visual + face + dialog search"
