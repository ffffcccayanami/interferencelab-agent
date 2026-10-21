# 启动后端 + 前端（开发用），Ctrl+C 结束
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root "backend"
$frontend = Join-Path $root "frontend"

$python = Join-Path $backend ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "[dev] 未找到 backend/.venv，先执行：" -ForegroundColor Yellow
    Write-Host "      cd backend; py -3.12 -m venv .venv; .venv\Scripts\python -m pip install -e `".[dev]`""
    exit 1
}

if (-not (Test-Path (Join-Path $backend ".env"))) {
    Write-Host "[dev] 未找到 backend/.env，复制 .env.example 后按需填写（可保持 LLM_ENABLED=false）" -ForegroundColor Yellow
    Copy-Item (Join-Path $root ".env.example") (Join-Path $backend ".env")
}

if (-not (Test-Path (Join-Path $frontend "node_modules"))) {
    Write-Host "[dev] 安装前端依赖…"
    Push-Location $frontend
    npm install
    Pop-Location
}

Write-Host "[dev] 后端 http://127.0.0.1:8010 ，前端 http://127.0.0.1:5175" -ForegroundColor Green

$backendJob = Start-Job -ScriptBlock {
    param($dir, $py)
    Set-Location $dir
    & $py -m app.main
} -ArgumentList $backend, $python

$frontendJob = Start-Job -ScriptBlock {
    param($dir)
    Set-Location $dir
    npm run dev
} -ArgumentList $frontend

try {
    while ($true) {
        Receive-Job -Job $backendJob, $frontendJob
        Start-Sleep -Seconds 2
    }
}
finally {
    Stop-Job -Job $backendJob, $frontendJob -ErrorAction SilentlyContinue
    Remove-Job -Job $backendJob, $frontendJob -Force -ErrorAction SilentlyContinue
}
