import logging
from model.shopeeModel import db, Anuncios, Produtos, get_br_now

logger = logging.getLogger(__name__)

class TinyController:
    def process_webhook(self, payload):
        """
        Processa o payload do Tiny ERP. Valida duplicidade usando Item ID e SKU.
        Salva ou atualiza o anúncio e suas variações no banco de dados.
        """
        if not payload:
            return {"status": "erro", "mensagem": "Payload vazio."}, 400

        # Suporta tanto o webhook encapsulado do Tiny {"tipo": "produto", "dados": {...}}
        # quanto um envio direto contendo os dados do produto.
        if isinstance(payload, dict) and "dados" in payload and isinstance(payload["dados"], dict):
            p_info = payload["dados"]
        elif isinstance(payload, dict):
            p_info = payload
        else:
            return {"status": "erro", "mensagem": "Formato de payload inválido."}, 400

        sku = str(p_info.get("sku") or p_info.get("codigo") or "").strip()
        tiny_id = str(p_info.get("id") or p_info.get("idProduto") or p_info.get("id_produto") or "").strip()
        shopee_item_id = str(p_info.get("shopee_item_id") or p_info.get("item_id") or "").strip()

        # Funções auxiliares seguras para tipos numéricos
        def get_int(source, keys, default=0):
            for k in keys:
                v = source.get(k)
                if v is not None and str(v).strip() != "":
                    try:
                        return int(float(v))
                    except:
                        pass
            return default

        def get_float(source, keys, default=0.0):
            for k in keys:
                v = source.get(k)
                if v is not None and str(v).strip() != "":
                    try:
                        return float(v)
                    except:
                        pass
            return default

        nome = p_info.get("nome") or p_info.get("descricao") or ""
        preco = get_float(p_info, ["preco", "preco_base", "price"])
        estoque = get_int(p_info, ["estoque", "quantity", "stock", "estoqueAtual", "estoque_atual"])
        ean = p_info.get("ean") or p_info.get("gtin") or ""
        situacao = p_info.get("situacao") or p_info.get("status") or "NORMAL"

        # Tratamento simples para conversão de flags do Tiny ERP
        if situacao == "A":
            situacao = "NORMAL"
        elif situacao == "I":
            situacao = "UNLIST"

        if not sku:
            return {"status": "erro", "mensagem": "SKU (codigo) é obrigatório."}, 400

        try:
            agora = get_br_now()

            # 1. Verifica se o produto com este SKU já existe no banco de dados (anúncio pai ou variação)
            anuncio_por_sku = Anuncios.query.filter_by(sku_pai=sku).first()
            produto_por_sku = Produtos.query.filter_by(sku=sku).first()

            if anuncio_por_sku or produto_por_sku:
                print(f"[Tiny ERP] Produto com SKU {sku} já cadastrado no banco. Ignorando e retornando 200.")
                anuncio_existente = anuncio_por_sku or (produto_por_sku.anuncio if produto_por_sku else None)
                return {
                    "status": "sucesso",
                    "mensagem": f"Produto com SKU {sku} já cadastrado no banco. Ignorado.",
                    "anuncio": anuncio_existente.to_dict() if anuncio_existente else None
                }, 200

            # 2. Como o SKU não existe, o produto é novo. Resolve o shopee_item_id virtual
            anuncio = None
            if not shopee_item_id:
                # Produto novo no ERP: gera ID virtual temporário
                if tiny_id:
                    shopee_item_id = f"TINY_{tiny_id}"
                else:
                    shopee_item_id = f"TINY_SKU_{sku}"
                print(f"[Tiny ERP] Produto novo. Gerando shopee_item_id virtual temporário: {shopee_item_id}")

            if anuncio:
                # PRODUTO JÁ EXISTE: ATUALIZAÇÃO
                print(f"[Tiny ERP] Produto já existe no banco. Atualizando Anúncio ID {anuncio.shopee_item_id} (SKU: {sku})")
                anuncio.nome = nome or anuncio.nome
                # Garante sincronismo de Item ID e SKU se houve mapeamento
                anuncio.shopee_item_id = shopee_item_id
                anuncio.sku_pai = sku
                anuncio.status = situacao
                anuncio.updated_at = agora

                variacoes = p_info.get("variacoes", [])
                if variacoes:
                    estoque_total = 0
                    for var in variacoes:
                        v_model_id = str(var.get("shopee_model_id") or var.get("model_id") or "").strip()
                        v_sku = str(var.get("sku") or var.get("codigo") or "").strip()
                        v_nome = var.get("nome_variacao") or var.get("nome") or ""
                        v_preco = get_float(var, ["preco", "price"])
                        v_estoque = get_int(var, ["estoque", "stock", "estoqueAtual", "estoque_atual"])
                        v_ean = var.get("ean") or var.get("gtin") or ""

                        prod_var = None
                        if v_model_id:
                            prod_var = Produtos.query.filter_by(shopee_item_id=anuncio.shopee_item_id, shopee_model_id=v_model_id).first()
                        if not prod_var and v_sku:
                            prod_var = Produtos.query.filter_by(sku=v_sku).first()

                        if prod_var:
                            prod_var.nome_variacao = v_nome or prod_var.nome_variacao
                            prod_var.sku = v_sku or prod_var.sku
                            prod_var.preco_base = v_preco or prod_var.preco_base
                            prod_var.estoque = v_estoque
                            prod_var.ean = v_ean or prod_var.ean
                            prod_var.situacao = situacao
                            prod_var.updated_at = agora
                        else:
                            prod_var = Produtos(
                                anuncio_id=anuncio.id,
                                shopee_item_id=anuncio.shopee_item_id,
                                shopee_model_id=v_model_id or "0",
                                nome_variacao=v_nome,
                                sku=v_sku,
                                preco_base=v_preco,
                                estoque=v_estoque,
                                ean=v_ean,
                                situacao=situacao,
                                created_at=agora,
                                updated_at=agora
                            )
                            db.session.add(prod_var)
                        estoque_total += v_estoque
                    anuncio.estoque_total = estoque_total
                else:
                    # Produto Simples
                    prod_simples = Produtos.query.filter_by(shopee_item_id=anuncio.shopee_item_id, shopee_model_id="0").first()
                    if not prod_simples and sku:
                        prod_simples = Produtos.query.filter_by(sku=sku).first()

                    if prod_simples:
                        prod_simples.sku = sku
                        prod_simples.preco_base = preco or prod_simples.preco_base
                        prod_simples.estoque = estoque
                        prod_simples.ean = ean or prod_simples.ean
                        prod_simples.situacao = situacao
                        prod_simples.updated_at = agora
                    else:
                        prod_simples = Produtos(
                            anuncio_id=anuncio.id,
                            shopee_item_id=anuncio.shopee_item_id,
                            shopee_model_id="0",
                            sku=sku,
                            preco_base=preco,
                            estoque=estoque,
                            ean=ean,
                            situacao=situacao,
                            created_at=agora,
                            updated_at=agora
                        )
                        db.session.add(prod_simples)
                    anuncio.estoque_total = estoque
            else:
                # PRODUTO NÃO EXISTE: CRIAÇÃO
                print(f"[Tiny ERP] Produto não encontrado no banco. Criando novo Anúncio ID {shopee_item_id} (SKU: {sku})")
                anuncio = Anuncios(
                    shopee_item_id=shopee_item_id,
                    nome=nome,
                    sku_pai=sku,
                    status=situacao,
                    estoque_total=estoque,
                    created_at=agora,
                    updated_at=agora
                )
                db.session.add(anuncio)
                db.session.flush()  # Obtém anuncio.id para relacionar produtos

                variacoes = p_info.get("variacoes", [])
                if variacoes:
                    estoque_total = 0
                    for var in variacoes:
                        v_model_id = str(var.get("shopee_model_id") or var.get("model_id") or "").strip()
                        v_sku = str(var.get("sku") or var.get("codigo") or "").strip()
                        v_nome = var.get("nome_variacao") or var.get("nome") or ""
                        v_preco = get_float(var, ["preco", "price"])
                        v_estoque = get_int(var, ["estoque", "stock", "estoqueAtual", "estoque_atual"])
                        v_ean = var.get("ean") or var.get("gtin") or ""

                        prod_var = Produtos(
                            anuncio_id=anuncio.id,
                            shopee_item_id=shopee_item_id,
                            shopee_model_id=v_model_id or "0",
                            nome_variacao=v_nome,
                            sku=v_sku,
                            preco_base=v_preco,
                            estoque=v_estoque,
                            ean=v_ean,
                            situacao=situacao,
                            created_at=agora,
                            updated_at=agora
                        )
                        db.session.add(prod_var)
                        estoque_total += v_estoque
                    anuncio.estoque_total = estoque_total
                else:
                    prod_simples = Produtos(
                        anuncio_id=anuncio.id,
                        shopee_item_id=shopee_item_id,
                        shopee_model_id="0",
                        sku=sku,
                        preco_base=preco,
                        estoque=estoque,
                        ean=ean,
                        situacao=situacao,
                        created_at=agora,
                        updated_at=agora
                    )
                    db.session.add(prod_simples)

            db.session.commit()

            # Executa a sincronização em tempo real do item recém-criado na Shopee
            try:
                from controller.shopee_update.shopee_update_controller import ShopeeService
                shopee_service = ShopeeService()
                print(f"[Tiny ERP Webhook] Iniciando sincronização em tempo real para o SKU {sku} (ID Virtual: {shopee_item_id})...")
                sync_res = shopee_service.sync_item_from_shopee(shopee_item_id)
                print(f"[Tiny ERP Webhook] Resultado da sincronização em tempo real: {sync_res}")

                if sync_res.get("status") == "sucesso":
                    real_item_ids = sync_res.get("item_ids", [])
                    if real_item_ids:
                        anuncio_real = Anuncios.query.filter_by(shopee_item_id=real_item_ids[0]).first()
                        if anuncio_real:
                            anuncio = anuncio_real
            except Exception as sync_err:
                logger.error(f"[Tiny ERP Webhook] Erro ao realizar sincronização em tempo real: {sync_err}", exc_info=True)

            return {"status": "sucesso", "mensagem": "Produto salvo e sincronizado com sucesso no banco de dados.", "anuncio": anuncio.to_dict()}, 200

        except Exception as e:
            db.session.rollback()
            logger.error(f"[TinyController] Erro ao salvar/atualizar produto do Tiny: {e}", exc_info=True)
            return {"status": "erro", "mensagem": f"Erro ao persistir dados: {str(e)}"}, 500
