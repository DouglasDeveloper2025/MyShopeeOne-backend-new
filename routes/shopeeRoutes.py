from flask import Blueprint, request, jsonify, redirect, g
from sqlalchemy import or_
from datetime import datetime, timedelta
import pandas as pd
import io
import re
import requests
from sqlalchemy.orm import joinedload, selectinload
from controller.shopee_update.shopee_update_controller import ShopeeService
from controller.shopee_boost import BoostController
from controller.auth.authShopee import TokenShopee
from model.shopeeModel import (
    db,
    IntegracaoShopee,
    Anuncios,
    Produtos,
    BoostLog,
    Configuracoes,
)
from middleware.authMiddleware import (
    token_required,
    admin_required,
    permission_required,
)

# Criação do Blueprint para as rotas da Shopee
shopee_bp = Blueprint("shopee", __name__)

# Instâncias dos serviços
shopee_service = ShopeeService()
auth_shopee = TokenShopee()


@shopee_bp.route("/test", methods=["GET"])
def test_api():
    """Rota simples para testar se a API está online."""
    return (
        jsonify({"status": "sucesso", "mensagem": "API está rodando perfeitamente!"}),
        200,
    )


@shopee_bp.route("/shopee/calculate-combo", methods=["GET"])
def calculate_combo():
    """Calcula o preço do combo com base no preço base e quantidade."""
    try:
        base_price = request.args.get("base_price", type=float)
        qty = request.args.get("qty", type=int)

        if base_price is None or qty is None:
            return (
                jsonify(
                    {
                        "status": "erro",
                        "mensagem": "Parâmetros base_price e qty são obrigatórios",
                    }
                ),
                400,
            )

        if qty < 1:
            return (
                jsonify(
                    {"status": "erro", "mensagem": "Quantidade deve ser pelo menos 1"}
                ),
                400,
            )

        if qty == 1:
            return jsonify({"suggested_price": base_price})

        # Regra Progressiva: (Preço * Quantidade) * (0.99 ^ (n-1))
        calc = (base_price * qty) * (0.99 ** (qty - 1))
        suggested_price = round(calc, 2)

        return jsonify(
            {"suggested_price": suggested_price, "rule": "1% OFF sobre o total"}
        )
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@shopee_bp.route("/shopee/auth-url", methods=["GET"])
def get_auth_url():
    # print("ENTRY: get_auth_url")
    """
    Gera a URL de autorização da Shopee para o vendedor.
    """

    dados = request.get_json()
    nome_integracao = dados.get("")
    partner_id = dados.get("")
    partner_key = dados.get("")
    url = auth_shopee.generate_auth_url(nome_integracao, partner_id, partner_key)
    return jsonify({"url": url})


@shopee_bp.route("/shopee/callback", methods=["GET"])
def shopee_callback():
    """
    Recebe o code e shop_id da Shopee após a autorização do vendedor.
    """
    code = request.args.get("code")
    shop_id = request.args.get("shop_id")

    if not code or not shop_id:
        return jsonify({"status": "erro", "mensagem": "Code ou shop_id ausente"}), 400

    sucesso, erro = auth_shopee.get_tokens_via_callback(code, shop_id)

    if sucesso:
        # Redirecionar para o frontend após o sucesso
        return redirect("http://localhost:5173/configuracao?status=success")
    else:
        return jsonify({"status": "erro", "mensagem": erro}), 500


@shopee_bp.route("/shopee/integration-status", methods=["GET"])
@token_required
def get_integration_status():
    """
    Verifica se a integração com a Shopee está configurada e retorna o status atual.
    """
    try:
        # Busca a integração principal
        integracao = IntegracaoShopee.query.first()

        if not integracao:
            return jsonify({"status": "Pendente", "shop_id": ""}), 200

        # Calcula tempo de expiração
        expires_at = None
        if integracao.last_access_update_at and integracao.expire_in:
            # Forçar o sufixo Z para que o frontend saiba que a data está em UTC e não local
            expires_at = (
                integracao.last_access_update_at
                + timedelta(seconds=integracao.expire_in)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")

        response_data = {
            "status": integracao.status or "Pendente",
            "shop_id": integracao.shop_id or "",
            "name": integracao.name or "",
            "partner_id": integracao.partner_id or "",
            "partner_key": integracao.partner_key or "",
            "expires_at": expires_at,
        }

        # APENAS Admins podem ver o Token de Acesso real por segurança
        if (
            hasattr(g, "current_user")
            and g.current_user
            and g.current_user.role == "admin"
        ):
            response_data["access_token"] = integracao.last_access_token

        return jsonify(response_data), 200

    except Exception as e:
        return jsonify({"status": "erro", "mensagem": f"Erro interno: {str(e)}"}), 500


@shopee_bp.route("/shopee/refresh-token", methods=["POST"])
@token_required
def manual_refresh_token():
    """Força a renovação do token de acesso da Shopee."""
    try:
        integracao = IntegracaoShopee.query.first()
        if not integracao:
            return (
                jsonify(
                    {"status": "erro", "mensagem": "Nenhuma integração encontrada"}
                ),
                404,
            )

        resultado, erro = auth_shopee._refresh_token(integracao)
        if erro:
            return jsonify({"status": "erro", "mensagem": erro}), 400

        return (
            jsonify(
                {
                    "status": "sucesso",
                    "mensagem": "Token renovado com sucesso!",
                    "expires_at": (
                        integracao.last_access_update_at
                        + timedelta(seconds=integracao.expire_in)
                    ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            ),
            200,
        )
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


# --- Rotas de Impulsionamento (Boost) ---


@shopee_bp.route("/boost/status", methods=["GET"])
@token_required
@permission_required("view_boost")
def get_boost_status():
    """Retorna o status geral do impulsionamento e produtos ativos."""
    try:
        import logging

        logger = logging.getLogger(__name__)
        logger.info("Iniciando get_boost_status")

        controller = BoostController()
        logger.info("Controller inicializado")

        resp = controller.client.get_boosted_list()
        logger.info(
            f"Resposta do get_boosted_list recebida: {resp.get('error') if resp else 'None'}"
        )

        active_boosts = []
        if not resp.get("error"):
            active_boosts = resp.get("response", {}).get("item_list", [])

        config = Configuracoes.query.first()
        boost_mode = config.boost_mode if config else "sequential"

        # Busca logs recentes
        logs = BoostLog.query.order_by(BoostLog.criado_em.desc()).limit(20).all()

        # Busca produtos configurados para boost (Apenas Ativos e COM ESTOQUE MINIMO >= 3)
        enabled_count = (
            Anuncios.query.filter(Anuncios.status.in_(["NORMAL", "ATIVO"]))
            .filter(Anuncios.estoque_total >= 3)
            .count()
        )
        priority_count = (
            Anuncios.query.filter(Anuncios.status.in_(["NORMAL", "ATIVO"]))
            .filter(Anuncios.estoque_total >= 3)
            .filter_by(boost_priority=True)
            .count()
        )

        # Busca próximos da fila (50 slots futuros)
        next_boosts = controller.get_next_boosts(limit=50)

        # Enriquecer active_boosts com dados locais (Nome e SKU)
        enriched_active = []
        for ab in active_boosts:
            item_id = str(ab.get("item_id"))
            local_item = Anuncios.query.filter_by(shopee_item_id=item_id).first()
            enriched_active.append(
                {
                    "item_id": item_id,
                    "cool_down_second": ab.get("cool_down_second"),
                    "nome": local_item.nome if local_item else "Desconhecido",
                    "sku": local_item.sku_pai if local_item else "N/A",
                }
            )

        return jsonify(
            {
                "active_boosts": enriched_active,
                "enabled_count": enabled_count,
                "priority_count": priority_count,
                "boost_mode": boost_mode,
                "next_boosts": [
                    {
                        "shopee_item_id": a.shopee_item_id,
                        "nome": a.nome,
                        "sku": a.sku_pai or "",
                        "boost_priority": a.boost_priority,
                    }
                    for a in next_boosts
                ],
                "logs": [l.to_dict() for l in logs],
            }
        )
    except Exception as e:
        import logging

        logging.getLogger(__name__).error(f"Erro em get_boost_status: {e}")
        return jsonify({"mensagem": f"Erro interno: {str(e)}"}), 500


@shopee_bp.route("/boost/toggle", methods=["POST"])
@token_required
@permission_required("view_boost")
def toggle_boost():
    """Ativa ou desativa o impulsionamento automático para um item."""
    data = request.json
    item_id = data.get("itemId")
    enabled = data.get("enabled", False)

    if not item_id:
        return jsonify({"mensagem": "ItemID não fornecido"}), 400

    anuncio = Anuncios.query.filter_by(shopee_item_id=str(item_id)).first()
    if not anuncio:
        return jsonify({"mensagem": "Anúncio não encontrado"}), 404

    anuncio.boost_enabled = enabled
    db.session.commit()

    status_msg = "ativado" if enabled else "desativado"
    return jsonify(
        {"mensagem": f"Impulsionamento automático {status_msg} para {anuncio.nome}"}
    )


@shopee_bp.route("/boost/priority", methods=["POST"])
@token_required
@permission_required("view_boost")
def toggle_boost_priority():
    """Define a prioridade de um item para o impulsionamento."""
    data = request.json
    item_id = data.get("itemId")
    priority = data.get("priority", False)

    anuncio = Anuncios.query.filter_by(shopee_item_id=str(item_id)).first()
    if not anuncio:
        return jsonify({"mensagem": "Anúncio não encontrado"}), 404

    anuncio.boost_priority = priority
    db.session.commit()
    return jsonify(
        {
            "mensagem": f"Prioridade {'ativada' if priority else 'desativada'} para {anuncio.nome}"
        }
    )


@shopee_bp.route("/boost/mode", methods=["POST"])
@token_required
@permission_required("view_boost")
def update_boost_mode():
    """Altera o modo de seleção do impulsionamento (sequential/random)."""
    data = request.json
    mode = data.get("mode")
    if mode not in ["sequential", "random"]:
        return jsonify({"mensagem": "Modo inválido"}), 400

    config = Configuracoes.query.first()
    if not config:
        config = Configuracoes()
        db.session.add(config)

    config.boost_mode = mode
    db.session.commit()
    return jsonify({"mensagem": f"Modo de impulsionamento alterado para {mode}"})


@shopee_bp.route("/boost/manual", methods=["POST"])
@token_required
@permission_required("view_boost")
def manual_boost():
    """Impulsiona um item manualmente via API da Shopee."""
    data = request.json
    item_id = data.get("itemId")

    # Verifica se o item existe e tem estoque
    anuncio = Anuncios.query.filter_by(shopee_item_id=str(item_id)).first()
    if anuncio and anuncio.estoque_total <= 0:
        return (
            jsonify(
                {
                    "status": "erro",
                    "mensagem": "Falha ao impulsionar: O anúncio está sem estoque (Stock 0).",
                }
            ),
            400,
        )
    if anuncio and anuncio.status not in ["NORMAL", "ATIVO"]:
        return (
            jsonify(
                {
                    "status": "erro",
                    "mensagem": f"Falha ao impulsionar: Status do anúncio é {anuncio.status}.",
                }
            ),
            400,
        )

    controller = BoostController()
    resp = controller.client.boost_item(item_id)

    if resp.get("error") and resp.get("error") != "":
        msg = resp.get("message") or resp.get("error")
        import logging

        logging.getLogger(__name__).error(
            f"Erro no Impulso Manual (Item {item_id}): {resp}"
        )
        return (
            jsonify({"status": "erro", "mensagem": f"Falha ao impulsionar: {msg}"}),
            400,
        )

    # Atualiza no banco que foi impulsionado
    anuncio = Anuncios.query.filter_by(shopee_item_id=str(item_id)).first()
    if anuncio:
        from controller.shopee_boost import get_br_now

        anuncio.last_boost_at = get_br_now()
        anuncio.boost_end_at = get_br_now() + timedelta(hours=4)
        db.session.commit()

    return jsonify(
        {"status": "sucesso", "mensagem": "Produto impulsionado com sucesso na Shopee!"}
    )


@shopee_bp.route("/boost/run-now", methods=["POST"])
@token_required
@permission_required("view_boost")
def run_boost_now():
    """Dispara o ciclo de impulsionamento imediatamente."""
    try:
        from controller.shopee_boost import BoostController

        controller = BoostController()
        result = controller.run_boost_cycle()
        return jsonify({"status": "sucesso", "mensagem": f"Ciclo executado: {result}"})
    except Exception as e:
        return (
            jsonify(
                {"status": "erro", "mensagem": f"Erro ao executar ciclo: {str(e)}"}
            ),
            500,
        )


@shopee_bp.route("/boost/announcements", methods=["GET"])
@token_required
@permission_required("view_boost")
def get_boost_announcements():
    """Retorna a lista de anúncios para o dashboard de boost com paginação."""
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 10, type=int)
    search = request.args.get("search", "")
    show_all = request.args.get("showAll", "false").lower() == "true"

    query = Anuncios.query.filter(Anuncios.status.in_(["NORMAL", "ATIVO"])).filter(
        Anuncios.estoque_total >= 3
    )

    # Se show_all for falso (padrão), removemos quem está com boost ativo
    if not show_all:
        query = query.filter(Anuncios.boost_end_at == None)
    if search:
        query = query.filter(
            Anuncios.nome.ilike(f"%{search}%")
            | Anuncios.shopee_item_id.ilike(f"%{search}%")
            | Anuncios.sku_pai.ilike(f"%{search}%")
        )

    # Ordenar por prioridade e depois por nome
    pagination = query.order_by(
        Anuncios.boost_priority.desc(), Anuncios.nome.asc()
    ).paginate(page=page, per_page=per_page, error_out=False)

    return jsonify(
        {
            "items": [
                {
                    "id": a.id,
                    "shopee_item_id": a.shopee_item_id,
                    "nome": a.nome,
                    "sku": a.sku_pai,
                    "boost_enabled": a.boost_enabled,
                    "boost_priority": a.boost_priority,
                    "last_boost_at": (
                        a.last_boost_at.isoformat() if a.last_boost_at else None
                    ),
                }
                for a in pagination.items
            ],
            "total": pagination.total,
            "pages": pagination.pages,
            "current_page": pagination.page,
        }
    )


@shopee_bp.route("/boost/logs", methods=["GET"])
@token_required
@permission_required("view_boost")
def get_boost_logs():
    """Retorna o histórico de logs de boost filtrado por impulsos ou erros."""
    from sqlalchemy import or_

    logs = (
        BoostLog.query.filter(
            BoostLog.acao.in_(["boost_start", "boost_error"])
        )
        .order_by(BoostLog.criado_em.desc())
        .limit(200)
        .all()
    )
    return jsonify([l.to_dict() for l in logs])


def _format_announcement(a: Anuncios, dias_espera: int = 0):
    """Auxiliar para formatar um objeto Anuncios para o frontend."""
    from model.shopeeModel import HistoricoPreco
    from datetime import datetime

    item_data = {
        "itemId": a.shopee_item_id,
        "title": a.nome,
        "skuPai": a.sku_pai,
        "status": a.status or "NORMAL",
        "updatedAt": (
            a.updated_at.strftime("%d/%m/%Y %H:%M:%S")
            if getattr(a, "updated_at", None)
            else ""
        ),
        "variacoes": [],
    }

    # Ordenar para priorizar IDs de modelo reais sobre o '0' e variações com SKU preenchido
    all_variations = sorted(
        a.variacoes,
        key=lambda x: (
            1 if str(x.shopee_model_id or "0") == "0" else 0,
            1 if not x.sku else 0,
        ),
    )

    seen_models = set()
    seen_skus = set()

    # Verificar se existem modelos reais (diferentes de "0") para este anúncio
    has_real_models = any(str(v.shopee_model_id or "0") != "0" for v in a.variacoes)

    for p in all_variations:
        mid = str(p.shopee_model_id or "0")

        # Regra de Ocultação Refinada: ocultar "0" apenas se existirem outros modelos reais
        if mid == "0" and has_real_models:
            continue

        sku_clean = str(p.sku or "").strip()

        # Deduplicação robusta
        if mid in seen_models:
            continue
        if sku_clean and sku_clean in seen_skus:
            continue

        seen_models.add(mid)
        if sku_clean:
            seen_skus.add(sku_clean)

        # Preço de exibição prioriza o promocional
        display_price = (
            p.preco_promocional
            if (p.preco_promocional and p.preco_promocional > 0)
            else p.preco_base
        )

        # Limpar nome "nan" (comum em imports pandas)
        nome_clean = p.nome_variacao
        if nome_clean and str(nome_clean).lower() == "nan":
            nome_clean = ""

        dias_faltantes = 0
        if dias_espera > 0 and p.preco_modificado_em:
            import pytz

            agora_br = datetime.now(pytz.timezone("America/Sao_Paulo")).replace(
                tzinfo=None
            )
            dias_passados = (agora_br - p.preco_modificado_em).days
            if dias_passados < dias_espera:
                dias_faltantes = dias_espera - dias_passados

        # --- Precificação Automática de COMBOS ---
        suggested_price = None
        import re

        combo_detected = False

        if sku_clean:
            # === MÉTODO 1: Combo por sufixo -C{N} no final do SKU ===
            # Ex: ABC-C2, ABC-C3 → base = ABC, busca no banco
            end_match = re.search(r"[- ]C(\d+)$", sku_clean, re.IGNORECASE)
            if end_match:
                try:
                    n = int(end_match.group(1))
                    if n >= 2:
                        base_sku = sku_clean[: end_match.start()].strip()
                        from model.shopeeModel import Produtos

                        base_prod = Produtos.query.filter(
                            (Produtos.sku == base_sku)
                            | (Produtos.sku == base_sku + "-C1")
                            | (Produtos.sku == base_sku + "-S1")
                            | (Produtos.sku == base_sku + "-UN")
                            | (Produtos.sku == base_sku + "-1")
                        ).first()

                        if not base_prod:
                            base_prod = Produtos.query.filter(
                                (Produtos.sku.like(f"{base_sku}%"))
                                & (~Produtos.sku.like(f"{base_sku}-C%"))
                                & (~Produtos.sku.contains("KIT"))
                            ).first()

                        if base_prod:
                            bp = (
                                base_prod.preco_promocional
                                if (
                                    base_prod.preco_promocional
                                    and base_prod.preco_promocional > 0
                                )
                                else base_prod.preco_base
                            )
                            if bp and bp > 0:
                                suggested_price = round((bp * n) * (0.99 ** (n - 1)), 2)
                                combo_detected = True
                except Exception as e:
                    print(f"⚠️ Erro combo (sufixo) {sku_clean}: {e}")

            # === MÉTODO 2: Combo por padrão -C{N}-{X} no meio do SKU ===
            # Ex: LB-02301-C2-1 → base = LB-02301-C1-1, busca irmã + banco
            if not combo_detected:
                mid_match = re.search(r"-C(\d+)-", sku_clean, re.IGNORECASE)
                if mid_match:
                    try:
                        n = int(mid_match.group(1))
                        if n >= 2:
                            # Substituir apenas o número: C2→C1, C3→C1
                            base_sku = (
                                sku_clean[: mid_match.start(1)]
                                + "1"
                                + sku_clean[mid_match.end(1) :]
                            )
                            bp = None

                            # 1. Buscar irmã C1 no MESMO anúncio
                            for sib in all_variations:
                                s_sku = str(sib.sku or "").strip()
                                if str(sib.shopee_model_id or "0") == mid:
                                    continue
                                if s_sku == base_sku:
                                    bp = (
                                        sib.preco_promocional
                                        if (
                                            sib.preco_promocional
                                            and sib.preco_promocional > 0
                                        )
                                        else sib.preco_base
                                    )
                                    break

                            # 2. Irmã sem -C no mesmo anúncio (variação unitária)
                            if not bp:
                                for sib in all_variations:
                                    s_sku = str(sib.sku or "").strip()
                                    if str(sib.shopee_model_id or "0") == mid:
                                        continue
                                    if not re.search(r"-C\d+", s_sku, re.IGNORECASE):
                                        sib_p = (
                                            sib.preco_promocional
                                            if (
                                                sib.preco_promocional
                                                and sib.preco_promocional > 0
                                            )
                                            else sib.preco_base
                                        )
                                        if sib_p and sib_p > 0:
                                            bp = sib_p
                                            break

                            # 3. Fallback: buscar no banco inteiro
                            if not bp:
                                from model.shopeeModel import Produtos

                                base_prod = Produtos.query.filter(
                                    Produtos.sku == base_sku
                                ).first()
                                if base_prod:
                                    bp = (
                                        base_prod.preco_promocional
                                        if (
                                            base_prod.preco_promocional
                                            and base_prod.preco_promocional > 0
                                        )
                                        else base_prod.preco_base
                                    )

                            if bp and bp > 0:
                                suggested_price = round((bp * n) * (0.99 ** (n - 1)), 2)
                                combo_detected = True
                    except Exception as e:
                        print(f"⚠️ Erro combo (variação) {sku_clean}: {e}")

        item_data["variacoes"].append(
            {
                "itemId": p.shopee_item_id,
                "modelId": mid,
                "nome_variacao": nome_clean,
                "sku": p.sku,
                "price": display_price,
                "price_base": p.preco_base,
                "price_promo": p.preco_promocional,
                "suggested_price": suggested_price,  # Novo campo para o frontend
                "promotion_id": p.promotion_id,
                "ean": p.ean,
                "status": p.situacao or "NORMAL",
                "dias_faltantes": dias_faltantes,
                "updatedAt": (
                    p.updated_at.strftime("%d/%m/%Y %H:%M:%S")
                    if getattr(p, "updated_at", None)
                    else ""
                ),
            }
        )

    if item_data["variacoes"]:
        # Calcular min/max com base no que o cliente realmente paga (display_price)
        prices = [v["price"] for v in item_data["variacoes"] if v["price"] > 0]
        if prices:
            item_data["min_price"] = min(prices)
            item_data["max_price"] = max(prices)
            item_data["price"] = item_data["min_price"]
        else:
            item_data["min_price"] = 0.0
            item_data["max_price"] = 0.0
            item_data["price"] = 0.0
    else:
        # Tenta pegar do pai se não houver variações explicítas no mapeamento?
        # Na verdade, no nosso schema, produtos simples tem model_id="0"
        item_data["price"] = 0.0
        item_data["min_price"] = 0.0
        item_data["max_price"] = 0.0

    return item_data


@shopee_bp.route("/shopee/sync-all", methods=["POST"])
@token_required
@permission_required("update_price")
def sync_all_announcements():
    from config.redis_config import shopee_queue

    # Enfileira o trabalho no RQ com um timeout de 1 hora (3600 segundos)
    # já que sincronizações grandes podem demorar bastante.
    job = shopee_queue.enqueue(
        "controller.shopee_update.shopee_update_controller.run_full_sync_job",
        job_timeout=3600,
    )

    return (
        jsonify(
            {
                "status": "sucesso",
                "mensagem": "Sincronização total enfileirada com sucesso.",
                "job_id": job.id,
            }
        ),
        200,
    )


@shopee_bp.route("/shopee/sync-progress", methods=["GET"])
def get_sync_progress():
    res = shopee_service.get_sync_progress()
    return jsonify(res), 200


@shopee_bp.route("/shopee/cancel-sync", methods=["GET", "POST"])
@token_required
def cancel_sync():
    res = shopee_service.cancelar_sincronizacao()
    return jsonify(res), 200


@shopee_bp.route("/shopee/sync-item/<item_id>", methods=["POST"])
@token_required
@permission_required("update_price")
def sync_item(item_id):
    """Sincroniza um item específico em tempo real e retorna os dados novos."""
    res = shopee_service.sync_item_from_shopee(item_id)
    if res["status"] == "erro":
        return jsonify(res), 400

    # Buscar o item atualizado para retornar
    item = Anuncios.query.filter_by(shopee_item_id=str(item_id)).first()
    if item:
        from model.shopeeModel import Configuracoes

        config = Configuracoes.query.first()
        dias_espera = config.dias_espera_simples if config else 15

        item_data = _format_announcement(item, dias_espera)
        return jsonify({"status": "sucesso", "item": item_data}), 200

    return jsonify(res), 200


@shopee_bp.route("/shopee/sync-batch", methods=["POST"])
@token_required
@permission_required("update_price")
def sync_batch():
    """Sincroniza uma lista de itens específicos."""
    dados = request.get_json()
    item_ids = dados.get("itemIds", [])

    if not item_ids:
        return jsonify({"status": "erro", "mensagem": "Nenhum item selecionado"}), 400

    creds, erro = auth_shopee.ensure_valid_token()
    if erro:
        return jsonify({"status": "erro", "mensagem": erro}), 401

    res = shopee_service.sync_batch_from_shopee(item_ids, creds)
    return (
        jsonify(
            {
                "status": "sucesso",
                "mensagem": f"{res.get('sucessos', 0)} anúncios sincronizados com sucesso.",
                "detalhes": res,
            }
        ),
        200,
    )


@shopee_bp.route("/shopee/announcements", methods=["GET"])
def get_announcements():
    """
    Retorna a lista de anúncios (Pais) com suas variações (Filhos).
    """
    try:
        page = request.args.get("page", 1, type=int)
        per_page = request.args.get("per_page", 10, type=int)
        search = request.args.get("search", "", type=str)
        filter_type = request.args.get("filter", "all", type=str)
        sort_field = request.args.get("sort", "updated_at", type=str)
        sort_order = request.args.get("order", "desc", type=str)

        # Limpar busca
        search = search.strip()
        search_str = f"%{search}%" if search else None

        # Base query com carregamento de variações
        query = Anuncios.query.options(selectinload(Anuncios.variacoes))

        from model.shopeeModel import Configuracoes, Produtos

        config = Configuracoes.query.first()
        dias_espera = config.dias_espera_simples if config else 15

        # 1. Aplicar Joins Necessários (Apenas uma vez)
        # Se houver busca ou filtros que dependam de Produtos, fazemos o join agora
        needs_products_join = bool(search) or filter_type in ["active", "inactive", "locked"]
        
        if needs_products_join:
            # Usamos outerjoin para busca para não excluir anúncios sem variações que batem no nome
            # Mas se for filtro de status (active/inactive), o join interno é mais apropriado.
            # Para simplificar e manter a busca funcional, usamos outerjoin e controlamos no filter.
            query = query.outerjoin(Anuncios.variacoes)

        # 2. Construir Filtros
        filters = []

        if search:
            search_filters = [
                Anuncios.nome.ilike(search_str),
                Anuncios.sku_pai.ilike(search_str),
                Anuncios.shopee_item_id.ilike(search_str)
            ]
            
            # Adiciona filtros de variação se o join existir
            if needs_products_join:
                search_filters.extend([
                    Produtos.sku.ilike(search_str),
                    Produtos.ean.ilike(search_str),
                    Produtos.nome_variacao.ilike(search_str)
                ])
                
            filters.append(or_(*search_filters))

        if filter_type == "locked":
            from datetime import datetime, timedelta
            import pytz
            agora_br = datetime.now(pytz.timezone("America/Sao_Paulo")).replace(tzinfo=None)
            limite = agora_br - timedelta(days=dias_espera)
            filters.append(Produtos.preco_modificado_em > limite)
            
        elif filter_type == "promo":
            subq = db.session.query(Produtos.shopee_item_id).filter(
                (Produtos.promotion_id != None) & (Produtos.promotion_id != "")
            ).distinct().subquery()
            filters.append(Anuncios.shopee_item_id.in_(subq))
            
        elif filter_type in ["no-promo", "available"]:
            subq = db.session.query(Produtos.shopee_item_id).filter(
                (Produtos.promotion_id != None) & (Produtos.promotion_id != "")
            ).distinct().subquery()
            filters.append(~Anuncios.shopee_item_id.in_(subq))
            
        elif filter_type == "active":
            filters.append(Produtos.situacao == "NORMAL")
            
        elif filter_type == "inactive":
            filters.append(Produtos.situacao != "NORMAL")

        # Aplicar todos os filtros acumulados
        if filters:
            query = query.filter(*filters).distinct()

        # 3. Ordenação Dinâmica
        col = Anuncios.nome if sort_field == "title" else Anuncios.updated_at
        if sort_order == "asc":
            query = query.order_by(col.asc())
        else:
            query = query.order_by(col.desc())

        # 4. Executar Paginação
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        lista_final = []
        for a in pagination.items:
            formatted = _format_announcement(a, dias_espera)
            # Apenas incluir se sobrar alguma variação após a filtragem estrita do model 0
            if formatted.get("variacoes"):
                lista_final.append(formatted)

        return (
            jsonify(
                {
                    "items": lista_final,
                    "total": pagination.total,
                    "pages": pagination.pages,
                    "current_page": pagination.page,
                    "per_page": pagination.per_page,
                }
            ),
            200,
        )
    except Exception as e:
        import traceback

        traceback.print_exc()
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@shopee_bp.route("/shopee/import-spreadsheet", methods=["POST"])
@token_required
@permission_required("update_price")
def import_spreadsheet():
    """
    Importa anúncios de uma planilha. Prioriza o banco local e faz fallback para a API Shopee.
    """
    try:
        if "file" not in request.files:
            return jsonify({"status": "erro", "mensagem": "Arquivo não enviado"}), 400

        file = request.files["file"]
        if file.filename == "":
            return jsonify({"status": "erro", "mensagem": "Nome de arquivo vazio"}), 400

        # 1. Ler planilha bruta para detectar cabeçalho dinamicamente
        content = file.read()
        df_raw = pd.read_excel(io.BytesIO(content), engine="calamine", header=None)

        header_idx: int = 0
        df = df_raw
        for idx, row in df_raw.iterrows():
            row_str = " ".join([str(v).lower() for v in row.values if pd.notna(v)])
            if (
                "id do produto" in row_str
                or "id do item" in row_str
                or "sku" in row_str
            ):
                header_idx = int(idx)  # type: ignore
                df = pd.read_excel(
                    io.BytesIO(content), engine="calamine", skiprows=header_idx
                )
                break

        # Normalizar nomes das colunas (remover espaços, acentos e caracteres especiais)
        df.columns = [
            str(c)
            .strip()
            .lower()
            .replace(" ", "")
            .replace("ç", "c")
            .replace("ã", "a")
            .replace("õ", "o")
            .replace("á", "a")
            .replace("é", "e")
            .replace("í", "i")
            .replace("ó", "o")
            .replace("ú", "u")
            for c in df.columns
        ]

        # Mapeamento robusto de colunas (Suporta nomes amigáveis e chaves internas da Shopee)
        sku_key = next(
            (
                c
                for c in df.columns
                if "variation_sku" in c
                or "parent_sku" in c
                or "skudereferencia" in c
                or "skudavariação" in c
                or "sku" == c
                or "sku" in c
            ),
            None,
        )

        price_key = next(
            (
                c
                for c in df.columns
                if "variation_price" in c
                or "unit_price" in c
                or "preço" in c
                or "preco" in c
                or "price" in c
            ),
            None,
        )

        id_item_key = next(
            (
                c
                for c in df.columns
                if "product_id" in c
                or "iddoproduto" in c
                or "itemid" in c
                or "productid" in c
            ),
            None,
        )
        id_model_key = next(
            (
                c
                for c in df.columns
                if "variation_id" in c
                or "model_id" in c
                or "iddavariacao" in c
                or "idmodelo" in c
                or "idavariaçao" in c
            ),
            None,
        )
        nome_pai_key = next(
            (
                c
                for c in df.columns
                if "product_name" in c or "nomedoproduto" in c or "titulo" in c
            ),
            None,
        )
        nome_var_key = next(
            (
                c
                for c in df.columns
                if "variation_name" in c
                or "nomedavariação" in c
                or "nome_variacao" in c
                or "nomevariation" in c
            ),
            None,
        )
        sku_pai_key = next(
            (
                c
                for c in df.columns
                if "parent_sku" in c or "skudereferencia" in c or "skupai" in c
            ),
            None,
        )

        if not sku_key or not price_key:
            return (
                jsonify(
                    {
                        "status": "erro",
                        "mensagem": f"Colunas SKU ou Preço não encontradas. Detectadas: {list(df.columns)}",
                    }
                ),
                400,
            )

        # 2. Processamento via Service
        stats, erro = shopee_service.process_spreadsheet(df)

        if erro:
            return jsonify({"status": "erro", "mensagem": erro}), 400

        return (
            jsonify(
                {
                    "status": "sucesso",
                    "estatisticas": stats,
                    "mensagem": f"Importação finalizada. Sucessos: {stats['sucesso']}. Use 'Sincronizar Anúncios' para enviar para a Shopee.",
                }
            ),
            200,
        )

    except Exception as e:
        import traceback

        traceback.print_exc()
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@shopee_bp.route("/shopee/update-price", methods=["POST"])
@token_required
@permission_required("update_price")
def update_price_api():
    """Interface para atualização de preço compatível com o frontend React."""
    try:
        from model.shopeeModel import Configuracoes, HistoricoPreco, Produtos
        from datetime import datetime

        dados = request.get_json()
        item_id = str(dados.get("itemId") or dados.get("item_id"))
        price_list = dados.get("priceList") or dados.get("price_list")

        if not item_id:
            return jsonify({"status": "erro", "mensagem": "itemId é obrigatório"}), 400

        # Validar regra de dias de espera para cada item solicitado (Trava de Segurança)
        if price_list and isinstance(price_list, list):
            for p_req in price_list:
                m_id = str(p_req.get("model_id") or "0")
                is_locked, _, lock_msg = shopee_service.validate_price_lock(
                    item_id, m_id
                )
                if is_locked:
                    return jsonify({"status": "erro", "mensagem": lock_msg}), 403

        # Suporte para lista de preços (Lote)
        user_id = (
            getattr(g, "current_user", None).id
            if getattr(g, "current_user", None)
            else None
        )
        custom_msg = dados.get("mensagem")
        force_promo = dados.get("forcePromotion", False)
        origem = dados.get("origem")

        if price_list and isinstance(price_list, list):
            resultado = shopee_service.alterar_precos_lote(
                item_id,
                price_list,
                user_id=user_id,
                custom_msg=custom_msg,
                force_promotion=force_promo,
                origem=origem,
            )
            status_code = 200
        else:
            # Caso padrão: Preço único
            model_id = dados.get("modelId") or dados.get("model_id")
            price = dados.get("price")

            if price is not None:
                try:
                    price = float(price)
                except (ValueError, TypeError):
                    return (
                        jsonify(
                            {
                                "status": "erro",
                                "mensagem": "price deve ser um número válido",
                            }
                        ),
                        400,
                    )

            if price is None or price <= 0:
                return (
                    jsonify({"status": "erro", "mensagem": "price deve ser > 0"}),
                    400,
                )

            resultado, status_code = shopee_service.update_price(
                price, item_id=item_id, model_id=model_id, user_id=user_id
            )

        # Se teve sucesso, anexar o anúncio atualizado para o frontend sincronizar
        if status_code == 200 and resultado.get("status") == "sucesso":
            anuncio_obj = Anuncios.query.filter_by(shopee_item_id=str(item_id)).first()
            if anuncio_obj:
                config = Configuracoes.query.first()
                d_espera = config.dias_espera_simples if config else 15
                resultado["item"] = _format_announcement(anuncio_obj, d_espera)

        return jsonify(resultado), status_code

    except Exception as e:
        import traceback

        traceback.print_exc()
        return (
            jsonify(
                {
                    "status": "erro",
                    "mensagem": f"Erro ao processar requisição: {str(e)}",
                }
            ),
            500,
        )


@shopee_bp.route("/shopee/history", methods=["GET"])
@token_required
def get_shopee_history():
    """Retorna os últimos 200 registros de alteração de preço do banco."""
    from model.shopeeModel import HistoricoPreco, Usuario

    try:
        registros = (
            HistoricoPreco.query.order_by(HistoricoPreco.criado_em.desc())
            .limit(200)
            .all()
        )

        resultado = []
        for r in registros:
            usuario_nome = None
            if r.usuario_id:
                u = Usuario.query.get(r.usuario_id)
                usuario_nome = u.nome if u else None
            resultado.append(
                {
                    "id": str(r.id),
                    "timestamp": r.criado_em.isoformat(),
                    "itemId": r.shopee_item_id,
                    "modelId": r.shopee_model_id,
                    "itemName": r.nome_produto,
                    "oldPrice": r.preco_anterior,
                    "price": r.preco_atual,
                    "status": r.status,
                    "message": r.mensagem,
                    "sku": r.sku,
                    "usuarioId": r.usuario_id,
                    "usuarioNome": usuario_nome,
                    "origem": r.origem,
                }
            )
        return jsonify(resultado), 200
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@shopee_bp.route("/shopee/history/clear", methods=["POST"])
def clear_shopee_history():
    """Remove todos os registros da tabela de histórico."""
    from model.shopeeModel import HistoricoPreco

    try:
        db.session.query(HistoricoPreco).delete()
        db.session.commit()
        return (
            jsonify({"status": "sucesso", "mensagem": "Histórico limpo com sucesso!"}),
            200,
        )
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@shopee_bp.route("/config", methods=["GET"])
def get_config():
    from model.shopeeModel import Configuracoes

    try:
        config = Configuracoes.query.first()
        if not config:
            config = Configuracoes(dias_espera_simples=15)
            db.session.add(config)
            db.session.commit()

        return (
            jsonify(
                {"status": "sucesso", "dias_espera_simples": config.dias_espera_simples}
            ),
            200,
        )
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@shopee_bp.route("/config", methods=["POST"])
def update_config():
    from model.shopeeModel import Configuracoes

    try:
        dados = request.get_json()
        dias = dados.get("dias_espera_simples")

        config = Configuracoes.query.first()
        if not config:
            config = Configuracoes()
            db.session.add(config)

        if dias is not None:
            config.dias_espera_simples = int(dias)
            db.session.commit()

            # Gatilho: Revalidar as travas IMEDIATAMENTE
            from controller.shopee_update.shopee_update_controller import ShopeeService

            service = ShopeeService()
            service.revalidate_all_locks()

        # Novos campos de horário
        hora = dados.get("hora_sincronizacao")
        minuto = dados.get("minuto_sincronizacao")

        if hora is not None:
            config.hora_sincronizacao = int(hora)
        if minuto is not None:
            config.minuto_sincronizacao = int(minuto)

        # Novo campo de intervalo de token
        intervalo = dados.get("intervalo_refresh_token")
        if intervalo is not None:
            config.intervalo_refresh_token = int(intervalo)

        db.session.commit()
        return (
            jsonify(
                {
                    "status": "sucesso",
                    "mensagem": "Configurações atualizadas e travas revalidadas!",
                }
            ),
            200,
        )
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


# --- Rotas de Promoções (V2 Discount) ---


@shopee_bp.route("/shopee/discounts/search", methods=["GET"])
def search_discounts_by_product():
    """Busca campanhas a partir de atributos de produto (item_id, SKU, nome).
    Retorna as campanhas que contêm produtos correspondentes à busca."""
    try:
        search = request.args.get("q", "", type=str).strip()
        status_filter = request.args.get("status", "all", type=str)

        if not search:
            return jsonify({"discount_list": [], "matched_products": {}}), 200

        from model.shopeeModel import Promocoes
        from sqlalchemy import distinct

        search_str = f"%{search}%"

        # 1. Buscar produtos que casam com a busca
        matched_query = (
            db.session.query(
                Produtos.promotion_id,
                Produtos.shopee_item_id,
                Produtos.shopee_model_id,
                Produtos.sku,
                Produtos.nome_variacao,
                Anuncios.nome,
                Anuncios.shopee_item_id.label("anuncio_item_id"),
                Anuncios.sku_pai,
            )
            .join(Anuncios, Produtos.shopee_item_id == Anuncios.shopee_item_id)
            .filter(
                Produtos.promotion_id != None,
                Produtos.promotion_id != "",
                (
                    Anuncios.nome.ilike(search_str)
                    | Anuncios.shopee_item_id.ilike(search_str)
                    | Anuncios.sku_pai.ilike(search_str)
                    | Produtos.sku.ilike(search_str)
                    | Produtos.shopee_item_id.ilike(search_str)
                    | Produtos.nome_variacao.ilike(search_str)
                ),
            )
        ).all()

        if not matched_query:
            return jsonify({"discount_list": [], "matched_products": {}}), 200

        # 2. Agrupar por promotion_id e coletar produtos correspondentes
        promo_ids = set()
        matched_products = {}  # discount_id -> list of matched product summaries

        for row in matched_query:
            pid = str(row.promotion_id)
            # Ignorar promoções detectadas automaticamente (sem ID numérico real)
            if pid in ("PROMO_DETECTADA", "None", ""):
                continue
            promo_ids.add(pid)

            if pid not in matched_products:
                matched_products[pid] = []
            matched_products[pid].append(
                {
                    "item_id": str(row.shopee_item_id),
                    "item_name": row.nome or "Sem Nome",
                    "model_id": str(row.shopee_model_id or "0"),
                    "model_name": row.nome_variacao or "",
                    "sku": row.sku or row.sku_pai or "",
                }
            )

        if not promo_ids:
            return jsonify({"discount_list": [], "matched_products": {}}), 200

        # 3. Buscar detalhes das campanhas correspondentes
        promo_id_ints = []
        for pid in promo_ids:
            try:
                promo_id_ints.append(int(pid))
            except ValueError:
                pass

        query = Promocoes.query.filter(Promocoes.discount_id.in_(promo_id_ints))

        if status_filter and status_filter != "all":
            query = query.filter_by(status=status_filter)

        promos = query.order_by(Promocoes.start_time.desc()).all()

        discount_list = []
        for p in promos:
            discount_list.append(
                {
                    "discount_id": str(p.discount_id),
                    "discount_name": p.discount_name,
                    "start_time": int(p.start_time.timestamp()) if p.start_time else 0,
                    "end_time": int(p.end_time.timestamp()) if p.end_time else 0,
                    "discount_status": p.status or "unknown",
                }
            )

        return (
            jsonify(
                {"discount_list": discount_list, "matched_products": matched_products}
            ),
            200,
        )

    except Exception as e:
        import traceback

        traceback.print_exc()
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@shopee_bp.route("/shopee/discounts/<discount_id>", methods=["GET"])
def get_discount_detail(discount_id):
    """Busca detalhes de uma única campanha."""
    try:
        res = shopee_service.get_shopee_discount_detail(discount_id)
        if res:
            return jsonify(res), 200

        # Se não achou no banco, tenta buscar na Shopee (Sync sob demanda)
        creds, erro = auth_shopee.ensure_valid_token()
        if not erro:
            # Forçar um sync da lista para popular o banco
            shopee_service.get_shopee_discounts(creds, status="all", page=1)
            res = shopee_service.get_shopee_discount_detail(discount_id)
            if res:
                return jsonify(res), 200

        return jsonify({"status": "erro", "mensagem": "Campanha não encontrada"}), 404
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@shopee_bp.route("/shopee/discounts", methods=["GET"])
def get_discounts():
    """Retorna lista de campanhas de desconto."""
    try:
        status = request.args.get("status", "all")
        page = request.args.get("page", 1, type=int)
        force_sync = request.args.get("sync", "false").lower() == "true"

        creds, erro = auth_shopee.ensure_valid_token()
        if erro:
            return jsonify({"status": "erro", "mensagem": erro}), 401

        res = shopee_service.get_shopee_discounts(
            creds, status=status, page=page, force_sync=force_sync
        )
        return jsonify(res), 200
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@shopee_bp.route("/shopee/discounts", methods=["POST"])
def add_discount():
    """Cria uma nova campanha de promoção."""
    try:
        dados = request.get_json()
        name = dados.get("name")
        start = dados.get("startTime") or dados.get("start_time")
        end = dados.get("endTime") or dados.get("end_time")

        if not all([name, start, end]):
            return (
                jsonify(
                    {
                        "status": "erro",
                        "mensagem": "Nome, Início e Fim são obrigatórios",
                    }
                ),
                400,
            )

        creds, erro = auth_shopee.ensure_valid_token()
        if erro:
            return jsonify({"status": "erro", "mensagem": erro}), 401

        res, code = shopee_service.add_shopee_discount(creds, name, start, end)
        return jsonify(res), code
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@shopee_bp.route("/shopee/discounts/<discount_id>", methods=["DELETE"])
def delete_discount(discount_id):
    """Exclui (upcoming) ou Encerra (ongoing) uma campanha."""
    try:
        status = request.args.get("status", "upcoming")
        creds, erro = auth_shopee.ensure_valid_token()
        if erro:
            return jsonify({"status": "erro", "mensagem": erro}), 401

        if status == "upcoming":
            res, code = shopee_service.delete_shopee_discount(creds, discount_id)
        else:
            res, code = shopee_service.end_shopee_discount(creds, discount_id)

        return jsonify(res), code
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@shopee_bp.route("/shopee/discounts/sync-all", methods=["POST"])
def sync_all_campaigns_route():
    """Sincroniza todas as campanhas ativas e seus itens."""
    try:
        creds, erro = auth_shopee.ensure_valid_token()
        if erro:
            return jsonify({"status": "erro", "mensagem": erro}), 401

        res = shopee_service.sync_all_active_campaigns(creds)
        return jsonify(res), 200
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@shopee_bp.route("/shopee/discounts/<discount_id>/items", methods=["GET"])
def get_discount_items(discount_id):
    """Lista itens de uma promoção específica."""
    try:
        page = request.args.get("page", 1, type=int)
        page_size = request.args.get("page_size", 20, type=int)

        creds, erro = auth_shopee.ensure_valid_token()
        if erro:
            return jsonify({"status": "erro", "mensagem": erro}), 401

        search = request.args.get("search", "")
        force_sync = request.args.get("sync", "false").lower() == "true"

        res = shopee_service.get_discount_item_list(
            creds,
            discount_id,
            page=page,
            page_size=page_size,
            search=search,
            force_sync=force_sync,
        )
        return jsonify(res), 200
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@shopee_bp.route("/shopee/discounts/<discount_id>/items", methods=["POST"])
def add_discount_items_route(discount_id):
    """Adiciona itens a uma promoção."""
    try:
        dados = request.get_json()
        items = dados.get("items", [])

        if not items:
            return jsonify({"status": "erro", "mensagem": "Nenhum item enviado"}), 400

        creds, erro = auth_shopee.ensure_valid_token()
        if erro:
            return jsonify({"status": "erro", "mensagem": erro}), 401

        origem = dados.get("origem", "Promocoes")
        res, code = shopee_service.add_discount_item(
            creds, discount_id, items, origem=origem
        )
        return jsonify(res), code
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@shopee_bp.route(
    "/shopee/discounts/<discount_id>/items/<item_id>/<model_id>", methods=["DELETE"]
)
def delete_discount_item_route(discount_id, item_id, model_id):
    """Remove um item/variação de uma promoção."""
    try:
        creds, erro = auth_shopee.ensure_valid_token()
        if erro:
            return jsonify({"status": "erro", "mensagem": erro}), 401

        res, code = shopee_service.delete_discount_item(
            creds, discount_id, item_id, model_id
        )
        return jsonify(res), code
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@shopee_bp.route("/shopee/item/<string:item_id>", methods=["GET"])
@token_required
def get_item_info(item_id):
    """Retorna informações de um item específico do banco local."""
    from model.shopeeModel import Anuncios

    try:
        anuncio = Anuncios.query.filter_by(shopee_item_id=item_id).first()
        if not anuncio:
            return jsonify({"status": "erro", "mensagem": "Item não encontrado"}), 404
        return jsonify(anuncio.to_dict()), 200
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@shopee_bp.route("/shopee/alerts/auto-promote", methods=["POST"])
def auto_promote_route():
    """Aciona a promoção automática de 25% para um item de alerta."""
    try:
        dados = request.get_json()
        item_id = dados.get("item_id")
        model_id = dados.get("model_id", "0")

        if not item_id:
            return jsonify({"status": "erro", "mensagem": "Item ID é obrigatório"}), 400

        creds, erro = auth_shopee.ensure_valid_token()
        if erro:
            return jsonify({"status": "erro", "mensagem": erro}), 401

        res, code = shopee_service.auto_promote_item(creds, item_id, model_id)

        return jsonify(res), code
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@shopee_bp.route("/shopee/notifications", methods=["GET"])
@token_required
def get_notifications():
    """Retorna as notificações do sistema."""
    from model.shopeeModel import NotificacaoSistema

    try:
        # Pega as últimas 50 notificações
        notifs = (
            NotificacaoSistema.query.order_by(NotificacaoSistema.criado_em.desc())
            .limit(50)
            .all()
        )
        return jsonify([n.to_dict() for n in notifs]), 200
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@shopee_bp.route("/shopee/notifications/read-all", methods=["POST"])
@token_required
def mark_all_notifications_as_read():
    """Marca todas as notificações como lidas."""
    from model.shopeeModel import NotificacaoSistema

    try:
        NotificacaoSistema.query.filter_by(lida=False).update(
            {NotificacaoSistema.lida: True}
        )
        db.session.commit()
        return jsonify({"status": "sucesso"}), 200
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@shopee_bp.route("/shopee/notifications/<int:id>/read", methods=["POST"])
@token_required
def mark_notification_as_read(id):
    """Marca uma notificação específica como lida."""
    from model.shopeeModel import NotificacaoSistema

    try:
        n = NotificacaoSistema.query.get(id)
        if n:
            n.lida = True
            db.session.commit()
        return jsonify({"status": "sucesso"}), 200
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500
