import os
import sys
from dotenv import load_dotenv

# Carrega variáveis de ambiente
load_dotenv()
from rq import Worker, SimpleWorker
import platform

# Adiciona o diretório raiz do backend ao path para encontrar o app.py
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Define que estamos no worker para o app.py saber
os.environ["IS_RQ_WORKER"] = "true"

from app import app
from config.redis_config import redis_conn

listen = ['shopee_tasks']

def start_worker():
    # O worker precisa do contexto do Flask para interagir com o banco de dados
    with app.app_context():
        # No Render/Linux com Python 3.14, o Worker padrão (que usa fork/multiprocessing)
        # está causando BlockingIOError. Usaremos SimpleWorker para maior estabilidade.
        print("--- RQ Worker Iniciado (Modo Estabilidade/SimpleWorker) ---")
        worker = SimpleWorker(listen, connection=redis_conn)
            
        worker.work(with_scheduler=True)

if __name__ == '__main__':
    start_worker()
