$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
    Write-Host "Criando ambiente virtual..."
    py -3 -m venv .venv
}

Write-Host "Ativando venv e instalando dependencias..."
& .\.venv\Scripts\Activate.ps1
pip install --upgrade pip -q
pip install -r requirements.txt -q

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Arquivo .env criado a partir de .env.example"
}

$ffmpegCmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $ffmpegCmd) {
    Write-Host "AVISO: ffmpeg nao encontrado no PATH."
    Write-Host "Instale com: winget install --id Gyan.FFmpeg -e"
    Write-Host "Depois feche e reabra o terminal."
}

Write-Host ""
Write-Host "Iniciando API em http://127.0.0.1:8000"
Write-Host "Playground de teste: http://127.0.0.1:8000/dev"
Write-Host "API Key padrao: dev-local-key"
Write-Host ""
Write-Host "Requisito: ffmpeg instalado e no PATH"
Write-Host "Pressione Ctrl+C para parar"
Write-Host ""

python -m app.main
