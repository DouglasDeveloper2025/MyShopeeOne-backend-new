from flask import Blueprint, request, jsonify, render_template
import pandas as pd
import io
from model.shopeeModel import db, HistoricoPreco, IntegracaoShopee, Produtos
from controller.shopee_update.shopee_update_controller import ShopeeService
from controller.auth.authShopee import TokenShopee

auth_bp = Blueprint("auth", __name__)

# Instâncias dos serviços
shopee_service = ShopeeService()
token_service = TokenShopee()


# --- ROTAS ORIGINAIS (Mantidas para compatibilidade interna se necessário) ---
@auth_bp.route("/auth/url", methods=["POST"])
def auth_url():
    """Gera a URL de autorização da Shopee."""
    dados = request.get_json()
    nome = dados.get("name")
    partner_id = dados.get("partner_id")
    partner_key = dados.get("partner_key")

    if not all([nome, partner_id, partner_key]):
        return (
            jsonify(
                {
                    "error": "Os campos 'name', 'partner_id' e 'partner_key' são obrigatórios"
                }
            ),
            400,
        )

    resultado = token_service.gerar_url_autenticacao(nome, partner_id, partner_key)
    status_code = 200 if resultado["status"] == "sucesso" else 500
    return jsonify(resultado), status_code


@auth_bp.route("/auth/callback", methods=["GET"])
def token_callback():
    """Rota de callback da Shopee (recebe code, shop_id e state)."""
    code = request.args.get("code")
    shop_id = request.args.get("shop_id")
    state = request.args.get("state")

    # Se o state não vier ou for inválido, buscamos a integração pendente mais recente
    from model.shopeeModel import IntegracaoShopee

    id_integracao = state
    if not id_integracao or id_integracao == "null":
        integracao = (
            IntegracaoShopee.query.filter_by(status="Pendente")
            .order_by(IntegracaoShopee.id.desc())
            .first()
        )
        id_integracao = integracao.id if integracao else None

    resultado = token_service.obter_tokens_via_callback(code, shop_id, id_integracao)

    if resultado["status"] == "sucesso":
        return """
        <html>
            <body style="text-align: center; font-family: sans-serif; padding-top: 50px;">
                <h1>Conectado com Sucesso!</h1>
                <p>Redirecionando...</p>
                <script>setTimeout(() => { window.location.href = '/'; }, 1500);</script>
            </body>
        </html>
        """

    return jsonify(resultado), 400


@auth_bp.route("/auth/auth-url", methods=["POST"])
def auth_url_api():
    """Alias para geração de URL de autenticação."""
    # O frontend envia partnerId e partnerKey no body
    dados = request.get_json()
    nome = dados.get("name") or "Loja React"
    partner_id = dados.get("partnerId")
    partner_key = dados.get("partnerKey")

    resultado = token_service.gerar_url_autenticacao(nome, partner_id, partner_key)
    return jsonify(resultado), 200 if resultado["status"] == "sucesso" else 400


@auth_bp.route("/shopee/token", methods=["POST"])
def exchange_token_api():
    """Troca o code pelo token e salva no banco sem expor dados sensíveis ao frontend."""
    dados = request.get_json()
    code = dados.get("code")
    shop_id = dados.get("shop_id")

    if not code or not shop_id:
        return jsonify({"status": "erro", "mensagem": "Code ou shop_id ausente"}), 400

    # Busca a integração pendente para vincular o token
    from model.shopeeModel import IntegracaoShopee

    integracao = IntegracaoShopee.query.filter_by(status="Pendente").order_by(IntegracaoShopee.id.desc()).first()
    
    if not integracao:
        # Se não houver pendente, tenta a primeira existente como fallback (pode ser atualização)
        integracao = IntegracaoShopee.query.first()

    if not integracao:
        return jsonify({"status": "erro", "mensagem": "Nenhuma configuração de integração encontrada. Configure Partner ID/Key primeiro."}), 400

    id_integracao = integracao.id
    resultado = token_service.obter_tokens_via_callback(code, shop_id, id_integracao)

    if resultado["status"] == "sucesso":
        return jsonify({"status": "sucesso", "mensagem": "Autenticação concluída e tokens salvos com segurança."}), 200

    return jsonify(resultado), 400
