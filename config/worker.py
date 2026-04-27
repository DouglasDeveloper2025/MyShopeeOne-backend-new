import eventlet
eventlet.monkey_patch()

import os
import sys
from dotenv import load_dotenv

# Carrega variáveis de ambiente
load_dotenv()
from rq import Worker, SimpleWorker
import platform

# Adiciona o diretório raiz do backend ao path para encontrar o app.py
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app
from config.redis_config import redis_conn

listen = ['shopee_tasks']

def start_worker():
    # O worker precisa do contexto do Flask para interagir com o banco de dados
    with app.app_context():
        # Detectar SO
        is_windows = platform.system() == "Windows"
        
        if is_windows:
            print("--- RQ SimpleWorker Iniciado (Modo Windows) ---")
            worker = SimpleWorker(listen, connection=redis_conn)
        else:
            print("--- RQ Worker Iniciado (Modo Linux/Produção) ---")
            worker = Worker(listen, connection=redis_conn)
            
        worker.work()

if __name__ == '__main__':
    start_worker()
