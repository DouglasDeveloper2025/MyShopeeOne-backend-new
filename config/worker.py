"""
RQ Worker – standalone process (sem eventlet).

Este módulo cria sua própria instância do Flask *sem* importar app.py,
evitando que eventlet.monkey_patch() seja invocado dentro do worker.
Isso elimina os erros:
  • "1 RLock(s) were not greened"
  • BlockingIOError [Errno 11] do multiprocessing/forkserver
"""

import os
import sys
from dotenv import load_dotenv

# 1. Carrega variáveis de ambiente ANTES de qualquer import pesado
load_dotenv()

# 2. Marca que estamos no worker (segurança extra)
os.environ["IS_RQ_WORKER"] = "true"

# 3. Garante que o diretório raiz do backend esteja no path
backend_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_root not in sys.path:
    sys.path.insert(0, backend_root)

# 4. Imports (nenhum deles puxa eventlet)
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from rq import SimpleWorker
from config.redis_config import redis_conn
from model.shopeeModel import db          # Mesmo db do app principal

LISTEN_QUEUES = ['shopee_tasks']


def create_worker_app() -> Flask:
    """
    Cria um mini-app Flask apenas com a configuração de banco
    necessária para o worker executar jobs com acesso ao DB.
    """
    worker_app = Flask(__name__)

    # --- Banco de Dados ---
    database_url = os.environ.get("DATABASE_URL", "")
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)

    worker_app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    worker_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    worker_app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
        "pool_size": 5,
        "max_overflow": 10,
    }

    worker_app.config["JWT_SECRET_KEY"] = os.environ.get("JWT_SECRET_KEY")

    db.init_app(worker_app)
    return worker_app


def start_worker():
    worker_app = create_worker_app()

    with worker_app.app_context():
        print("--- RQ Worker Iniciado (SimpleWorker, sem eventlet) ---")
        worker = SimpleWorker(LISTEN_QUEUES, connection=redis_conn)
        worker.work(with_scheduler=True)


if __name__ == "__main__":
    start_worker()
