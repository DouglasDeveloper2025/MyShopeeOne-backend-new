import redis

r = redis.Redis(host='127.0.0.1', port=6379, db=0)
r.set('shopee_sync_cancel', 'true')
print("Flag de cancelamento setada com sucesso no Redis 6379 (shopee_sync_cancel)!")
