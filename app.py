import os
from flask import Flask, request, jsonify
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
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# Configuração do Banco de Dados postgresql
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = os.environ.get(
    "SQLALCHEMY_TRACK_MODIFICATIONS", False
)
app.config["JWT_SECRET_KEY"] = os.environ.get("JWT_SECRET_KEY")

# Inicializa o DB
db.init_app(app)

# Registra as rotas
app.register_blueprint(shopee_bp, url_prefix="/api")
app.register_blueprint(auth_bp, url_prefix="/api")
app.register_blueprint(user_bp, url_prefix="/api")

# Cria as tabelas se não existirem
with app.app_context():
    db.create_all()


def background_checker():
    """Tarefa que roda em segundo plano para enfileirar jobs no RQ."""
    import time
    from datetime import datetime
    import pytz
    from config.redis_config import shopee_queue

    tz_br = pytz.timezone("America/Sao_Paulo")

    print("--- Gatilho de Agendamento RQ Iniciado (Fuso: SP) ---")
    while True:
        try:
            # Pega o horário atual em São Paulo
            now = datetime.now(tz_br)
            # Todo dia às 00:05 enfileira o job de desbloqueio
            if now.hour == 0 and now.minute == 5:
                print(
                    f"[{now.strftime('%d/%m/%Y %H:%M:%S')}] Enfileirando checagem diária de desbloqueios no RQ..."
                )
                # Colocamos o job na fila shopee_tasks
                shopee_queue.enqueue(
                    "controller.shopeeUpdate.shopeeUpdateController.run_unlock_check_job"
                )

                # Dorme por 15 minutos para garantir que não enfileire novamente na mesma janela
                time.sleep(900)

            # Verifica a cada 1 minuto se já é meia-noite
            time.sleep(60)
        except Exception as e:
            print(f"?? Erro no gatilho de agendamento: {e}")
            time.sleep(60)


if __name__ == "__main__":
    import threading

    # Inicia a thread em modo daemon para que ela morra com o processo principal
    checker_thread = threading.Thread(target=background_checker, daemon=True)
    checker_thread.start()

    socketio.run(app, host="0.0.0.0", port=5005, debug=True)
