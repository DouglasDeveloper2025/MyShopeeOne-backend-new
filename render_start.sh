#!/bin/bash

# 1. Inicia o Worker do RQ em segundo plano
echo "--- Iniciando RQ Worker em background ---"
python config/worker.py &

# 2. Inicia o servidor Web (Gunicorn)
# O Gunicorn fica em primeiro plano para o Render monitorar a saúde do serviço
echo "--- Iniciando Gunicorn Web Server ---"
gunicorn --worker-class eventlet -w 1 --bind 0.0.0.0:$PORT app:app
