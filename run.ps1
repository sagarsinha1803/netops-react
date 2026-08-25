# Start the NetOps agent (FastAPI backend + built React frontend).
#
#   .\run.ps1              real MCPs
#   .\run.ps1 -Mock        mock CMDB / devices / Tufin / local probes
#   .\run.ps1 -Port 8001   if 8000 is taken
#   .\run.ps1 -Dev         also start the Vite dev server (hot reload, :5173)
param(
    [switch]$Mock,
    [switch]$Dev,
    [int]$Port = 8000
)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

if ($Mock) { $env:USE_MOCKS = "1" } else { $env:USE_MOCKS = "0" }

# A mock server that cannot start takes its whole stage with it, and the app
# can only report that AFTER you press Run. Check here, where the fix is
# obvious: a missing file means the checkout is behind, not that anything is
# broken.
if ($Mock) {
    $needed = @("scenarios.py", "unicorn_mock.py", "tufin_mock.py",
                "alert_mock.py", "device_mock.py")
    $missing = $needed | Where-Object {
        -not (Test-Path (Join-Path $here "tests\mocks\$_")) }
    if ($missing) {
        Write-Host "tests\mocks is missing: $($missing -join ', ')" -ForegroundColor Red
        Write-Host "This checkout is behind. Run: git pull" -ForegroundColor Yellow
        exit 1
    }
}

# Build the frontend when it has never been built, OR when the sources have
# moved on since it was. frontend/dist is a build output and is not in git, so
# a `git pull` brings new UI SOURCE and leaves the old bundle sitting there.
# The app then serves a UI older than the backend it talks to: new stages and
# tabs arrive over the socket and nothing renders them, which reads as "the
# feature does not work" rather than "this page is stale".
$dist = Join-Path $here "frontend\dist\index.html"
$sources = @((Join-Path $here "frontend\src"),
             (Join-Path $here "frontend\package.json"),
             (Join-Path $here "frontend\index.html")) |
           Where-Object { Test-Path $_ }
$newest = Get-ChildItem $sources -Recurse -File -ErrorAction SilentlyContinue |
          Sort-Object LastWriteTime -Descending | Select-Object -First 1
$stale = (Test-Path $dist) -and $newest -and
         ($newest.LastWriteTime -gt (Get-Item $dist).LastWriteTime)

if ((-not (Test-Path $dist)) -or $stale) {
    if ($stale) {
        Write-Host "frontend/dist is older than frontend/src - rebuilding..." -ForegroundColor Yellow
    } else {
        Write-Host "frontend/dist missing - building it (one-off)..." -ForegroundColor Yellow
    }
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        Write-Host "npm is not on PATH, so the UI cannot be rebuilt." -ForegroundColor Red
        Write-Host "The page will be older than the backend: new tabs and stages" -ForegroundColor Red
        Write-Host "simply will not appear. Install Node, or copy a built" -ForegroundColor Red
        Write-Host "frontend\dist from a machine that has one." -ForegroundColor Red
        if (-not (Test-Path $dist)) { exit 1 }
    } else {
        Push-Location "$here\frontend"
        if (-not (Test-Path "node_modules")) { npm install }
        npm run build
        Pop-Location
    }
}

if ($Dev) {
    Start-Process powershell -ArgumentList "-NoExit", "-Command",
        "cd '$here\frontend'; npm run dev"
    Write-Host "Vite dev server starting on http://localhost:5173 (proxies to :$Port)"
}

Write-Host "Backend on http://localhost:$Port  (mocks: $env:USE_MOCKS)"
& "$here\.venv\Scripts\python.exe" -m uvicorn api.main:app --host 127.0.0.1 --port $Port
