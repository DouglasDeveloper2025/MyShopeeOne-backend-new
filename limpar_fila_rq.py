# backend/limpar_fila_rq.py
from config.redis_config import shopee_queue, redis_conn

print("--- [INICIANDO LIMPEZA DO REDIS] ---")

# 1. Limpa jobs aguardando na fila
print("Limpando fila 'shopee_tasks'...")
count = shopee_queue.empty()
print(f"Fila limpa ({count} jobs removidos).")

# 2. Envia sinal de cancelamento para processos rodando
print("Enviando sinal de cancelamento para processos ativos...")
redis_conn.set("shopee_sync_cancel", "true", ex=10)

# 3. Reseta o status de progresso no dashboard
print("Resetando status de progresso...")
redis_conn.delete("shopee_sync_status")

print("\n[SUCESSO] O Redis foi resetado. Se houver um processo rodando, ele deve parar em alguns segundos.")
print("Agora você pode reiniciar o start_all.bat com segurança.")
