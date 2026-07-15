# DFI local quality-eval runner
# Runs the golden-query search-quality evaluator against the live backend using
# the backend venv (falls back to system python). Logs each run.
# Note: python warnings on stderr are expected and must NOT abort the run.

$repo   = 'C:\Users\MasterUnion\DriveFaceIndexer'
$py     = Join-Path $repo 'backend\.venv\Scripts\python.exe'
$script = Join-Path $repo 'backend\scripts\eval_search_quality.py'
$logDir = Join-Path $repo 'logs\quality'
$log    = Join-Path $logDir 'runner.log'

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
function Log($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m" | Tee-Object -FilePath $log -Append }

if (-not (Test-Path $py)) { $py = 'python' }

# Do not let native-command stderr be treated as a terminating error.
$ErrorActionPreference = 'Continue'

Log "Starting quality eval..."
& $py $script 2>&1 | Tee-Object -FilePath $log -Append
$code = $LASTEXITCODE
Log "Quality eval finished with exit code $code."
exit $code
