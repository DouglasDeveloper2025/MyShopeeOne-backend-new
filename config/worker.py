import os
import sys
from redis import Redis
from rq import SimpleWorker # Importamos SimpleWorker para Windows

# Adiciona o diretório raiz do backend ao path para encontrar o app.py
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app
from config.redis_config import redis_conn

listen = ['shopee_tasks']

def start_worker():
    # O worker precisa do contexto do Flask para interagir com o banco de dados
    with app.app_context():
        print("--- RQ SimpleWorker Iniciado (Modo Windows) ---")
        # No Windows, usamos SimpleWorker para evitar o erro de 'os.fork'
        worker = SimpleWorker(listen, connection=redis_conn)
        worker.work()

if __name__ == '__main__':
    start_worker()
