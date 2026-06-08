import hmac
import hashlib
import time
import os
import requests
import json
import re
import logging
from datetime import datetime, timedelta
import pytz
import math
from controller.auth.authShopee import TokenShopee
from sqlalchemy import or_  # pyrefly: ignore [missing-import]
from model.shopeeModel import db, HistoricoPreco, Produtos, Anuncios, Configuracoes

logger = logging.getLogger(__name__)


class SyncCancelledException(Exception):
    """Custom exception raised when synchronization is cancelled by the user."""
    pass


class ShopeeService:
    def __init__(self):
        self.tokens = TokenShopee()
        self.host_base = "https://partner.shopeemobile.com"
        self.tz_brasil = pytz.timezone("America/Sao_Paulo")

        # Controle de progresso para concorrência
        self.cancel_requested = False
        self.sync_status = {
            "is_running": False,
            "sucessos": 0,
            "erros": 0,
            "total": 0,
            "atual": 0,
            "mensagem": "Inativo",
        }

    def _safe_emit(self, event, data):
        """Emite evento via SocketIO de forma segura.

        Quando rodando no web server (com eventlet), importa o socketio normalmente.
        Quando rodando no RQ Worker (sem eventlet), silenciosamente ignora a emissão
        para evitar importar app.py e disparar eventlet.monkey_patch().
        """
        try:
            import os

            if os.environ.get("IS_RQ_WORKER") == "true":
                # No worker, nao temos SocketIO - apenas loga
                print(f"[RQ Worker] SocketIO emit ignorado: {event}")
                return
            from app import socketio

            socketio.emit(event, data)
        except Exception as e:
            print(f"[SocketIO] Falha ao emitir '{event}': {e}")

    def _get_brasilia_time(self):
        """Retorna o horário atual no fuso de Brasília como naive datetime (sem fuso)."""
        return datetime.now(self.tz_brasil).replace(tzinfo=None)

    def _get_wait_time_config(self):
        """Busca o tempo de espera configurado no banco de dados."""
        try:
            config = Configuracoes.query.first()
            return config.dias_espera_simples if config else 15
        except:
            return 15

    def validate_price_lock(self, item_id, model_id=0):
        """
        Valida se o item/modelo está sob a trava de 15 dias da Shopee (Alteração de Preço Base).
        Retorna (is_locked, dias_faltantes, mensagem).
        """
        try:
            mid = str(model_id or "0")
            prod = Produtos.query.filter_by(
                shopee_item_id=str(item_id), shopee_model_id=mid
            ).first()

            if not prod:
                return False, 0, ""

            # Se já estiver em promoção, a Shopee geralmente permite alterar o preço promocional
            # A trava é focada em Preço Base -> Promoção ou Preço Base -> Preço Base
            if prod.preco_promocional and prod.preco_promocional > 0:
                return False, 0, ""

            if prod.preco_modificado_em:
                try:
                    dias_espera = int(self._get_wait_time_config() or 0)
                except:
                    dias_espera = 15

                if dias_espera <= 0:
                    return False, 0, ""

                agora = self._get_brasilia_time()
                dias_passados = (agora - prod.preco_modificado_em).days

                if dias_passados < dias_espera:
                    faltam = dias_espera - dias_passados
                    nome = prod.nome_variacao or (
                        prod.anuncio.nome if prod.anuncio else "Item"
                    )
                    msg = f"O Anúncio '{nome}' teve o preço base alterado recentemente. Aguarde {faltam} dia(s) para nova alteração ou promoção."
                    return True, faltam, msg

            return False, 0, ""
        except Exception as e:
            print(f"Erro ao validar trava de preço: {e}")
            return False, 0, ""

    def _shopee_request(self, path, creds, params=None, method="GET", json_data=None):
        """Helper centralizado para chamadas à API da Shopee."""
        timestamp = int(time.time())
        string_base = f"{creds['partner_id']}{path}{timestamp}{creds['access_token']}{creds['shop_id']}"
        sign = hmac.new(
            creds["partner_key"].encode(), string_base.encode(), hashlib.sha256
        ).hexdigest()

        default_params = {
            "partner_id": int(creds["partner_id"]),
            "shop_id": int(creds["shop_id"]),
            "timestamp": timestamp,
            "access_token": creds["access_token"],
            "sign": sign,
        }
        if params:
            default_params.update(params)

        url = f"{self.host_base}{path}"
        try:
            if method.upper() == "POST":
                resp = requests.post(
                    url, params=default_params, json=json_data, timeout=60
                )
            else:
                resp = requests.get(url, params=default_params, timeout=60)

            status_code = resp.status_code

            # TENTATIVA DE AUTO-RECOVERY EM CASO DE 403 (TOKEN INVÁLIDO/EXPIRADO)
            if status_code == 403:
                print(
                    f"--- [403 DETECTED] Erro de autenticação em {path}. Tentando renovar token... ---"
                )
                from model.shopeeModel import IntegracaoShopee

                integracao = IntegracaoShopee.query.first()
                if integracao:
                    new_creds, erro = self.tokens._refresh_token(integracao)
                    if not erro:
                        print(
                            "--- [403 RECOVERY] Token renovado! Repetindo requisição... ---"
                        )
                        # Recalcular assinatura com novo token e repetir (retry_on_403=False para evitar loop)
                        return self._shopee_request(
                            path, new_creds, params, method, json_data
                        )
                    else:
                        print(f"--- [403 FAILURE] Falha ao renovar token: {erro} ---")

            return resp.json(), status_code
        except Exception as e:
            return {"error": str(e)}, 500

    def sincronizar_todos_anuncios(self, flask_app):
        """Dispara a sincronização global em uma Thread separada (Assíncrona)."""
        if self.sync_status["is_running"]:
            return {
                "status": "erro",
                "mensagem": "Uma sincronização já está em andamento.",
            }

        self.cancel_requested = False

        # Iniciar Thread injetando a APP
        import threading

        thread = threading.Thread(target=self._run_sync_worker, args=(flask_app,))
        thread.daemon = True
        thread.start()

        return {
            "status": "sucesso",
            "mensagem": "Sincronização iniciada em segundo plano.",
        }

    def _run_sync_worker_logic(self, item_ids, creds):
        """Lógica central de sincronização que atualiza o Redis."""
        from config.redis_config import redis_conn

        try:
            total = len(item_ids)
            # Resetar flags e status para novo início
            redis_conn.delete("shopee_sync_cancel")

            self._update_sync_status(
                is_running=True,
                total=total,
                atual=0,
                sucessos=0,
                erros=0,
                mensagem="Iniciando sincronização em lote...",
            )

            BATCH_SIZE = 50
            print(
                f"--- [DEBUG] Iniciando processamento de {total} itens em lotes de {BATCH_SIZE} ---"
            )

            for i in range(0, total, BATCH_SIZE):
                current_batch_num = (i // BATCH_SIZE) + 1
                # Verificar cancelamento via Redis (Checagem mais frequente)
                cancel_signal = redis_conn.get("shopee_sync_cancel")
                if cancel_signal:
                    print(
                        f"--- [SYNC] CANCELAMENTO DETECTADO no Lote {current_batch_num}. Parando... ---"
                    )
                    raise SyncCancelledException("Sincronização cancelada pelo usuário.")

                batch_ids = item_ids[i : i + BATCH_SIZE]
                print(
                    f"--- [DEBUG] Lote {current_batch_num}: Processando {len(batch_ids)} IDs ---"
                )

                try:
                    res_batch = self.sync_batch_from_shopee(batch_ids, creds)

                    # Verificação dupla após a chamada da API (que é o que mais demora)
                    if redis_conn.get("shopee_sync_cancel"):
                        raise SyncCancelledException("Sincronização cancelada pelo usuário.")

                    # Atualização atômica de progresso
                    prog = self.get_sync_progress()
                    prog["atual"] = min(i + len(batch_ids), total)
                    prog["sucessos"] += res_batch.get("sucessos", 0)
                    prog["erros"] += res_batch.get("erros", 0)
                    prog["mensagem"] = f"Sincronizando: {prog['atual']}/{total}"

                    self._update_sync_status(**prog)
                    print(
                        f"--- [DEBUG] Lote {current_batch_num} OK: {res_batch.get('sucessos')} sucessos ---"
                    )
                except SyncCancelledException as e_cancel:
                    raise e_cancel
                except Exception as e_batch:
                    print(f"[ERRO] Erro no lote {current_batch_num}: {e_batch}")
                    prog = self.get_sync_progress()
                    prog["erros"] += len(batch_ids)
                    prog["atual"] = min(i + len(batch_ids), total)
                    self._update_sync_status(**prog)

                time.sleep(0.5)

            final = self.get_sync_progress()
            self._update_sync_status(
                is_running=False,
                mensagem=f"Finalizado: {final['sucessos']} sucessos, {final['erros']} erros.",
            )
            print(f"--- [SYNC] Processo finalizado com sucesso. Total: {total} ---")

            # Verificar desbloqueios de 15 dias após sincronizar
            self.verificar_desbloqueios(item_ids)

        except SyncCancelledException as e:
            print(f"--- [SYNC] Lógica de sincronização interrompida devido ao cancelamento: {e} ---")
            self._update_sync_status(
                is_running=False,
                mensagem="Cancelado pelo usuário.",
            )
            redis_conn.delete("shopee_sync_cancel")
            raise e
        except Exception as e:
            print(f"[ERRO] Erro no worker: {e}")
            self._update_sync_status(is_running=False, mensagem=f"Erro: {str(e)}")

    def _run_sync_worker(self, app):
        """Thread que executa a sincronização pesada utilizando processamento em lote."""
        import traceback

        ctx = app.app_context()
        ctx.push()

        try:
            self.sync_status.update(
                {
                    "is_running": True,
                    "sucessos": 0,
                    "erros": 0,
                    "total": 0,
                    "atual": 0,
                    "mensagem": "Buscando IDs na Shopee...",
                }
            )

            # 1. Validar Token
            creds, erro = self.tokens.ensure_valid_token(1)
            if erro:
                self._update_sync_status(
                    is_running=False, mensagem=f"Erro de token: {erro}"
                )
                return

            # 2. Buscar IDs
            item_ids = self.get_item_ids(creds)
            if not item_ids:
                self._update_sync_status(
                    is_running=False, mensagem="Nenhum item encontrado."
                )
                return

            self.sync_status["total"] = len(item_ids)
            self.sync_status["mensagem"] = "Sincronizando itens em lote..."

            # 3. Processar em Lotes de 50
            BATCH_SIZE = 50
            for i in range(0, len(item_ids), BATCH_SIZE):
                # Checar sinal no Redis (Unificado)
                from config.redis_config import redis_conn

                if self.cancel_requested or redis_conn.get("shopee_sync_cancel"):
                    raise SyncCancelledException("Sincronização cancelada pelo usuário.")

                batch_ids = item_ids[i : i + BATCH_SIZE]
                self.sync_status["atual"] = min(i + len(batch_ids), len(item_ids))

                try:
                    # Sincronizar o lote
                    res_batch = self.sync_batch_from_shopee(batch_ids, creds)
                    self.sync_status["sucessos"] += res_batch.get("sucessos", 0)
                    self.sync_status["erros"] += res_batch.get("erros", 0)
                except SyncCancelledException as e_cancel:
                    raise e_cancel
                except Exception as e_batch:
                    print(f"[ERRO] Erro fatal no lote {i//BATCH_SIZE + 1}: {str(e_batch)}")
                    self.sync_status["erros"] += len(batch_ids)

                # Pequena pausa para evitar overstrain nos buffers locais, mas muito mais rápido que o loop individual
                time.sleep(0.5)

            self.sync_status.update(
                {
                    "is_running": False,
                    "mensagem": f"Finalizado: {self.sync_status['sucessos']} sucessos, {self.sync_status['erros']} erros.",
                }
            )

            # --- NOVO: Verificar desbloqueios de 15 dias após sincronizar ---
            self.verificar_desbloqueios(item_ids)

            # Salvar log no banco
            _save_sync_log(
                total_sincronizados=self.sync_status["sucessos"],
                erro=f"{self.sync_status['erros']} erro(s) durante a sincronização." if self.sync_status["erros"] > 0 else None
            )

        except SyncCancelledException as e:
            self.sync_status.update(
                {
                    "is_running": False,
                    "mensagem": "Cancelado pelo usuário.",
                }
            )
            self._update_sync_status(is_running=False, mensagem="Cancelado pelo usuário.")
            self._safe_emit("sync_finished", {"sucesso": False, "erro": "Cancelado pelo usuário."})
            _save_sync_log(
                total_sincronizados=self.sync_status.get("sucessos", 0),
                erro="Cancelado pelo usuário."
            )
        except Exception as e:
            error_msg = str(e)
            self.sync_status.update(
                {
                    "is_running": False,
                    "mensagem": f"Erro crítico: {error_msg}",
                    "error_critical": True,
                }
            )
            # Emitir falha
            self._safe_emit("sync_finished", {"sucesso": False, "erro": error_msg})
            _save_sync_log(
                total_sincronizados=self.sync_status.get("sucessos", 0),
                erro=f"Erro crítico: {error_msg}"
            )
        finally:
            # Emitir sucesso se não caiu no except crítico
            if not self.sync_status.get("error_critical") and not self.sync_status.get("mensagem") == "Cancelado pelo usuário.":
                self._safe_emit(
                    "sync_finished",
                    {
                        "sucesso": True,
                        "total": self.sync_status["total"],
                        "sucessos": self.sync_status["sucessos"],
                        "erros": self.sync_status["erros"],
                    },
                )
            ctx.pop()

    def sync_batch_from_shopee(self, item_id_list, creds):
        """Sincroniza um lote de itens da Shopee de forma eficiente."""
        from model.shopeeModel import Anuncios, Produtos
        from config.redis_config import redis_conn

        stats = {"sucessos": 0, "erros": 0}
        agora = self._get_brasilia_time()

        try:
            if redis_conn.get("shopee_sync_cancel"):
                raise SyncCancelledException("Sincronização cancelada pelo usuário.")

            # 1. Buscar Info Base em Lote
            items_info = self._get_item_base_info_batch(item_id_list, creds)
            if not items_info:
                return {"sucessos": 0, "erros": len(item_id_list)}

            if redis_conn.get("shopee_sync_cancel"):
                raise SyncCancelledException("Sincronização cancelada pelo usuário.")

            # 2. Buscar Promoções em Lote
            promos_all = self._get_active_promotion_batch(item_id_list, creds)

            # Mapear promos por item_id para busca rápida
            promo_map = {str(p["item_id"]): p.get("promotion", []) for p in promos_all}
            for info in items_info:
                if redis_conn.get("shopee_sync_cancel"):
                    raise SyncCancelledException("Sincronização cancelada pelo usuário.")

                iid = str(info["item_id"])
                try:
                    # Garantir Anúncio (Pai)
                    anuncio = Anuncios.query.filter_by(shopee_item_id=iid).first()
                    if not anuncio:
                        # Se não encontrar por item_id, busca por sku_pai onde o shopee_item_id começa com 'TINY_'
                        # para converter o anúncio virtual do ERP em anúncio real da Shopee.
                        sku_pai_val = info.get("item_sku")
                        if sku_pai_val:
                            anuncio = Anuncios.query.filter(
                                Anuncios.sku_pai == sku_pai_val,
                                Anuncios.shopee_item_id.like("TINY_%")
                            ).first()
                        
                        if anuncio:
                            print(f"[Sync] Convertendo anúncio virtual {anuncio.shopee_item_id} para ID Shopee real {iid}")
                            for p_var in anuncio.variacoes:
                                p_var.shopee_item_id = iid
                            anuncio.shopee_item_id = iid
                            db.session.flush()
                        else:
                            anuncio = Anuncios(
                                shopee_item_id=iid,
                                nome=info.get("item_name"),
                                sku_pai=info.get("item_sku"),
                                status=info.get("item_status", "NORMAL"),
                            )
                            db.session.add(anuncio)
                            db.session.flush()
                    else:
                        anuncio.nome = info.get("item_name", anuncio.nome)
                        anuncio.sku_pai = info.get("item_sku", anuncio.sku_pai)
                        anuncio.status = info.get("item_status", anuncio.status)

                    # Inicializa estoque total (será calculado abaixo)
                    anuncio.estoque_total = 0

                    promos_list = promo_map.get(iid, [])
                    has_model = info.get("has_model", False)

                    if has_model:
                        # 1. Limpeza Proativa de Fantasmas: Se tem variações, o model_id "0" deve ser removido
                        Produtos.query.filter_by(
                            shopee_item_id=iid, shopee_model_id="0"
                        ).delete()

                        modelos = self._get_models(iid, creds)
                        total_stock_acc = 0
                        for m in modelos:
                            mid = str(m.get("model_id"))
                            mid_int = int(m.get("model_id", 0))

                            # Prioriza "Discount Promotions" e ignora "Whole Sale"
                            matches = [
                                p
                                for p in promos_list
                                if str(p.get("model_id", 0)) == str(mid_int)
                                and p.get("promotion_type") != "Whole Sale"
                            ]
                            promo_v = next(
                                (
                                    p
                                    for p in matches
                                    if p.get("promotion_type") == "Discount Promotions"
                                ),
                                matches[0] if matches else None,
                            )
                            p_info = m.get("price_info", [{}])[0]
                            p_original = p_info.get("original_price", 0.0)
                            p_atual = p_info.get("current_price", 0.0)

                            prod = Produtos.query.filter_by(
                                shopee_item_id=iid, shopee_model_id=mid
                            ).first()
                            if not prod:
                                prod = Produtos(
                                    anuncio_id=anuncio.id,
                                    shopee_item_id=iid,
                                    shopee_model_id=mid,
                                )
                                db.session.add(prod)

                            prod.nome_variacao = m.get("model_name")
                            prod.sku = m.get("model_sku")
                            prod.ean = m.get("gtin_code") or m.get(
                                "ean"
                            )  # Captura o EAN/GTIN da variação
                            prod.situacao = info.get(
                                "item_status"
                            )  # Status do anúncio pai reflete nas variações

                            # Captura estoque da variação específica
                            m_stock_v2 = m.get("stock_info_v2", {})
                            prod.estoque = m_stock_v2.get("summary_info", {}).get(
                                "total_available_stock", 0
                            )
                            total_stock_acc += prod.estoque

                            prod.preco_base = p_original

                            if promo_v:
                                v_promo = promo_v.get("promotion_price_info", [{}])[
                                    0
                                ].get("promotion_price")
                                prod.preco_promocional = (
                                    v_promo
                                    if v_promo
                                    else (p_atual if p_atual != p_original else None)
                                )
                                prod.promotion_id = str(
                                    promo_v.get("promotion_id")
                                    or promo_v.get("discount_id")
                                )
                            elif p_atual != p_original and p_atual > 0:
                                prod.preco_promocional = p_atual
                                prod.promotion_id = "PROMO_DETECTADA"
                            else:
                                prod.preco_promocional = None
                                prod.promotion_id = None

                            # Limpeza de IDs legados se encontrados
                            if prod.promotion_id == "1":
                                prod.promotion_id = None

                            prod.updated_at = agora

                        # Atualiza o estoque total do anúncio com a soma das variações
                        anuncio.estoque_total = total_stock_acc
                        anuncio.updated_at = agora

                        # (Removido daqui pois agora é proativo no início do bloco has_model)
                        pass
                    else:
                        p_info = info.get("price_info", [{}])[0]
                        p_original = p_info.get("original_price", 0.0)
                        p_atual = p_info.get("current_price", 0.0)
                        # Prioriza "Discount Promotions" e ignora "Whole Sale"
                        valid_promos = [
                            p
                            for p in promos_list
                            if p.get("promotion_type") != "Whole Sale"
                        ]
                        promo_v = next(
                            (
                                p
                                for p in valid_promos
                                if p.get("promotion_type") == "Discount Promotions"
                            ),
                            valid_promos[0] if valid_promos else None,
                        )

                        prod = Produtos.query.filter_by(
                            shopee_item_id=iid, shopee_model_id="0"
                        ).first()
                        if not prod:
                            prod = Produtos(
                                anuncio_id=anuncio.id,
                                shopee_item_id=iid,
                                shopee_model_id="0",
                            )
                            db.session.add(prod)

                        prod.sku = anuncio.sku_pai
                        prod.ean = info.get("gtin_code") or info.get(
                            "ean"
                        )  # Captura o EAN/GTIN do anúncio simples
                        prod.situacao = info.get("item_status")

                        # Captura estoque (item simples)
                        s_stock_v2 = info.get("stock_info_v2", {})
                        prod.estoque = s_stock_v2.get("summary_info", {}).get(
                            "total_available_stock", 0
                        )
                        # No item simples, o estoque total do anúncio é o estoque do produto "0"
                        anuncio.estoque_total = prod.estoque

                        prod.preco_base = p_original
                        if promo_v:
                            v_promo = (
                                promo_v.get("promotion_price_info", [{}])[0].get(
                                    "promotion_price"
                                )
                                if promo_v.get("promotion_price_info")
                                else p_atual
                            )
                            prod.preco_promocional = v_promo
                            prod.promotion_id = str(
                                promo_v.get("promotion_id")
                                or promo_v.get("discount_id")
                            )
                        else:
                            prod.preco_promocional = None
                            prod.promotion_id = None

                        # Limpeza de IDs legados
                        if prod.promotion_id == "1":
                            prod.promotion_id = None

                        prod.updated_at = agora
                        anuncio.updated_at = agora

                    stats["sucessos"] += 1
                except SyncCancelledException as e_cancel:
                    raise e_cancel
                except Exception as e_item:
                    print(f"[AVISO] Erro ao processar item {iid} no batch: {e_item}")
                    stats["erros"] += 1
                    # Registrar erro de processamento individual no histórico
                    self._log_and_save_update(
                        item_id=iid,
                        model_id="0",
                        nome=info.get("item_name", "Desconhecido"),
                        p_antigo=0.0,
                        p_novo=0.0,
                        status="erro",
                        msg=f"Erro no batch sync: {str(e_item)}",
                        sku=info.get("item_sku", ""),
                    )

            db.session.commit()

            # Verificar desbloqueios após o commit para garantir que os dados estão atualizados
            try:
                self.verificar_desbloqueios(item_id_list)
            except:
                pass

            return stats
        except SyncCancelledException as e_cancel:
            db.session.rollback()
            raise e_cancel
        except Exception as e_batch:
            db.session.rollback()
            print(f"[ERRO] Erro critico no sync_batch_from_shopee: {e_batch}")
            return {"sucessos": 0, "erros": len(item_id_list)}

    def get_sync_progress(self):
        """Retorna o estado atual da sincronização lendo do Redis."""
        try:
            from config.redis_config import redis_conn
            import json

            data = redis_conn.get("shopee_sync_status")
            if data:
                return json.loads(data)
        except:
            pass
        return {
            "is_running": False,
            "sucessos": 0,
            "erros": 0,
            "total": 0,
            "atual": 0,
            "mensagem": "Inativo",
        }

    def _update_sync_status(self, **kwargs):
        """Atualiza o status no Redis para que todos os processos vejam."""
        try:
            from config.redis_config import redis_conn
            import json

            current = self.get_sync_progress()
            current.update(kwargs)
            redis_conn.set(
                "shopee_sync_status", json.dumps(current), ex=3600
            )  # Expira em 1h
        except Exception as e:
            print(f"Erro ao atualizar status no Redis: {e}")

    def get_item_ids(self, creds):
        """Busca todos os IDs de itens da loja (NORMAL e UNLIST)."""
        from config.redis_config import redis_conn

        path = "/api/v2/product/get_item_list"
        item_ids = []

        # Filtros de status para sincronização total
        statuses = ["NORMAL", "UNLIST"]

        for status in statuses:
            offset = 0
            has_next_page = True

            while has_next_page:
                # Verificar cancelamento via Redis
                cancel_signal = redis_conn.get("shopee_sync_cancel")
                if cancel_signal:
                    print(
                        "--- [SYNC] Cancelamento detectado durante busca de IDs. Interrompendo... ---"
                    )
                    raise SyncCancelledException("Sincronização cancelada pelo usuário.")

                params = {
                    "offset": offset,
                    "page_size": 100,
                    "item_status": status,
                }

                print(f"--- [DEBUG] Buscando {status} (Offset: {offset})... ---")
                resp_json, code = self._shopee_request(path, creds, params=params)

                if code != 200:
                    print(
                        f"[AVISO] Erro ao buscar IDs (Status {status}, Offset {offset}): {code} - Resposta: {resp_json}"
                    )
                    break

                response = resp_json.get("response", {})
                items = response.get("item", [])

                print(
                    f"--- [DEBUG] Shopee retornou {len(items)} itens para {status} ---"
                )

                if not items:
                    break

                for item in items:
                    item_ids.append(item["item_id"])

                # Atualizar progresso na tela em tempo real durante a coleta
                self._update_sync_status(
                    is_running=True,
                    mensagem=f"Coletando anúncios na Shopee: {len(item_ids)}...",
                )

                has_next_page = response.get("has_next_page", False)
                if has_next_page:
                    offset += 100
                    print(
                        f"--- [DEBUG] Indo para próxima página (Novo Offset: {offset}) ---"
                    )
                else:
                    print(f"--- [DEBUG] Fim das páginas para {status} ---")
                    break

        print(f"--- [DEBUG] Total final de IDs coletados: {len(item_ids)} ---")
        return item_ids

    def cancelar_sincronizacao(self):
        """Solicita o cancelamento da sincronização em andamento via Redis."""
        from config.redis_config import redis_conn

        redis_conn.set("shopee_sync_cancel", "true", ex=600)  # Flag dura 10 min
        self.cancel_requested = True
        return {"status": "sucesso", "mensagem": "Cancelamento solicitado."}

    def verificar_desbloqueios(self, item_id_list):
        """Verifica se itens saíram do estado de bloqueio de 15 dias."""
        try:
            from model.shopeeModel import Produtos, Configuracoes

            config = Configuracoes.query.first()
            dias_espera = config.dias_espera_simples if config else 15
            agora = self._get_brasilia_time()

            for iid in item_id_list:
                prods = Produtos.query.filter_by(shopee_item_id=str(iid)).all()
                for p in prods:
                    if p.preco_modificado_em:
                        dias_passados = (agora - p.preco_modificado_em).days
                        # Se o bloqueio acabou nos últimos 2 dias e ainda não notificamos
                        if (
                            dias_passados >= dias_espera
                            and dias_passados <= dias_espera + 1
                            and not p.notificado_desbloqueio
                        ):
                            self._criar_notificacao(
                                tipo="desbloqueio",
                                titulo=p.anuncio.nome if p.anuncio else p.nome_variacao,
                                mensagem=f"Produto Desbloqueado e Pronto para ser Adicionado em Promoção.",
                                item_id=p.shopee_item_id,
                                model_id=p.shopee_model_id,
                                sku=p.sku,
                            )
                            p.notificado_desbloqueio = True
                            db.session.add(p)
                        elif dias_passados < dias_espera:
                            # Opcional: Logar bloqueio se for detectado agora
                            pass
            db.session.commit()
        except Exception as e:
            print(f"?? Erro ao verificar desbloqueios: {e}")

    def verificar_todos_desbloqueios(self):
        """Varre todos os produtos com trava e gera notificações para os liberados."""
        try:
            from model.shopeeModel import Produtos

            # Busca IDs únicos que possuem trava de data
            all_items = (
                Produtos.query.with_entities(Produtos.shopee_item_id)
                .filter(Produtos.preco_modificado_em.isnot(None))
                .distinct()
                .all()
            )
            item_ids = [i[0] for i in all_items]
            if item_ids:
                self.verificar_desbloqueios(item_ids)
            return True
        except Exception as e:
            print(f"?? Erro na verificação global: {e}")
            return False

    def sync_item_from_shopee(self, item_id):
        """
        Sincroniza um único item. Suporta Shopee Item ID numérico,
        ID virtual temporário (TINY_...) ou SKU do produto.
        """
        try:
            creds, erro = self.tokens.ensure_valid_token(1)
            if erro:
                return {"status": "erro", "mensagem": erro}

            item_id_or_sku = str(item_id).strip()
            is_virtual = item_id_or_sku.startswith("TINY_")
            is_numeric = item_id_or_sku.isdigit()

            sku_alvo = None
            if is_virtual:
                from model.shopeeModel import Anuncios
                anuncio_local = Anuncios.query.filter_by(shopee_item_id=item_id_or_sku).first()
                if anuncio_local and anuncio_local.sku_pai:
                    sku_alvo = anuncio_local.sku_pai
                else:
                    return {"status": "erro", "mensagem": f"ID virtual {item_id_or_sku} não localizado ou sem SKU associado."}
            elif not is_numeric:
                sku_alvo = item_id_or_sku
            else:
                # Se for numérico, verifica se já existe localmente como anúncio.
                # Se não existir, pode ser um SKU puramente numérico.
                from model.shopeeModel import Anuncios
                anuncio_local = Anuncios.query.filter_by(shopee_item_id=item_id_or_sku).first()
                if not anuncio_local:
                    sku_alvo = item_id_or_sku

            real_item_ids = []
            if sku_alvo:
                print(f"[Sync Item] Identificado como SKU/ID Virtual. Buscando item correspondente na Shopee pelo SKU: {sku_alvo}")
                matches = self.buscar_todos_ids_sku(sku_alvo, creds)
                if not matches:
                    return {"status": "erro", "mensagem": f"Nenhum anúncio encontrado na Shopee com o SKU {sku_alvo}."}
                real_item_ids = list(set([str(m["item_id"]) for m in matches]))
            else:
                real_item_ids = [item_id_or_sku]

            res = self.sync_batch_from_shopee(real_item_ids, creds)
            if res.get("sucessos", 0) > 0:
                return {"status": "sucesso", "item_ids": real_item_ids}
            return {"status": "erro", "mensagem": "Falha na sincronização do item."}
        except Exception as e:
            return {"status": "erro", "mensagem": str(e)}

    def update_price(
        self,
        preco_desejado,
        item_id=None,
        sku=None,
        model_id=None,
        user_id=None,
        force=False,
    ):
        creds, erro = self.tokens.ensure_valid_token(1)
        if erro:
            return {"status": "erro", "mensagem": f"Erro de autenticação: {erro}"}, 401

        identificador = str(item_id or sku or "").strip()
        if not identificador:
            return {"status": "erro", "mensagem": "item_id or sku é obrigatório"}, 400

        # Buscar o preço atual antes de alterar para o histórico
        from model.shopeeModel import Produtos

        p_antigo = 0.0
        p_nome = "Produto"
        try:
            prod_local = Produtos.query.filter_by(
                shopee_item_id=str(identificador), shopee_model_id=str(model_id or "0")
            ).first()
            if prod_local:
                p_antigo = (
                    prod_local.preco_promocional
                    if (
                        prod_local.preco_promocional
                        and prod_local.preco_promocional > 0
                    )
                    else prod_local.preco_base
                )
                p_nome = prod_local.nome_variacao or (
                    prod_local.anuncio.nome if prod_local.anuncio else "Produto"
                )
        except:
            pass

        resultado = self._atualizar_na_shopee(
            int(identificador),
            int(model_id or 0),
            float(preco_desejado),
            creds,
            force=force,
            usuario_id=user_id,
        )
        return resultado, 200

    def _log_and_save_update(
        self,
        item_id,
        model_id,
        nome,
        p_antigo,
        p_novo,
        status,
        msg,
        sku=None,
        item_info=None,
        promo_info=None,
        usuario_id=None,
        origem=None,
    ):
        """
        Consolida o registro de histórico e a atualização do banco de dados local.
        """
        try:
            from model.shopeeModel import HistoricoPreco, Produtos, db

            agora_br = self._get_brasilia_time()

            # Auto-detectar usuario_id do contexto Flask se não fornecido
            if usuario_id is None:
                try:
                    # pyrefly: ignore [missing-import]
                    from flask import g

                    if hasattr(g, "current_user") and g.current_user:
                        usuario_id = g.current_user.id
                except Exception:
                    pass

            # 1. Gerar mensagem detalhada se for sucesso
            if status == "sucesso":
                # Se for sucesso, gerar mensagem descritiva detalhada de acordo com a origem
                if msg in [
                    "Atualizado", 
                    "Preço atualizado", 
                    "Promoção atualizada", 
                    "Preço de Promoção Atualizado", 
                    "Anuncio colocado em promoção",
                    "Preço de Promoção Atualizado (Campanha)"
                ]:
                    if origem in ["Promocoes", "Promocoes Edicao"]:
                        campanha_nome = "Desconhecida"
                        if promo_info:
                            promo_id = promo_info.get("promotion_id") or promo_info.get("discount_id")
                            if promo_id:
                                from model.shopeeModel import Promocoes
                                camp = Promocoes.query.filter_by(discount_id=int(promo_id)).first()
                                if camp:
                                    campanha_nome = camp.discount_name
                                else:
                                    campanha_nome = f"ID {promo_id}"
                        
                        percentual = round((1.0 - (float(p_novo) / float(p_antigo))) * 100.0, 1) if float(p_antigo) > 0 else 0.0
                        if origem == "Promocoes":
                            if str(model_id) != "0":
                                msg = f"Colocou a variação '{nome}' na campanha '{campanha_nome}' em promoção com desconto de ({percentual}%)"
                            else:
                                msg = f"Colocou o anúncio na campanha '{campanha_nome}' em promoção com desconto de ({percentual}%)"
                        else: # Promocoes Edicao
                            if str(model_id) != "0":
                                msg = f"Alterou o preço promocional da variação '{nome}' na campanha '{campanha_nome}' com desconto de ({percentual}%)"
                            else:
                                msg = f"Alterou o preço promocional do anúncio na campanha '{campanha_nome}' com desconto de ({percentual}%)"
                    
                    elif origem == "Notificação" or origem == "Alertas":
                        if str(model_id) != "0":
                            msg = f"Alterou o preço da variação '{nome}' pela Notificação"
                        else:
                            msg = f"Alterou o preço do anúncio pela Notificação"
                    
                    else: # Anuncios / Home
                        if str(model_id) != "0":
                            msg = f"Alterou o preço da variação '{nome}'"
                        else:
                            msg = f"Alterou o preço do anúncio"

            # 2. Registrar no Histórico
            novo_log = HistoricoPreco(
                shopee_item_id=str(item_id),
                shopee_model_id=str(model_id),
                nome_produto=nome,
                preco_anterior=float(p_antigo),
                preco_atual=float(p_novo),
                status="sucesso" if status == "sucesso" else "erro",
                mensagem=msg,
                sku=sku,
                origem=origem,
                criado_em=agora_br,
                usuario_id=usuario_id,
            )
            db.session.add(novo_log)

            # 2. Se foi um sucesso, atualizar também o registro do produto no banco local
            if status == "sucesso":
                prod = Produtos.query.filter_by(
                    shopee_item_id=str(item_id), shopee_model_id=str(model_id)
                ).first()

                # Fallback: Se não achou com model_id="0", tenta o primeiro produto do item
                if not prod and str(model_id) == "0":
                    prod = Produtos.query.filter_by(shopee_item_id=str(item_id)).first()
                if prod:
                    # Se tiver info de promoção, atualizar preco_promocional e ID
                    if promo_info:
                        prod.preco_promocional = float(p_novo)
                        prod.promotion_id = str(
                            promo_info.get("promotion_id")
                            or promo_info.get("discount_id")
                        )
                    else:
                        # Se não for promoção, atualiza o preco_base e limpa promocional
                        prod.preco_base = float(p_novo)
                        prod.preco_promocional = None
                        prod.promotion_id = None
                        prod.preco_modificado_em = (
                            agora_br  # Atualiza a trava SOMENTE para preço base
                        )
                        prod.notificado_desbloqueio = (
                            False  # Resetar flag de notificação de desbloqueio
                        )
                        try:
                            dias_espera = int(self._get_wait_time_config() or 0)
                        except:
                            dias_espera = 15

                        if dias_espera <= 0:
                            # Caso não haja trava, já notifica que está pronto para promoção
                            prod.notificado_desbloqueio = True  # Já notificamos aqui
                            self._criar_notificacao(
                                tipo="desbloqueio",
                                titulo=nome,
                                mensagem=f"Preço alterado com sucesso! Este anúncio já está disponível para ser colocado em uma Promoção.",
                                item_id=str(item_id),
                                model_id=str(model_id),
                                sku=sku,
                            )
                        else:
                            # Caso contrário, mantém a lógica de bloqueio por segurança
                            prod.notificado_desbloqueio = (
                                False  # Resetar para o background checker pegar depois
                            )
                            self._criar_notificacao(
                                tipo="bloqueio",
                                titulo=nome,
                                mensagem=f"Esse anuncio está temporariamente bloqueado por Segurança, aguarde {dias_espera} dias para alterar o preço novamente ou Adicionar em uma Promoção.",
                                item_id=str(item_id),
                                model_id=str(model_id),
                                sku=sku,
                            )

                    prod.updated_at = agora_br
                    # Atualizar o pai também se existir
                    if prod.anuncio:
                        prod.anuncio.updated_at = agora_br

            db.session.commit()
        except Exception as e:
            db.session.rollback()
            # Log de fallback em caso de erro no banco
            error_msg = (
                f"[DB ERROR] Falha ao salvar evento ({item_id}:{model_id}): {str(e)}"
            )
            print(error_msg)

    def _atualizar_na_shopee(
        self,
        item_id,
        model_id,
        preco_novo,
        creds,
        custom_msg=None,
        force_promotion=False,
        origem=None,
        force=False,
        usuario_id=None,
        lista_precos=None,
    ):
        LOG_DIR = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "..", "logs"
        )
        os.makedirs(LOG_DIR, exist_ok=True)
        log_path = os.path.join(LOG_DIR, "shopee_debug.json")

        try:
            # Validação de entrada
            if not item_id:
                return {
                    "status": "erro",
                    "mensagem": f"item_id={item_id} - Valor inválido",
                }
            
            if not lista_precos and preco_novo <= 0:
                return {
                    "status": "erro",
                    "mensagem": f"preco_novo={preco_novo} - Valor inválido",
                }

            # --- PASSO 1: Buscar info do item na Shopee ---
            item_info = self._get_item_base_info(item_id, creds)

            if not item_info:
                return {
                    "status": "erro",
                    "mensagem": f"Item {item_id} não encontrado na Shopee. Verifique o ID.",
                }

            has_model = item_info.get("has_model", False)
            item_name = item_info.get("item_name", f"Item {item_id}")

            sucessos = 0
            erros = []
            detalhes_sucesso = []

            # --- PASSO 2 e 3: Construir alvos_com_preco ---
            alvos_com_preco = []
            
            if lista_precos:
                models = self._get_models(item_id, creds) if has_model else []
                for p_req in lista_precos:
                    req_mid = int(p_req.get("model_id") or 0)
                    req_preco = round(float(p_req.get("preco") or 0), 2)
                    
                    alvo_encontrado = None
                    if has_model:
                        alvo_encontrado = next((m for m in models if int(m.get("model_id", 0)) == req_mid), None)
                    else:
                        alvo_encontrado = {"model_id": 0, "model_name": item_name, "price_info": item_info.get("price_info", [])}
                    
                    if alvo_encontrado:
                        alvos_com_preco.append({"alvo": alvo_encontrado, "mid": req_mid, "preco": req_preco})
                    else:
                        erros.append({"model_id": req_mid, "erro": f"Modelo {req_mid} não encontrado"})
            else:
                preco_novo = round(float(preco_novo), 2)
                alvos = []
                if has_model:
                    models = self._get_models(item_id, creds)
                    if model_id > 0:
                        m_alvo = next((m for m in models if int(m.get("model_id", 0)) == model_id), None)
                        if m_alvo: 
                            alvos.append(m_alvo)
                        else: 
                            return {"status": "erro", "mensagem": f"Modelo {model_id} não encontrado"}
                    else:
                        alvos = models
                else:
                    alvos = [{"model_id": 0, "model_name": item_name, "price_info": item_info.get("price_info", [])}]
                    
                for alvo in alvos:
                    mid_atual = int(alvo.get("model_id") or 0)
                    alvos_com_preco.append({"alvo": alvo, "mid": mid_atual, "preco": preco_novo})

            if not alvos_com_preco:
                return {
                    "status": "erro",
                    "mensagem": f"Nenhum modelo válido para atualizar.",
                    "detalhes": erros,
                }

            # --- PASSO 4: Agrupar por tipo de atualização (Batching) ---
            promo_updates = {} # discount_id -> list of items
            base_updates = []
            force_promo_updates = []
            
            try:
                dias_espera = int(self._get_wait_time_config() or 0)
            except:
                dias_espera = 15

            for item in alvos_com_preco:
                mid_atual = item["mid"]
                promocao = self._get_active_promotion(item_id, mid_atual if mid_atual > 0 else None, creds)
                item["promocao"] = promocao
                
                if promocao:
                    promo_id = promocao.get("promotion_id") or promocao.get("discount_id")
                    if promo_id not in promo_updates:
                        promo_updates[promo_id] = []
                    promo_updates[promo_id].append(item)
                elif force_promotion:
                    force_promo_updates.append(item)
                else:
                    is_locked, _, lock_msg = self.validate_price_lock(item_id, mid_atual)
                    if is_locked and dias_espera > 0 and not force:
                        item["is_locked"] = True
                        item["lock_msg"] = lock_msg
                        
                        item_name_log = item["alvo"].get("model_name") or item_name
                        self._criar_notificacao(
                            tipo="bloqueio",
                            titulo=f"Bloqueio de {dias_espera} Dias",
                            mensagem=f"{item_name_log} está Bloqueado Temporariamente por Segurança. Aguarde {dias_espera} dias para nova alteração.",
                            item_id=str(item_id),
                            model_id=str(mid_atual),
                        )
                    else:
                        item["is_locked"] = False
                        base_updates.append(item)

            # --- PASSO 5: Executar atualizações em lote ---
            
            # 5.1 Atualizações de Preço Base
            if base_updates:
                price_list = []
                for item in base_updates:
                    p_item = {"original_price": float(item["preco"])}
                    if has_model and item["mid"] > 0:
                        p_item["model_id"] = int(item["mid"])
                    price_list.append(p_item)
                    
                resp, code = self.atualizar_preco_base_lote(item_id, price_list, creds)
                
                if code == 200 and not resp.get("error"):
                    for item in base_updates:
                        item["sucesso"] = True
                        item["resp"] = resp
                else:
                    erro_msg = self._extrair_erro(resp)
                    for item in base_updates:
                        item["sucesso"] = False
                        item["erro_msg"] = erro_msg
                        item["resp"] = resp

            # 5.2 Atualizações de Promoção (por discount_id)
            for discount_id, items in promo_updates.items():
                item_list = []
                item_data = {"item_id": int(item_id)}
                
                if has_model:
                    model_list = []
                    for item in items:
                        if item["mid"] > 0:
                            model_list.append({
                                "model_id": int(item["mid"]),
                                "model_promotion_price": float(item["preco"])
                            })
                    if model_list:
                        item_data["model_list"] = model_list
                    else:
                        item_data["item_promotion_price"] = float(items[0]["preco"])
                else:
                    item_data["item_promotion_price"] = float(items[0]["preco"])
                    
                item_list.append(item_data)
                resp, code = self.atualizar_promocao_lote(discount_id, item_list, creds)
                
                if code == 200 and not resp.get("error"):
                    for item in items:
                        item["sucesso"] = True
                        item["resp"] = resp
                else:
                    erro_msg = self._extrair_erro(resp)
                    for item in items:
                        item["sucesso"] = False
                        item["erro_msg"] = erro_msg
                        item["resp"] = resp

            # 5.3 Force Promotion
            if force_promo_updates:
                ongoing_promo_id = self._find_any_ongoing_discount(creds)
                if ongoing_promo_id:
                    # Checar limite (1000)
                    from model.shopeeModel import Produtos, db
                    count_promo = (
                        db.session.query(Produtos.shopee_item_id)
                        .filter_by(promotion_id=str(ongoing_promo_id))
                        .distinct()
                        .count()
                    )
                    if count_promo >= 995:
                        for item in force_promo_updates:
                            item["sucesso"] = False
                            item["erro_msg"] = f"A campanha automática atingiu o limite de anúncios ({count_promo}/1000). Remova itens ou crie uma nova na Shopee."
                            item["resp"] = {}
                    else:
                        item_data = {"item_id": int(item_id)}
                        if has_model:
                            model_list = []
                            for item in force_promo_updates:
                                if item["mid"] > 0:
                                    model_list.append({
                                        "model_id": int(item["mid"]),
                                        "model_promotion_price": float(item["preco"])
                                    })
                            if model_list:
                                item_data["model_list"] = model_list
                            else:
                                item_data["item_promotion_price"] = float(force_promo_updates[0]["preco"])
                        else:
                            item_data["item_promotion_price"] = float(force_promo_updates[0]["preco"])
                            
                        resp, code = self.add_discount_item(creds, ongoing_promo_id, [item_data], log_msg="Anuncio colocado em promoção", origem=origem)
                        
                        if code == 200 and not resp.get("error"):
                            for item in force_promo_updates:
                                item["sucesso"] = True
                                item["resp"] = resp
                                item["promocao"] = {"promotion_id": ongoing_promo_id, "promotion_type": "Discount Promotions"}
                        else:
                            erro_msg = self._extrair_erro(resp)
                            for item in force_promo_updates:
                                item["sucesso"] = False
                                item["erro_msg"] = erro_msg
                                item["resp"] = resp
                else:
                    for item in force_promo_updates:
                        item["sucesso"] = False
                        item["erro_msg"] = "Nenhuma promoção ativa (Ongoing) encontrada na loja para vincular o item."
                        item["resp"] = {}

            # --- PASSO 6: Salvar logs e atualizar BD local ---
            for item in alvos_com_preco:
                mid_atual = item["mid"]
                alvo = item["alvo"]
                
                p_atual = 0.0
                try:
                    p_atual = alvo["price_info"][0]["current_price"]
                except:
                    pass
                    
                if item.get("is_locked"):
                    erros.append({"model_id": mid_atual, "erro": item.get("lock_msg")})
                    continue
                    
                if item.get("sucesso"):
                    sucessos += 1
                    
                    log_msg = custom_msg
                    if not log_msg:
                        if force_promotion:
                            log_msg = "Anuncio colocado em promoção"
                        elif item.get("promocao"):
                            log_msg = "Preço de Promoção Atualizado"
                        else:
                            log_msg = "Atualizado"
                            
                    self._log_and_save_update(
                        item_id,
                        mid_atual,
                        item_name,
                        p_atual,
                        item["preco"],
                        "sucesso",
                        log_msg,
                        sku=(alvo.get("model_sku") or item_info.get("item_sku")),
                        promo_info=item.get("promocao"),
                        origem=origem,
                        usuario_id=usuario_id,
                    )
                    
                    detalhes_sucesso.append({
                        "model_id": mid_atual,
                        "nome": alvo.get("model_name") or item_name,
                        "preco_anterior": p_atual,
                        "tipo": "promocional" if item.get("promocao") else "base",
                    })
                else:
                    erro_msg = item.get("erro_msg", "Erro desconhecido")
                    erros.append({"model_id": mid_atual, "erro": erro_msg})
                    
                    self._log_and_save_update(
                        item_id,
                        mid_atual,
                        item_name,
                        p_atual,
                        item["preco"],
                        "erro",
                        erro_msg,
                        sku=(alvo.get("model_sku") or item_info.get("item_sku")),
                        origem=origem,
                        usuario_id=usuario_id,
                    )

            if sucessos == 0 and alvos_com_preco:
                return {
                    "status": "erro",
                    "mensagem": f"Falha ao atualizar na Shopee: {erros[0]['erro'] if erros else 'Erro desconhecido'}",
                    "detalhes": erros,
                }

            try:
                from model.shopeeModel import NotificacaoSistema, db
                NotificacaoSistema.query.filter_by(
                    shopee_item_id=str(item_id), tipo="desbloqueio", lida=False
                ).update({"lida": True})
                db.session.commit()
            except Exception as e:
                print(f"Erro ao limpar notificações automáticas: {e}")
                from model.shopeeModel import db
                db.session.rollback()

            mensagem = f"Atualizado com sucesso {sucessos} de {len(alvos_com_preco)} alvos."

            return {
                "status": "sucesso",
                "mensagem": mensagem,
                "detalhes": {
                    "item_id": item_id,
                    "sucessos": sucessos,
                    "total": len(alvos_com_preco),
                    "itens_atualizados": detalhes_sucesso,
                    "erros": erros,
                },
            }

        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            try:
                error_log = os.path.join(LOG_DIR, "shopee_error.log")
                with open(error_log, "a", encoding="utf-8") as f:
                    f.write(f"\n--- {datetime.now()} ---\n{tb}\n")
            except:
                pass

            return {"status": "erro", "mensagem": f"Erro interno: {str(e)}"}


    def alterar_precos_lote(
        self,
        item_id,
        lista_precos,
        user_id=None,
        custom_msg=None,
        force_promotion=False,
        origem=None,
        force=False,
    ):
        """Versão lote - para importação de planilha ou Modal com variações."""
        creds, erro = self.tokens.ensure_valid_token(1)
        if erro:
            return {"status": "erro", "mensagem": f"Erro de autenticação: {erro}"}

        res = self._atualizar_na_shopee(
            item_id=int(item_id),
            model_id=0,
            preco_novo=0,
            creds=creds,
            custom_msg=custom_msg,
            force_promotion=force_promotion,
            origem=origem,
            force=force,
            usuario_id=user_id,
            lista_precos=lista_precos
        )
        return res

    def atualizar_preco_base_lote(self, item_id, price_list, creds):
        """Metodo de baixo nível para chamar a API com múltiplos preços de uma vez."""
        path = "/api/v2/product/update_price"
        payload = {"item_id": int(item_id), "price_list": price_list}
        return self._shopee_request(path, creds, method="POST", json_data=payload)

    def atualizar_promocao_lote(self, discount_id, item_list, creds):
        """Metodo de baixo nível para atualizar vários produtos em um desconto."""
        path = "/api/v2/discount/update_discount_item"
        payload = {"discount_id": int(discount_id), "item_list": item_list}
        return self._shopee_request(path, creds, method="POST", json_data=payload)

    def _criar_notificacao(
        self, tipo, titulo, mensagem, item_id=None, model_id=None, sku=None
    ):
        """Cria uma notificação no banco e emite via WebSocket."""
        try:
            from model.shopeeModel import NotificacaoSistema, db

            nova = NotificacaoSistema(
                tipo=tipo,
                titulo=titulo,
                mensagem=mensagem,
                shopee_item_id=str(item_id) if item_id else None,
                shopee_model_id=str(model_id) if model_id else None,
                sku=sku,
            )
            db.session.add(nova)
            db.session.commit()

            # Emitir via WebSocket para o frontend (no-op se rodando no Worker)
            self._safe_emit("nova_notificacao", nova.to_dict())
            return True
        except Exception as e:
            print(f"?? Erro ao criar notificação: {e}")
            return False

    def _registrar_no_banco(
        self,
        item_id,
        model_id,
        nome,
        preco_ant,
        preco_nv,
        item_info,
        promo_info,
        user_id=None,
    ):
        """Grava no histórico e garante que Pai/Filho existam no banco sem duplicidade."""
        try:
            agora = self._get_brasilia_time()
            from model.shopeeModel import Anuncios, Produtos

            # 1. Garantir que o Anúncio (Pai) existe
            anuncio = Anuncios.query.filter_by(shopee_item_id=str(item_id)).first()
            if not anuncio:
                anuncio = Anuncios(
                    shopee_item_id=str(item_id),
                    nome=nome or (item_info.get("item_name") if item_info else "Item"),
                    sku_pai=item_info.get("item_sku") if item_info else None,
                )
                db.session.add(anuncio)
                db.session.flush()

            # 2. Histórico de Preços
            historico = HistoricoPreco(
                shopee_item_id=str(item_id),
                shopee_model_id=str(model_id or "0"),
                nome_produto=nome,
                preco_anterior=float(preco_ant),
                preco_atual=float(preco_nv),
                status="sucesso",
                mensagem="Atualização realizada",
                usuario_id=user_id,
                criado_em=agora,
                sku=item_info.get("item_sku") if item_info else None,
            )
            db.session.add(historico)

            # 3. Garantir que o Produto existe e atualizar
            mid_str = str(model_id or "0")
            is_promo = promo_info is not None and (
                promo_info.get("promotion_id") or promo_info.get("discount_id")
            )

            produto = Produtos.query.filter_by(
                shopee_item_id=str(item_id), shopee_model_id=mid_str
            ).first()

            if not produto:
                produto = Produtos(
                    anuncio_id=anuncio.id,
                    shopee_item_id=str(item_id),
                    shopee_model_id=mid_str,
                    preco_base=preco_nv if not is_promo else 0.0,
                )
                db.session.add(produto)

            if is_promo:
                produto.preco_promocional = preco_nv
                produto.promotion_id = str(
                    promo_info.get("promotion_id") or promo_info.get("discount_id")
                )
            else:
                produto.preco_base = preco_nv
                produto.promotion_id = None
                produto.preco_promocional = None

            # Sincronizar nome se estiver vazio
            if item_info and (not anuncio.nome or anuncio.nome.isdigit()):
                anuncio.nome = item_info.get("item_name") or anuncio.nome

            produto.updated_at = agora
            produto.preco_modificado_em = agora  # Atualiza a trava de 15 dias
            anuncio.updated_at = agora
            db.session.commit()
        except Exception as e:
            print(f"[AVISO] Error in _registrar_no_banco: {e}")
            db.session.rollback()

    # --- LÓGICA DE BUSCA ROBUSTA (v3) ---
    def buscar_todos_ids_sku(self, sku_alvo, creds):
        """Busca todas as variações/anúncios que usam o SKU."""
        all_matches = []
        unique_pairs = set()

        sku_norm = str(sku_alvo).strip().upper()
        prefixo = self._extrair_prefixo_sku(sku_norm)

        # 1. IDs Candidatos via API
        ids_candidatos = set()

        # Busca por variações comuns
        for s in [sku_norm, f"{sku_norm}-C1", f"{sku_norm}-C1-1", prefixo]:
            ids = self._call_search_item_ids(creds, item_sku=s)
            if ids:
                ids_candidatos.update(ids)

        # Busca por palavra-chave se ainda não achou nada
        if not ids_candidatos:
            ids = self._call_search_item_ids(creds, item_name=prefixo)
            if ids:
                ids_candidatos.update(ids)

        # 2. Varredura Local
        for iid in ids_candidatos:
            info = self._get_item_base_info(iid, creds)
            if not info:
                continue

            name = info.get("item_name", "Sem Nome")
            sku_pai = (info.get("item_sku") or "").strip().upper()

            # Confere Pai
            if sku_pai == sku_norm:
                if (iid, 0) not in unique_pairs:
                    unique_pairs.add((iid, 0))
                    all_matches.append(
                        {"item_id": iid, "model_id": 0, "sku": sku_pai, "name": name}
                    )

            # Confere Modelos
            if info.get("has_model"):
                modelos = self._get_models(iid, creds)
                for m in modelos:
                    m_sku = (m.get("model_sku") or "").strip().upper()
                    if m_sku == sku_norm:
                        if (iid, m["model_id"]) not in unique_pairs:
                            unique_pairs.add((iid, m["model_id"]))
                            all_matches.append(
                                {
                                    "item_id": iid,
                                    "model_id": m["model_id"],
                                    "sku": m_sku,
                                    "name": f"{name} [{m.get('model_name')}]",
                                }
                            )

        return all_matches

    def _extrair_prefixo_sku(self, sku):
        match = re.split(r"-(?:C\d+|V\d+|[A-Z]\d+)", sku, flags=re.IGNORECASE)
        if match and len(match[0]) > 3:
            return match[0]
        return sku.split("-")[0]

    # --- MÉTODOS DE SUPORTE (API) ---
    def _call_search_item_ids(self, creds, item_sku=None, item_name=None):
        path = "/api/v2/product/search_item"
        timestamp = int(time.time())
        string_base = f"{creds['partner_id']}{path}{timestamp}{creds['access_token']}{creds['shop_id']}"
        sign = hmac.new(
            creds["partner_key"].encode(), string_base.encode(), hashlib.sha256
        ).hexdigest()

        params = {
            "partner_id": int(creds["partner_id"]),
            "shop_id": int(creds["shop_id"]),
            "timestamp": timestamp,
            "access_token": creds["access_token"],
            "sign": sign,
            "page_size": 50,
        }
        if item_sku:
            params["item_sku"] = item_sku
        if item_name:
            params["item_name"] = item_name

        try:
            resp = requests.get(f"{self.host_base}{path}", params=params).json()
            return resp.get("response", {}).get("item_id_list", [])
        except:
            return []

    def _get_item_base_info(self, item_id, creds):
        res = self._get_item_base_info_batch([item_id], creds)
        return res[0] if res else None

    def _get_item_base_info_batch(self, item_id_list, creds):
        """Busca informações de múltiplos itens de uma vez (max 50)."""
        path = "/api/v2/product/get_item_base_info"
        ids_str = ",".join([str(i) for i in item_id_list])
        params = {"item_id_list": ids_str}

        resp_json, code = self._shopee_request(path, creds, params=params)
        if code == 200:
            return resp_json.get("response", {}).get("item_list", [])
        return []

    def _get_models(self, item_id, creds):
        """Busca variações (models) de um item."""
        path = "/api/v2/product/get_model_list"
        params = {"item_id": int(item_id)}
        resp_json, code = self._shopee_request(path, creds, params=params)
        if code == 200:
            return resp_json.get("response", {}).get("model", [])
        return []

    def _get_active_promotion(self, item_id, model_id, creds):
        """
        Identifica se há uma promoção ativa para o item/modelo.
        Prioriza 'Discount Promotions', pois é o único tipo editável via API de Descontos.
        """
        res = self._get_active_promotion_batch([item_id], creds)
        if not res:
            return None

        promos = res[0].get("promotion", [])

        # Filtrar promoções válidas para este modelo/item
        matches = []
        for p in promos:
            if p.get("promotion_id") or p.get("discount_id"):
                # Ignorar promoções de atacado (Whole Sale) que não são gerenciáveis via API de Descontos
                if p.get("promotion_type") == "Whole Sale":
                    continue

                if model_id:
                    if str(p.get("model_id", 0)) == str(model_id):
                        matches.append(p)
                else:
                    matches.append(p)

        if not matches:
            return None

        # 1. Tentar encontrar "Discount Promotions" (Prioridade Máxima)
        for p in matches:
            if p.get("promotion_type") == "Discount Promotions":
                return p

        # 2. Se não houver, retorna a primeira encontrada (Fallback)
        return matches[0]

    def _get_active_promotion_batch(self, item_id_list, creds):
        """Busca promoções para um lote de itens."""
        path = "/api/v2/product/get_item_promotion"
        ids_str = ",".join([str(i) for i in item_id_list])
        params = {"item_id_list": ids_str}

        resp_json, code = self._shopee_request(path, creds, params=params)
        if code == 200:
            return resp_json.get("response", {}).get("success_list", [])
        return []

    def _update_promotion_price(
        self, discount_id, item_id, price, model_id, creds, has_model=True
    ):
        """Atualiza o preço de uma promoção existente."""
        path = "/api/v2/discount/update_discount_item"
        item_data = {"item_id": int(item_id)}
        if has_model and model_id and int(model_id) > 0:
            item_data["model_list"] = [
                {"model_id": int(model_id), "model_promotion_price": float(price)}
            ]
        else:
            item_data["item_promotion_price"] = float(price)

        payload = {"discount_id": int(discount_id), "item_list": [item_data]}
        return self._shopee_request(path, creds, method="POST", json_data=payload)

    def _update_base_price(self, item_id, price, model_id, creds, has_model=True):
        """Atualiza o preço base (sem promoção) de um item."""
        path = "/api/v2/product/update_price"
        price_item = {"original_price": float(price)}
        if has_model and model_id and int(model_id) > 0:
            price_item["model_id"] = int(model_id)

        payload = {"item_id": int(item_id), "price_list": [price_item]}
        return self._shopee_request(path, creds, method="POST", json_data=payload)

    def process_spreadsheet(self, df):
        """
        Lógica de negócio para processar o DataFrame da planilha e salvar no banco local.
        """
        from model.shopeeModel import Anuncios, Produtos, db

        stats = {"sucesso": 0, "erro": 0}

        # Identificação de colunas (Lógica robusta movida das rotas)
        cols = [str(c).strip().lower() for c in df.columns]
        sku_key = next((c for i, c in enumerate(df.columns) if "sku" in cols[i]), None)
        price_key = next(
            (
                c
                for i, c in enumerate(df.columns)
                if "preco" in cols[i] or "preço" in cols[i] or "price" in cols[i]
            ),
            None,
        )
        id_item_key = next(
            (
                c
                for i, c in enumerate(df.columns)
                if "product_id" in cols[i] or "itemid" in cols[i]
            ),
            None,
        )
        id_model_key = next(
            (
                c
                for i, c in enumerate(df.columns)
                if "variation_id" in cols[i] or "model_id" in cols[i]
            ),
            None,
        )

        if not sku_key or not price_key:
            return None, "Colunas SKU ou Preço não identificadas."

        for _, row in df.iterrows():
            try:
                iid = (
                    str(row.get(id_item_key, "")).strip().replace(".0", "")
                    if id_item_key
                    else ""
                )
                mid = (
                    str(row.get(id_model_key, "0")).strip().replace(".0", "")
                    if id_model_key
                    else "0"
                )
                preco_nv = float(row[price_key])
                sku_raw = str(row.get(sku_key, "")).strip()

                if not iid.isdigit():
                    continue

                anuncio = Anuncios.query.filter_by(shopee_item_id=iid).first()
                if not anuncio:
                    anuncio = Anuncios(
                        shopee_item_id=iid, nome=f"Item {iid}", sku_pai=""
                    )
                    db.session.add(anuncio)
                    db.session.flush()

                produto = Produtos.query.filter_by(
                    shopee_item_id=iid, shopee_model_id=mid
                ).first()
                if not produto:
                    produto = Produtos(
                        anuncio_id=anuncio.id,
                        shopee_item_id=iid,
                        shopee_model_id=mid,
                        sku=sku_raw,
                        preco_base=preco_nv,
                    )
                    db.session.add(produto)
                else:
                    if produto.preco_promocional and produto.preco_promocional > 0:
                        produto.preco_promocional = preco_nv
                    else:
                        produto.preco_base = preco_nv
                    produto.sku = sku_raw
                    produto.updated_at = self._get_brasilia_time()
                    produto.preco_modificado_em = (
                        produto.updated_at
                    )  # Atualiza a trava de 15 dias
                    if produto.anuncio:
                        produto.anuncio.updated_at = produto.updated_at

                stats["sucesso"] += 1
            except:
                stats["erro"] += 1

        db.session.commit()
        return stats, None

    def validar_faixa_segura(self, preco_desejado, preco_atual):
        # Removendo a trava de 30% a pedido do usuário para que o preço mude exatamente para o desejado.
        return float(preco_desejado)

    # --- Módulo de Promoções (V2 Discount) ---
    def _find_any_ongoing_discount(self, creds):
        """Busca o ID da primeira promoção ativa (ongoing) na loja."""
        path = "/api/v2/discount/get_discount_list"
        params = {
            "page_no": 1,
            "page_size": 10,
            "discount_status": "ongoing",
        }
        resp, code = self._shopee_request(path, creds, params=params)
        if code == 200 and not resp.get("error"):
            discounts = resp.get("response", {}).get("discount_list", [])
            if discounts:
                return discounts[0]["discount_id"]
        return None

    def get_shopee_discounts(
        self, creds, status="all", page=1, page_size=100, force_sync=False
    ):
        """Busca lista de promoções (campanhas). Se force_sync=False, busca apenas no Banco de Dados local."""
        from model.shopeeModel import Promocoes, db
        from datetime import datetime
        import pytz

        # 1. Se NÃO for sincronização forçada, busca direto do Banco Local (Fast Load)
        if not force_sync:
            try:
                query = Promocoes.query
                if status and status != "all":
                    query = query.filter_by(status=status)

                # Paginação simples no banco se necessário, mas aqui retornamos tudo por enquanto
                promos = query.order_by(Promocoes.start_time.desc()).all()

                return {
                    "discount_list": [
                        {
                            "discount_id": str(p.discount_id),
                            "discount_name": p.discount_name,
                            "start_time": (
                                int(p.start_time.timestamp()) if p.start_time else 0
                            ),
                            "end_time": (
                                int(p.end_time.timestamp()) if p.end_time else 0
                            ),
                            "discount_status": p.status or "unknown",
                            "item_count": db.session.query(Produtos.shopee_item_id)
                            .filter_by(promotion_id=str(p.discount_id))
                            .distinct()
                            .count(),
                        }
                        for p in promos
                    ],
                    "more": False,
                    "source": "database",
                }
            except Exception as e:
                print(f"Erro ao buscar promoções no banco: {str(e)}")
                # Se der erro no banco, tenta o fluxo da API como fallback automático

        # 2. Fluxo de Sincronização Direta com a Shopee (API)
        path = "/api/v2/discount/get_discount_list"

        # Shopee API v2 OBRIGA o parâmetro discount_status (não aceita "all")
        if status == "all":
            statuses_to_fetch = ["ongoing", "upcoming", "expired"]
        else:
            statuses_to_fetch = [status]

        all_discounts = []

        try:
            for st in statuses_to_fetch:
                params = {
                    "page_no": page,
                    "page_size": page_size,
                    "discount_status": st,
                }
                resp, code = self._shopee_request(path, creds, params=params)

                if code == 200 and not resp.get("error"):
                    res = resp.get("response", {})
                    items = res.get("discount_list", [])

                    # Normalizar cada item para incluir discount_status e ID como string
                    for item in items:
                        # Mapear 'status' da Shopee para 'discount_status'
                        st_api = (
                            item.get("status")
                            or item.get("discount_status")
                            or "unknown"
                        )
                        item["discount_status"] = st_api
                        item["discount_id"] = str(item["discount_id"])
                        # Apenas contar itens já salvos localmente (evitar sync pesado aqui)
                        item["item_count"] = (
                            db.session.query(Produtos.shopee_item_id)
                            .filter_by(promotion_id=item["discount_id"])
                            .distinct()
                            .count()
                        )

                    all_discounts.extend(items)

            # Sincronização com o Banco Local
            for d in all_discounts:
                try:
                    did = int(d["discount_id"])
                    nome = d["discount_name"]

                    # Converter timestamp para datetime (Shopee usa UTC)
                    inicio = datetime.fromtimestamp(
                        d.get("start_time", 0), pytz.UTC
                    ).replace(tzinfo=None)
                    fim = datetime.fromtimestamp(
                        d.get("end_time", 0), pytz.UTC
                    ).replace(tzinfo=None)

                    d_status = d.get("discount_status") or "unknown"

                    promo = Promocoes.query.filter_by(discount_id=did).first()
                    if promo:
                        promo.discount_name = nome
                        promo.start_time = inicio
                        promo.end_time = fim
                        promo.status = d_status
                        promo.updated_at = datetime.utcnow()
                    else:
                        promo = Promocoes(
                            discount_id=did,
                            discount_name=nome,
                            start_time=inicio,
                            end_time=fim,
                            status=d_status,
                        )
                        db.session.add(promo)
                except Exception as e:
                    print(
                        f"Erro ao persistir campanha {d.get('discount_id')}: {str(e)}"
                    )

            if all_discounts:
                db.session.commit()

            return {"discount_list": all_discounts, "more": False, "source": "api"}

        except Exception as e:
            print(f"[SYNC] Fallback get_shopee_discounts: {str(e)}")
            try:
                query = Promocoes.query
                if status and status != "all":
                    query = query.filter_by(status=status)

                promos = query.order_by(Promocoes.start_time.desc()).all()

                return {
                    "discount_list": [
                        {
                            "discount_id": str(p.discount_id),
                            "discount_name": p.discount_name,
                            "start_time": (
                                int(p.start_time.timestamp()) if p.start_time else 0
                            ),
                            "end_time": (
                                int(p.end_time.timestamp()) if p.end_time else 0
                            ),
                            "discount_status": p.status or "unknown",
                        }
                        for p in promos
                    ],
                    "more": False,
                }
            except Exception as ef:
                print(f"Falha crítica no fallback: {str(ef)}")
                return {"discount_list": [], "more": False}

    def sync_all_active_campaigns(self, creds):
        """Sincroniza a lista de campanhas e, para cada uma ativa/agendada, sincroniza os itens detalhadamente."""
        try:
            # 1. Atualiza a lista de campanhas (Ongoing e Upcoming)
            print("--- Iniciando Sincronização Global de Campanhas ---")
            res_list = self.get_shopee_discounts(creds, status="all", force_sync=True)
            if res_list.get("status") == "error":
                return res_list

            discounts = res_list.get("discount_list", [])
            active_statuses = ["ongoing", "upcoming"]

            sync_count = 0
            for d in discounts:
                status = d.get("discount_status")
                did = d.get("discount_id")

                if status in active_statuses:
                    print(
                        f"-> Sincronizando itens da campanha: {d.get('discount_name')} ({did})"
                    )
                    # Chama o sync detalhado de itens (já com lógica de no_autoflush e retry)
                    self.get_discount_item_list(creds, did, page=1)
                    sync_count += 1

            print(
                f"--- Sincronização Global Concluída: {sync_count} campanhas processadas ---"
            )
            return {"status": "sucesso", "campanhas_processadas": sync_count}

        except Exception as e:
            import traceback

            traceback.print_exc()
            return {"status": "erro", "mensagem": str(e)}

    def add_shopee_discount(self, creds, name, start_time, end_time):
        """Cria uma nova campanha de promoção e salva no Banco Local."""
        from model.shopeeModel import Promocoes, db
        from datetime import datetime
        import pytz

        path = "/api/v2/discount/add_discount"
        payload = {
            "discount_name": name,
            "start_time": int(start_time),
            "end_time": int(end_time),
        }
        resp, code = self._shopee_request(path, creds, method="POST", json_data=payload)

        if code == 200:
            did = resp.get("response", {}).get("discount_id")
            if did:
                promo = Promocoes(
                    discount_id=did,
                    discount_name=name,
                    start_time=datetime.fromtimestamp(
                        int(start_time), pytz.UTC
                    ).replace(tzinfo=None),
                    end_time=datetime.fromtimestamp(int(end_time), pytz.UTC).replace(
                        tzinfo=None
                    ),
                    status="upcoming",  # Por padrão a Shopee cria com delay
                )
                db.session.add(promo)
                db.session.commit()
                # Converter ID no response para string
                resp["response"]["discount_id"] = str(did)

        return resp, code

    def get_shopee_discount_detail(self, discount_id):
        """Busca os detalhes de uma campanha específica no Banco Local."""
        from model.shopeeModel import Promocoes

        promo = Promocoes.query.filter_by(discount_id=int(discount_id)).first()
        if promo:
            return {
                "discount_id": str(promo.discount_id),
                "discount_name": promo.discount_name,
                "start_time": (
                    int(promo.start_time.timestamp()) if promo.start_time else 0
                ),
                "end_time": int(promo.end_time.timestamp()) if promo.end_time else 0,
                "discount_status": promo.status,
            }
        return None

    def delete_shopee_discount(self, creds, discount_id):
        """Exclui uma campanha agendada (Upcoming)."""
        path = "/api/v2/discount/delete_discount"
        payload = {"discount_id": int(discount_id)}
        return self._shopee_request(path, creds, method="POST", json_data=payload)

    def end_shopee_discount(self, creds, discount_id):
        """Encerra uma campanha ativa (Ongoing)."""
        path = "/api/v2/discount/end_discount"
        payload = {"discount_id": int(discount_id)}
        return self._shopee_request(path, creds, method="POST", json_data=payload)

    def get_discount_item_list(
        self, creds, discount_id, page=1, page_size=20, search=None, force_sync=False
    ):
        """Lista os itens vinculados a uma promoção, sincronizando com a Shopee para garantir consistência."""
        from model.shopeeModel import Produtos, Anuncios
        from datetime import datetime
        import time

        # 1. Sincronização Ativa com a Shopee (Sempre executada na primeira página para garantir dados novos)
        # 1. Sincronização Ativa com a Shopee (Apenas se forçar ou se não houver dados locais e for página 1)
        if page == 1 and force_sync:
            try:
                path = "/api/v2/discount/get_discount"
                current_sync_page = 1
                has_next_page = True
                current_item_models = set()
                has_fetched_any = False  # Flag de segurança: só limpa o banco se conseguiu baixar algo

                # SEGURANÇA: Usar no_autoflush para evitar que o SQLAlchemy envie UPDATES pro banco
                # no meio do loop (o que causa locks e deadlocks ao fazer novas queries)
                with db.session.no_autoflush:
                    while has_next_page:
                        params = {
                            "discount_id": int(discount_id),
                            "page_no": current_sync_page,
                            "page_size": 100,
                        }
                        resp, code = self._shopee_request(path, creds, params=params)

                        raw_items = []
                        if code == 200 and "response" in resp:
                            res_data = resp["response"]
                            raw_items = res_data.get("item_list", [])
                            has_next_page = res_data.get("more", False)
                            has_fetched_any = True
                            print(
                                f"DEBUG SYNC: Campanha {discount_id} - Pagina {current_sync_page} retornou {len(raw_items)} itens."
                            )
                        else:
                            print(
                                f"DEBUG SYNC: Campanha {discount_id} - Erro na API (Status {code}): {resp}"
                            )
                            has_next_page = False

                        for item in raw_items:
                            iid = str(item.get("item_id"))
                            models = item.get("model_list", [])

                            # Processar cada variação ou o item base se não houver modelos
                            model_data_list = models if models else [item]
                            for m in model_data_list:
                                mid = str(m.get("model_id", "0"))

                                # Conversões robustas para evitar crash com nulls da API
                                try:
                                    # Prioriza o preço promocional da Shopee se existir
                                    p_promo = m.get(
                                        "model_promotion_price"
                                    ) or item.get("item_promotion_price")
                                    # Se não houver preço promocional explícito, mas o current_price for diferente do original_price
                                    # podemos inferir que existe uma promoção (comum em algumas versões da API)
                                    orig = float(m.get("original_price") or 0.0)

                                    promo = float(p_promo) if p_promo else None
                                except (TypeError, ValueError):
                                    orig = 0.0
                                    promo = None

                                current_item_models.add((iid, mid))

                                # 1. Garantir que o Anúncio Pai exista e esteja completo
                                anuncio = Anuncios.query.filter_by(
                                    shopee_item_id=iid
                                ).first()
                                if not anuncio:
                                    anuncio = Anuncios(
                                        shopee_item_id=iid,
                                        nome=item.get("item_name", "Anúncio Importado"),
                                        sku_pai=item.get("item_sku", ""),
                                    )
                                    db.session.add(anuncio)
                                    db.session.flush()
                                else:
                                    # Se já existe mas o SKU ou nome está vazio, atualiza
                                    if not anuncio.sku_pai and item.get("item_sku"):
                                        anuncio.sku_pai = item.get("item_sku")
                                    if (
                                        not anuncio.nome
                                        or anuncio.nome == "Anúncio Importado"
                                    ) and item.get("item_name"):
                                        anuncio.nome = item.get("item_name")

                                # 2. Atualizar ou criar o Produto (Variação)
                                p = Produtos.query.filter_by(
                                    shopee_item_id=iid, shopee_model_id=mid
                                ).first()

                                if not p:
                                    p = Produtos(
                                        shopee_item_id=iid,
                                        shopee_model_id=mid,
                                        nome_variacao=m.get("model_name", "Padrão"),
                                        preco_base=orig,
                                        sku=m.get("model_sku") or item.get("item_sku"),
                                        anuncio_id=anuncio.id,
                                    )
                                    db.session.add(p)
                                else:
                                    # Se já existe, garante que o SKU e o vínculo com anúncio estão certos
                                    if not p.sku:
                                        p.sku = m.get("model_sku") or item.get(
                                            "item_sku"
                                        )
                                    if not p.anuncio_id:
                                        p.anuncio_id = anuncio.id
                                    if (
                                        not p.nome_variacao
                                        or p.nome_variacao == "Padrão"
                                    ) and m.get("model_name"):
                                        p.nome_variacao = m.get("model_name")
                                    if orig > 0:
                                        p.preco_base = orig

                                if p:
                                    p.promotion_id = str(discount_id)
                                    p.preco_promocional = promo
                                    p.updated_at = datetime.utcnow()

                        db.session.commit()
                        current_sync_page += 1
                        if current_sync_page > 20:
                            break

                # Se não conseguiu baixar nada (erro de API), registra o log mas permite continuar para mostrar o que tem no banco
                if not has_fetched_any:
                    print(
                        f"[AVISO] Sincronizacao falhou para campanha {discount_id}, exibindo dados locais."
                    )

                # Limpeza: itens que estavam nesta campanha localmente mas NÃO vieram na api da Shopee
                # Otimizado: Usar update em lote para evitar locks individuais e timeouts
                # SEGURANÇA: Só limpa se o sync teve sucesso (evita zerar banco em erro de API)
                if has_fetched_any:
                    others = Produtos.query.filter(
                        Produtos.promotion_id == str(discount_id)
                    ).all()
                    to_clear_ids = [
                        op.id
                        for op in others
                        if (op.shopee_item_id, op.shopee_model_id)
                        not in current_item_models
                    ]

                    if to_clear_ids:
                        from sqlalchemy import update  # pyrefly: ignore [missing-import]

                        db.session.query(Produtos).filter(
                            Produtos.id.in_(to_clear_ids)
                        ).update(
                            {
                                Produtos.promotion_id: None,
                                Produtos.preco_promocional: None,
                            },
                            synchronize_session=False,
                        )

                    db.session.commit()
            except Exception as e:
                db.session.rollback()
                import traceback

                # Retry simples em caso de Deadlock (Psycopg2)
                if "deadlock" in str(e).lower():
                    print(
                        f"[SYNC] Deadlock detectado na campanha {discount_id}. Tentando novamente em 1s..."
                    )
                    time.sleep(1)
                    return self.get_discount_item_list(
                        creds, discount_id, page, page_size, search
                    )

                print(f"[AVISO] Erro na sincronizacao passiva de campanha {discount_id}: {e}")
                traceback.print_exc()

        # 2. Busca no Banco Local (Após sincronização ou para páginas subsequentes)
        flat_items = []
        from sqlalchemy import or_  # pyrefly: ignore [missing-import]

        query = (
            db.session.query(Produtos, Anuncios.nome)
            .join(Anuncios, Produtos.shopee_item_id == Anuncios.shopee_item_id)
            .filter(Produtos.promotion_id == str(discount_id))
        )

        if search:
            search_str = f"%{search}%"
            query = query.filter(
                or_(
                    Anuncios.nome.ilike(search_str),
                    Anuncios.sku_pai.ilike(search_str),
                    Produtos.sku.ilike(search_str),
                    Produtos.shopee_item_id.ilike(search_str),
                    Produtos.shopee_model_id.ilike(search_str),
                )
            )

        query_db = query.all()

        if query_db:
            total_items = len(query_db)
            total_pages = math.ceil(total_items / page_size)

            # Paginação Local
            start = (page - 1) * page_size
            end = start + page_size
            page_items = query_db[start:end]
            has_more = total_items > end

            for p, item_nome in page_items:
                flat_items.append(
                    {
                        "item_id": str(p.shopee_item_id),
                        "item_name": item_nome,
                        "model_id": str(p.shopee_model_id or "0"),
                        "model_name": p.nome_variacao or "Padrão",
                        "original_price": p.preco_base,
                        "promotion_price": p.preco_promocional or 0,
                        "stock": 0,
                        "sku": p.sku or "",
                    }
                )

            # Contagem de IDs Únicos para o limite de 1000 da Shopee
            total_unique_items = (
                db.session.query(Produtos.shopee_item_id)
                .filter(Produtos.promotion_id == str(discount_id))
                .distinct()
                .count()
            )

            return {
                "item_list": flat_items,
                "total": total_items,
                "total_unique": total_unique_items,
                "pages": total_pages,
                "more": has_more,
            }

        return {"item_list": [], "total": 0, "pages": 0, "more": False}

    def auto_promote_item(self, creds, item_id, model_id, force=False):
        """Busca uma campanha ativa com espaço e adiciona o item com 25% de desconto."""
        from model.shopeeModel import Produtos, Promocoes, db
        import time

        try:
            # 1. Buscar o produto para pegar o preço base
            p = Produtos.query.filter_by(
                shopee_item_id=str(item_id), shopee_model_id=str(model_id)
            ).first()
            if not p:
                return {
                    "status": "erro",
                    "mensagem": "Produto não encontrado no banco local.",
                }, 404

            if not p.preco_base or p.preco_base <= 0:
                return {
                    "status": "erro",
                    "mensagem": "Produto sem preço base cadastrado.",
                }, 400

            # 2. Calcular preço com 25% de desconto
            promo_price = round(p.preco_base * 0.75, 2)

            # 3. Buscar uma campanha 'ongoing' que tenha espaço (< 1000 itens)
            campaigns = (
                Promocoes.query.filter_by(status="ongoing")
                .order_by(Promocoes.end_time.asc())
                .all()
            )

            target_campaign = None
            for c in campaigns:
                count = (
                    db.session.query(Produtos.shopee_item_id)
                    .filter_by(promotion_id=str(c.discount_id))
                    .distinct()
                    .count()
                )
                if count < 1000:
                    target_campaign = c
                    break

            if not target_campaign:
                # Tenta sincronizar a lista de campanhas para ver se há novas
                self.get_shopee_discounts(creds, status="ongoing", force_sync=True)
                campaigns = (
                    Promocoes.query.filter_by(status="ongoing")
                    .order_by(Promocoes.end_time.asc())
                    .all()
                )
                for c in campaigns:
                    count = (
                        db.session.query(Produtos.shopee_item_id)
                        .filter_by(promotion_id=str(c.discount_id))
                        .distinct()
                        .count()
                    )
                    if count < 1000:
                        target_campaign = c
                        break

            if not target_campaign:
                return {
                    "status": "erro",
                    "mensagem": "Nenhuma campanha ativa com espaço disponível encontrada.",
                }, 404

            # 4. Adicionar o item à campanha na Shopee
            items_to_add = []
            mid_int = int(model_id) if model_id and model_id != "0" else 0

            if mid_int == 0:
                items_to_add.append(
                    {"item_id": int(item_id), "item_promotion_price": promo_price}
                )
            else:
                items_to_add.append(
                    {
                        "item_id": int(item_id),
                        "model_list": [
                            {"model_id": mid_int, "model_promotion_price": promo_price}
                        ],
                    }
                )

            res, code = self.add_discount_item(
                creds,
                target_campaign.discount_id,
                items_to_add,
                log_msg="Campanha de Promoção",
                origem="Notificações",
                force=force,
            )

            if code == 200:
                return {
                    "status": "sucesso",
                    "mensagem": f"Item em promoção na campanha '{target_campaign.discount_name}' com 25% OFF.",
                }, 200
            else:
                return res, code

        except Exception as e:
            db.session.rollback()
            import traceback

            traceback.print_exc()
            return {"status": "erro", "mensagem": str(e)}, 500

    def add_discount_item(
        self,
        creds,
        discount_id,
        items,
        log_msg="Preço de Promoção Atualizado",
        origem=None,
        force=False,
    ):
        """
        Adiciona itens a uma promoção existente e atualiza o Banco Local.
        """
        # --- NOVO: Limite de 995 Anúncios (Margem de Segurança) por Campanha ---
        count_atual = (
            db.session.query(Produtos.shopee_item_id)
            .filter_by(promotion_id=str(discount_id))
            .distinct()
            .count()
        )
        if (count_atual + len(items)) > 995:
            msg = f"Campanha no Limite ({count_atual}/1000). Remova itens da promoção para poder adicionar novos."
            return {"status": "erro", "message": msg, "mensagem": msg}, 400

        # --- NOVO: Validação de Segurança (Preço Base Inflado) ---
        for itm in items:
            iid = str(itm.get("item_id"))
            models = itm.get("model_list", [])
            if models:
                for m in models:
                    mid = str(m.get("model_id"))
                    is_locked, _, msg = self.validate_price_lock(iid, mid)
                    if is_locked and not force:
                        # Retornar no formato que a rota espera (que por sua vez repassa o status_code)
                        return {
                            "status": "erro",
                            "error": "error_param",
                            "message": msg,
                            "mensagem": msg,
                        }, 403
            else:
                is_locked, _, msg = self.validate_price_lock(iid, 0)
                if is_locked and not force:
                    return {
                        "status": "erro",
                        "error": "error_param",
                        "message": msg,
                        "mensagem": msg,
                    }, 403
        # ---------------------------------------------------------

        path = "/api/v2/discount/add_discount_item"
        payload = {"discount_id": int(discount_id), "item_list": items}
        resp, code = self._shopee_request(path, creds, method="POST", json_data=payload)

        if code == 200:
            # Sincronizar Localmente
            for itm in items:
                iid = str(itm.get("item_id"))
                models = itm.get("model_list", [])
                if models:
                    for m in models:
                        mid = str(m.get("model_id"))
                        price = float(m.get("model_promotion_price") or 0)
                        p = Produtos.query.filter_by(
                            shopee_item_id=iid, shopee_model_id=mid
                        ).first()
                        if p:
                            p.promotion_id = str(discount_id)
                            p.preco_promocional = price

                            # Logar no Histórico
                            from flask import g  # pyrefly: ignore [missing-import]

                            u_id = (
                                getattr(g, "current_user", None).id
                                if hasattr(g, "current_user") and g.current_user
                                else None
                            )
                            self._log_and_save_update(
                                iid,
                                mid,
                                p.nome_variacao
                                or (p.anuncio.nome if p.anuncio else "Produto"),
                                p.preco_base,  # Preço antigo (base)
                                price,  # Preço novo (promo)
                                "sucesso",
                                log_msg,
                                sku=p.sku,
                                promo_info={"promotion_id": discount_id},
                                usuario_id=u_id,
                                origem=origem,
                            )
                else:
                    price = float(itm.get("item_promotion_price", 0))
                    p = Produtos.query.filter_by(
                        shopee_item_id=iid, shopee_model_id="0"
                    ).first()
                    if p:
                        p.promotion_id = str(discount_id)
                        p.preco_promocional = price

                        # Logar no Histórico
                        from flask import g  # pyrefly: ignore [missing-import]

                        u_id = (
                            getattr(g, "current_user", None).id
                            if hasattr(g, "current_user") and g.current_user
                            else None
                        )
                        self._log_and_save_update(
                            iid,
                            "0",  # Model ID para produto simples é sempre "0"
                            p.nome_variacao
                            or (p.anuncio.nome if p.anuncio else "Produto"),
                            p.preco_base,  # Preço antigo (base)
                            price,  # Preço novo (promo)
                            "sucesso",
                            log_msg,
                            sku=p.sku,
                            promo_info={"promotion_id": discount_id},
                            usuario_id=u_id,
                            origem=origem,
                        )
            db.session.commit()

        return resp, code

    def update_discount_items_in_campaign(
        self,
        creds,
        discount_id,
        items,
        log_msg="Preço de Promoção Atualizado (Campanha)",
        origem=None,
        force=False,
    ):
        """
        Atualiza o preço de itens que já estão em uma promoção.
        """
        for itm in items:
            iid = str(itm.get("item_id"))
            models = itm.get("model_list", [])
            if models:
                for m in models:
                    mid = str(m.get("model_id"))
                    is_locked, _, msg = self.validate_price_lock(iid, mid)
                    if is_locked and not force:
                        return {
                            "status": "erro",
                            "error": "error_param",
                            "message": msg,
                            "mensagem": msg,
                        }, 403
            else:
                is_locked, _, msg = self.validate_price_lock(iid, 0)
                if is_locked and not force:
                    return {
                        "status": "erro",
                        "error": "error_param",
                        "message": msg,
                        "mensagem": msg,
                    }, 403

        path = "/api/v2/discount/update_discount_item"
        payload = {"discount_id": int(discount_id), "item_list": items}
        resp, code = self._shopee_request(path, creds, method="POST", json_data=payload)

        if code == 200:
            for itm in items:
                iid = str(itm.get("item_id"))
                models = itm.get("model_list", [])
                if models:
                    for m in models:
                        mid = str(m.get("model_id"))
                        price = float(m.get("model_promotion_price") or 0)
                        p = Produtos.query.filter_by(
                            shopee_item_id=iid, shopee_model_id=mid
                        ).first()
                        if p:
                            p.preco_promocional = price
                            from flask import g  # pyrefly: ignore [missing-import]

                            u_id = (
                                getattr(g, "current_user", None).id
                                if hasattr(g, "current_user") and g.current_user
                                else None
                            )
                            self._log_and_save_update(
                                iid,
                                mid,
                                p.nome_variacao
                                or (p.anuncio.nome if p.anuncio else "Produto"),
                                p.preco_base,
                                price,
                                "sucesso",
                                log_msg,
                                sku=p.sku,
                                promo_info={"promotion_id": discount_id},
                                usuario_id=u_id,
                                origem=origem,
                            )
                else:
                    price = float(itm.get("item_promotion_price") or 0)
                    p = Produtos.query.filter_by(
                        shopee_item_id=iid, shopee_model_id="0"
                    ).first()
                    if p:
                        p.preco_promocional = price
                        from flask import g  # pyrefly: ignore [missing-import]

                        u_id = (
                            getattr(g, "current_user", None).id
                            if hasattr(g, "current_user") and g.current_user
                            else None
                        )
                        self._log_and_save_update(
                            iid,
                            "0",
                            p.nome_variacao
                            or (p.anuncio.nome if p.anuncio else "Produto"),
                            p.preco_base,
                            price,
                            "sucesso",
                            log_msg,
                            sku=p.sku,
                            promo_info={"promotion_id": discount_id},
                            usuario_id=u_id,
                            origem=origem,
                        )
            db.session.commit()

        return resp, code

    def delete_discount_item(self, creds, discount_id, item_id, model_id=0):
        """Remove um item de uma promoção e limpa o Banco Local."""
        path = "/api/v2/discount/delete_discount_item"
        payload = {"discount_id": int(discount_id), "item_id": int(item_id)}
        if model_id and int(model_id) > 0:
            payload["model_id"] = int(model_id)

        resp, code = self._shopee_request(path, creds, method="POST", json_data=payload)

        if code == 200:
            # Limpar Localmente
            mid = str(model_id) if model_id and int(model_id) > 0 else "0"
            p = Produtos.query.filter_by(
                shopee_item_id=str(item_id), shopee_model_id=mid
            ).first()
            if p:
                p.promotion_id = None
                p.preco_promocional = None
            db.session.commit()

        return resp, code

    def revalidate_all_locks(self):
        """Varre todos os produtos travados e desbloqueia os que passaram do tempo ou se a trava for 0."""
        from model.shopeeModel import Produtos, Configuracoes, db
        from datetime import datetime, timedelta

        config = Configuracoes.query.first()
        dias_trava = config.dias_espera_simples if config else 15

        print(f"DEBUG LOCK: Iniciando revalidação com trava de {dias_trava} dias.")

        # Se a trava for 0, desbloqueia TUDO
        if dias_trava <= 0:
            # 1. Limpa as datas de modificação nos produtos
            prod_count = (
                db.session.query(Produtos)
                .filter(Produtos.preco_modificado_em != None)
                .update(
                    {"preco_modificado_em": None, "notificado_desbloqueio": True},
                    synchronize_session=False,
                )
            )

            # 2. Converte notificações de 'bloqueio' para 'desbloqueio' para liberar o botão no frontend
            from model.shopeeModel import NotificacaoSistema

            notif_count = (
                db.session.query(NotificacaoSistema)
                .filter(NotificacaoSistema.tipo == "bloqueio")
                .update(
                    {
                        "tipo": "desbloqueio",
                        "mensagem": "Este anúncio foi desbloqueado pela alteração das configurações e já pode ser colocado em Promoção.",
                    },
                    synchronize_session=False,
                )
            )

            db.session.commit()
            print(
                f"DEBUG LOCK: Trava 0. {prod_count} produtos e {notif_count} notificações liberados."
            )
            return prod_count

        # Caso contrário, verifica a data (UTC para bater com o banco)
        limite = datetime.utcnow() - timedelta(days=dias_trava)
        count = (
            db.session.query(Produtos)
            .filter(
                Produtos.preco_modificado_em != None,
                Produtos.preco_modificado_em <= limite,
            )
            .update({"preco_modificado_em": None}, synchronize_session=False)
        )
        db.session.commit()
        print(
            f"DEBUG LOCK: {count} anúncios desbloqueados por atingirem o limite de {dias_trava} dias."
        )
        return count


def _save_sync_log(total_sincronizados, erro=None):
    try:
        from model.shopeeModel import db, SyncLog
        log = SyncLog(total_sincronizados=total_sincronizados, erro=erro)
        db.session.add(log)
        db.session.commit()
        print(f"[SyncLog] Log salvo com sucesso: {total_sincronizados} itens, erro: {erro}")
    except Exception as ex:
        print(f"[SyncLog] Erro ao salvar log no banco: {ex}")


def run_full_sync_job():
    """Job do RQ para sincronizar todos os anúncios vinculados.

    NOTA: O RQ Worker já empurra um app_context(), então NÃO importamos
    'app' diretamente (isso puxaria eventlet e causaria BlockingIOError).
    """
    from model.shopeeModel import Anuncios
    from config.redis_config import redis_conn

    # GARANTIR que começamos sem sinais de cancelamento antigos
    redis_conn.delete("shopee_sync_cancel")

    service = ShopeeService()
    try:
        # 1. Buscar credenciais válidas (pega a primeira loja configurada por padrão)
        creds, erro = service.tokens.ensure_valid_token()
        if erro:
            print(f"--- [RQ ERROR] Token Inválido: {erro} ---")
            service._update_sync_status(
                is_running=False, mensagem=f"Erro de Token: {erro}"
            )
            _save_sync_log(total_sincronizados=0, erro=f"Erro de Token: {erro}")
            return

        # 2. Resetar e buscar todos os IDs de itens DIRETAMENTE DA SHOPEE (Full Sync)
        service._update_sync_status(
            is_running=True,
            total=0,
            atual=0,
            sucessos=0,
            erros=0,
            mensagem="Buscando lista de anúncios na Shopee...",
        )

        all_item_ids = service.get_item_ids(creds)

        if not all_item_ids:
            print("--- [RQ INFO] Nenhum ID coletado na Shopee. ---")
            service._update_sync_status(
                is_running=False, mensagem="Nenhum anúncio encontrado na Shopee."
            )
            _save_sync_log(total_sincronizados=0, erro="Nenhum anúncio encontrado na Shopee.")
            return

        # 3. Executa a lógica de sincronização em lotes
        print(f"--- [RQ START] Iniciando Worker para {len(all_item_ids)} itens ---")
        service._run_sync_worker_logic(all_item_ids, creds)
        print(
            f"--- [RQ FINISH] Sincronização Finalizada: {len(all_item_ids)} itens processados ---"
        )

        # 4. Revalidar travas após a sincronização
        print("--- [RQ JOB] Revalidando travas de atualização ---")
        service.revalidate_all_locks()

        # Salvar log de sucesso
        progress = service.get_sync_progress()
        err_msg = None
        if progress.get("erros", 0) > 0:
            err_msg = f"{progress.get('erros')} erro(s) durante a sincronização."
        _save_sync_log(total_sincronizados=progress.get("sucessos", 0), erro=err_msg)

    except SyncCancelledException as e:
        print(f"--- [RQ INFO] Sincronização cancelada pelo usuário: {str(e)} ---")
        service._update_sync_status(
            is_running=False, mensagem="Cancelado pelo usuário."
        )
        progress = service.get_sync_progress()
        _save_sync_log(total_sincronizados=progress.get("sucessos", 0), erro="Cancelado pelo usuário.")
    except Exception as e:
        import traceback

        tb = traceback.format_exc()
        print(f"--- [RQ CRITICAL ERROR] ---\n{tb}")
        service._update_sync_status(
            is_running=False, mensagem=f"Erro Crítico: {str(e)}"
        )
        progress = service.get_sync_progress()
        _save_sync_log(total_sincronizados=progress.get("sucessos", 0), erro=f"Erro Crítico: {str(e)}")


def run_unlock_check_job():
    """Job do RQ para verificar desbloqueios diários.

    NOTA: O RQ Worker já empurra um app_context().
    """
    service = ShopeeService()
    print("--- [RQ JOB] Verificando Desbloqueios ---")
    service.verificar_todos_desbloqueios()
    print("--- [RQ JOB] Verificação Finalizada ---")
