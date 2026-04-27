Write-Host "====================================================" -ForegroundColor Cyan
Write-Host "  INICIANDO SERVICOS DO GERENCIADOR DE PRECOS (PS)" -ForegroundColor Cyan
Write-Host "====================================================" -ForegroundColor Cyan

if (!(Test-Path ".venv")) {
    Write-Host "[ERRO] Ambiente virtual (.venv) nao encontrado!" -ForegroundColor Red
    return
}

Write-Host "[1/3] Iniciando Servidor Flask..." -ForegroundColor Green
Start-Process powershell -ArgumentList "-NoExit", "-Command", "& '.\.venv\Scripts\Activate.ps1'; python app.py"

Write-Host "[2/3] Iniciando RQ Worker..." -ForegroundColor Green
Start-Process powershell -ArgumentList "-NoExit", "-Command", "& '.\.venv\Scripts\Activate.ps1'; python config/worker.py"

Write-Host "[3/3] Iniciando RQ Dashboard..." -ForegroundColor Green
Start-Process powershell -ArgumentList "-NoExit", "-Command", "& '.\.venv\Scripts\Activate.ps1'; rq-dashboard"

Write-Host "`nTodos os servicos foram lancados!" -ForegroundColor Yellow
pause
