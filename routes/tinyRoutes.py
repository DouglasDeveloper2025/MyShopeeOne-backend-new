from flask import Blueprint, request, jsonify  # pyrefly: ignore [missing-import]
from model.shopeeModel import Produtos, Anuncios
import json
import logging

tiny_bp = Blueprint("tiny", __name__)
logger = logging.getLogger(__name__)


@tiny_bp.route("/tiny/webhook", methods=["POST", "GET"])
def tiny_webhook():
    # Tenta pegar os dados como JSON
    dados = request.get_json(force=True, silent=True)

    # Se falhar, pega como texto bruto
    if dados is None:
        try:
            dados = json.loads(request.data.decode("utf-8"))
        except:
            dados = request.data.decode("utf-8")

    print(dados)

    if isinstance(dados, dict) and dados.get("tipo") == "produto":
        produto_dados = dados.get("dados", {})
        sku = produto_dados.get("codigo")

        if sku:
            print(f"\n[Tiny ERP] Produto detectado. SKU (codigo): {sku}")
            print("[Tiny ERP] Buscando anuncios correspondentes")

            try:
                produtos_existentes = Produtos.query.filter(Produtos.sku == sku).all()
                anuncios_existentes = Anuncios.query.filter(
                    Anuncios.sku_pai == sku
                ).all()

                item_ids_encontrados = set()

                for p in produtos_existentes:
                    item_ids_encontrados.add(p.shopee_item_id)

                for a in anuncios_existentes:
                    item_ids_encontrados.add(a.shopee_item_id)

                if item_ids_encontrados:
                    print(
                        f"[Shopee Check] Anúncios JÁ EXISTENTES encontrados para o SKU '{sku}'. Item IDs: {list(item_ids_encontrados)}"
                    )
                    print(
                        "[Ação] Inspeção falhou: O anúncio já existe na Shopee. Ignorando a criação."
                    )
                    return (
                        jsonify(
                            {
                                "status": "sucesso",
                                "mensagem": "Anúncio já existe",
                                "recebido": True,
                            }
                        ),
                        200,
                    )
                else:
                    print(
                        f"[Shopee Check] Nenhum anúncio encontrado para o SKU '{sku}'."
                    )
                    print(
                        "[Ação] Inspeção passou: O anúncio não existe na Shopee e pode ser aplicado/criado."
                    )

                    # AQUI: Inserir a logica de criacao do anuncio na Shopee (se necessario futuramente)
                    return (
                        jsonify(
                            {
                                "status": "sucesso",
                                "mensagem": "Produto apto para criação",
                                "recebido": True,
                            }
                        ),
                        200,
                    )

            except Exception as e:
                print(f"[Erro] Falha ao acessar banco de dados da Shopee: {e}")
                return jsonify({"status": "erro", "mensagem": str(e)}), 500

    return jsonify({"status": "sucesso", "recebido": True}), 200
