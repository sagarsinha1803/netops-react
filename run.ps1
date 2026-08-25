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

# build the frontend once if it has never been built
if (-not (Test-Path "$here\frontend\dist\index.html")) {
    Write-Host "frontend/dist missing - building it (one-off)..." -ForegroundColor Yellow
    Push-Location "$here\frontend"
    npm install
    npm run build
    Pop-Location
}

if ($Dev) {
    Start-Process powershell -ArgumentList "-NoExit", "-Command",
        "cd '$here\frontend'; npm run dev"
    Write-Host "Vite dev server starting on http://localhost:5173 (proxies to :$Port)"
}

Write-Host "Backend on http://localhost:$Port  (mocks: $env:USE_MOCKS)"
& "$here\.venv\Scripts\python.exe" -m uvicorn api.main:app --host 127.0.0.1 --port $Port
