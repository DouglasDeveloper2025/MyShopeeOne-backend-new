@echo off
title Shopee Price Manager - Starter
echo ====================================================
echo   INICIANDO SERVICOS DO GERENCIADOR DE PRECOS
echo ====================================================

:: Verifica se a pasta .venv existe
if not exist ".venv" (
    echo [ERRO] Ambiente virtual .venv nao encontrado!
    pause
    exit
)

echo [1/3] Iniciando Servidor Flask (API e Sockets)...
start "FLASK SERVER" cmd /k "call .venv\Scripts\activate && python app.py"

echo [2/3] Iniciando RQ Worker (Processamento de Filas)...
start "RQ WORKER" cmd /k "call .venv\Scripts\activate && python config/worker.py"

echo [3/3] Iniciando RQ Dashboard (Monitoramento de Filas)...
start "RQ DASHBOARD" cmd /k "call .venv\Scripts\activate && rq-dashboard"

echo.
echo ====================================================
echo   TODOS OS SERVICOS FORAM LANÇADOS COM SUCESSO!
echo   Pode fechar esta janela se desejar.
echo ====================================================
pause
