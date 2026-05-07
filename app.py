import eventlet

# Força o patch de forma agressiva para tentar capturar locks residuais
eventlet.monkey_patch()

# Garante que o psycopg2 seja greened corretamente para evitar erros de SSL
try:
    from eventlet.support import psycopg2_patcher

    psycopg2_patcher.make_psycopg_green()
except ImportError:
    pass

import sys
import os
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from model.shopeeModel import db
from routes.shopeeRoutes import shopee_bp
from routes.authRoutes import auth_bp
from routes.userRoutes import user_bp
from flask_socketio import SocketIO
from dotenv import load_dotenv

# Carrega variáveis de ambiente
load_dotenv()

app = Flask(__name__, static_folder="frontend/dist", static_url_path="/")
CORS(app)

# Ajuste de timeouts para evitar Bad file descriptor no Render
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="eventlet",
    ping_timeout=60,
    ping_interval=25,
)

# Configuração do Banco de Dados postgresql
database_url = os.environ.get("DATABASE_URL")
if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = os.environ.get(
    "SQLALCHEMY_TRACK_MODIFICATIONS", False
)
app.config["JWT_SECRET_KEY"] = os.environ.get("JWT_SECRET_KEY")

# Configurações para evitar erros de conexão (SSL/Bad Record MAC) no Render/Eventlet
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
    "pool_size": 10,
    "max_overflow": 20,
}

# Inicializa o DB
db.init_app(app)

# Registra as rotas
app.register_blueprint(shopee_bp, url_prefix="/api")
app.register_blueprint(auth_bp, url_prefix="/api")
app.register_blueprint(user_bp, url_prefix="/api")

# Cria as tabelas se não existirem
with app.app_context():
    db.create_all()


# Rota para servir o Frontend (SPA Fallback)
@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve(path):
    if path != "" and os.path.exists(os.path.join(app.static_folder, path)):
        return send_from_directory(app.static_folder, path)
    return send_from_directory(app.static_folder, "index.html")


def background_checker():
    """Tarefa que roda em segundo plano para enfileirar jobs no RQ."""
    import time
    from datetime import datetime
    import pytz
    from config.redis_config import shopee_queue

    tz_br = pytz.timezone("America/Sao_Paulo")

    while True:
        try:
            # Pega o horário atual em São Paulo
            now = datetime.now(tz_br)

            from model.shopeeModel import IntegracaoShopee, Configuracoes
            from datetime import datetime as dt_utc, timedelta

            with app.app_context():
                config = Configuracoes.query.first()
                integracao = IntegracaoShopee.query.first()

                # 1. Checagem de Token Shopee
                if integracao and integracao.last_access_update_at:
                    agora_utc = dt_utc.utcnow()
                    # Busca a configuração de intervalo do banco (ou usa 230 min como fallback)
                    intervalo_min = config.intervalo_refresh_token if config else 230

                    # Se o token tem mais que o intervalo definido, enfileira a renovação
                    if (agora_utc - integracao.last_access_update_at) >= timedelta(
                        minutes=intervalo_min
                    ):
                        print(
                            f"[{now.strftime('%H:%M:%S')}] Token Shopee próximo do limite ({intervalo_min} min). Enfileirando renovação no RQ..."
                        )
                        shopee_queue.enqueue(
                            "controller.auth.authShopee.run_token_refresh_job",
                            job_id="shopee_token_refresh_auto",
                        )

                # 2. Sincronização COMPLETA Agendada
                target_h = config.hora_sincronizacao if config else 0
                target_m = config.minuto_sincronizacao if config else 15

                if now.hour == target_h and now.minute == target_m:
                    print(
                        f"[{now.strftime('%d/%m/%Y %H:%M:%S')}] Enfileirando SINCRONIZAÇÃO COMPLETA agendada ({target_h:02d}:{target_m:02d}) no RQ..."
                    )
                    shopee_queue.enqueue(
                        "controller.shopee_update.shopee_update_controller.run_full_sync_job"
                    )
                    # Dorme por 70 segundos para garantir que saia da janela do minuto atual
                    time.sleep(70)

                # 3. Impulsionamento (Boost) Automático (A cada 1 minuto para manter slots cheios)
                if now.second < 60:
                    # O scheduler já dorme 60s no final, então rodará uma vez por minuto
                    # print(f"[{now.strftime('%H:%M:%S')}] Verificando Slots de BOOST (1 min interval)...")
                    shopee_queue.enqueue(
                        "controller.shopee_boost.run_boost_job",
                        job_id=f"shopee_boost_cycle_{now.strftime('%Y%m%d%H%M')}",
                    )

                # Limpa a sessão antes de sair do contexto para evitar conexões presas
                db.session.remove()

            # Verifica a cada 1 minuto
            time.sleep(60)
        except Exception as e:
            print(f"?? Erro no agendamento da Fila: {e}")
            time.sleep(60)


# Inicia a checagem em segundo plano de forma assíncrona compatível com eventlet
# Somente se NÃO for o worker do RQ (para não duplicar a tarefa de agendamento)
if "worker.py" not in sys.argv[0] and os.environ.get("IS_RQ_WORKER") != "true":
    eventlet.spawn(background_checker)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5005))
    socketio.run(app, host="0.0.0.0", port=port, debug=True)
