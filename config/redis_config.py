import os
from redis import Redis
from rq import Queue

# Configuração de conexão com o Redis
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

# Conexão global
redis_conn = Redis.from_url(REDIS_URL)

# Fila principal para tarefas da Shopee
shopee_queue = Queue('shopee_tasks', connection=redis_conn)
