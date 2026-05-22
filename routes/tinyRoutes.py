from flask import Blueprint, request, jsonify  # pyrefly: ignore [missing-import]
from controller.tinyController import TinyController
import json
import logging

tiny_bp = Blueprint("tiny", __name__)
logger = logging.getLogger(__name__)


@tiny_bp.route("/tiny/webhook", methods=["POST", "GET"])
def tiny_webhook():
    print("Webhook Tiny recebido", request.data)
    # Checagem simples para requisições GET
    if request.method == "GET":
        return jsonify({"status": "sucesso", "mensagem": "Webhook Tiny ativo."}), 200

    import os
    import json
    
    # Captura os dados brutos e headers para depuração
    headers = dict(request.headers)
    form_data = dict(request.form)
    json_data = request.get_json(force=True, silent=True)
    raw_data = ""
    try:
        raw_data = request.data.decode("utf-8") if request.data else ""
    except Exception as e:
        raw_data = f"Error decoding request.data: {e}"

    # Salva em um arquivo de log para depuração
    log_info = {
        "content_type": request.content_type,
        "headers": headers,
        "form": form_data,
        "json": json_data,
        "raw_data": raw_data
    }
    
    log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "last_webhook_payload.json")
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(log_info, f, indent=4, ensure_ascii=False)
        logger.info(f"Webhook gravado com sucesso em {log_path}")
    except Exception as log_err:
        logger.error(f"Erro ao gravar log do webhook: {log_err}")

    # Processamento robusto dos dados recebidos
    dados = json_data

    # Fallback para request.form['dados'] se for enviado via form-urlencoded com parâmetro dados
    if dados is None:
        if form_data and "dados" in form_data:
            try:
                dados = json.loads(form_data.get("dados"))
            except Exception as e:
                logger.warning(f"Erro ao decodificar JSON de request.form['dados']: {e}")
        elif form_data:
            dados = form_data
        else:
            try:
                dados = json.loads(raw_data) if raw_data else None
            except:
                dados = raw_data

    # Delega a lógica de negócio para o TinyController
    controller = TinyController()
    resultado, status_code = controller.process_webhook(dados)

    # O ERP Tiny exige HTTP 200 em todas as respostas de webhook para evitar sinalização de falha de envio.
    # Portanto, mesmo se ocorrer um erro de validação ou processamento (que retornaria 400 ou 500),
    # respondemos com HTTP 200 para a API do Tiny.
    return jsonify(resultado), 200