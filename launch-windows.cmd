@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Setup required. See the Windows instructions in README.md.
    exit /b 1
)
echo Checking for an existing workbench backend on port 8000.
powershell.exe -NoProfile -NonInteractive -Command ^
    "$ErrorActionPreference = 'Stop';" ^
    "function Get-BackendListener {" ^
    "    $connections = netstat.exe -ano -p tcp;" ^
    "    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect port 8000.' };" ^
    "    foreach ($line in $connections) {" ^
    "        if ($line -match '^\s*TCP\s+(?:127\.0\.0\.1|0\.0\.0\.0):8000\s+0\.0\.0\.0:0\s+\S+\s+(\d+)\s*$') { [int]$Matches[1] }" ^
    "    }" ^
    "};" ^
    "$listeners = @(Get-BackendListener | Sort-Object -Unique);" ^
    "if ($listeners.Count -eq 0) { exit 0 };" ^
    "if ($listeners.Count -ne 1) { throw 'Multiple processes are listening on port 8000.' };" ^
    "$listener = $listeners[0]; $process = Get-Process -Id $listener -ErrorAction SilentlyContinue;" ^
    "if (-not $process) { exit 0 };" ^
    "if ($process.ProcessName -notmatch '^python(?:w|\d+(?:\.\d+)?)?$') { throw ('Port 8000 is in use by another program, PID ' + $listener + '.') };" ^
    "$started = $process.StartTime;" ^
    "$details = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $listener) -ErrorAction SilentlyContinue;" ^
    "$isWorkbench = $details.CommandLine -match '\s-m\s+(?:danmaku(?=\s|$)|uvicorn\s+danmaku\.main:create_app(?=\s|$))';" ^
    "if (-not $isWorkbench) {" ^
    "    try {" ^
    "        $paths = (Invoke-RestMethod -Uri 'http://127.0.0.1:8000/openapi.json' -TimeoutSec 3).paths;" ^
    "        $isWorkbench = $paths.'/api/v1/health'.get -and $paths.'/api/v1/projects'.get -and $paths.'/api/v1/send-queue/interrupted'.get;" ^
    "    } catch { $isWorkbench = $false }" ^
    "};" ^
    "if (-not $isWorkbench) { throw ('Port 8000 is in use by an unrecognized Python process, PID ' + $listener + '.') };" ^
    "$process = Get-Process -Id $listener -ErrorAction SilentlyContinue;" ^
    "if ($process -and $process.StartTime -eq $started -and (@(Get-BackendListener) -contains $listener)) {" ^
    "    Write-Host ('Stopping the existing workbench backend, PID ' + $listener + '.');" ^
    "    Stop-Process -InputObject $process" ^
    "};" ^
    "$deadline = (Get-Date).AddSeconds(8);" ^
    "while (@(Get-BackendListener).Count -gt 0) {" ^
    "    if ((Get-Date) -ge $deadline) { throw 'Port 8000 did not become available.' };" ^
    "    Start-Sleep -Milliseconds 200" ^
    "}"
if errorlevel 1 exit /b 1
echo Starting workbench from "%CD%"
if "%~1"=="" if exist "frontend\dist\index.html" (
    echo Opening http://127.0.0.1:8000 when the workbench is ready.
    start "" /b powershell.exe -NoProfile -NonInteractive -Command ^
        "$url = 'http://127.0.0.1:8000'; $index = Join-Path (Get-Location) 'frontend/dist/index.html';" ^
        "$expected = [IO.File]::ReadAllText($index); $build = (Get-FileHash -LiteralPath $index).Hash.Substring(0, 12);" ^
        "$page = $url + '/?build=' + $build; $deadline = (Get-Date).AddSeconds(30);" ^
        "do {" ^
        "    try { $ready = (Invoke-WebRequest -Uri ($url + '/api/v1/health') -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop).StatusCode -eq 200 -and (Invoke-WebRequest -Uri $page -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop).Content -ceq $expected }" ^
        "    catch { $ready = $false };" ^
        "    if ($ready) { Start-Process $page; exit };" ^
        "    Start-Sleep -Milliseconds 500" ^
        "} while ((Get-Date) -lt $deadline)"
)
".venv\Scripts\python.exe" -m danmaku %*
