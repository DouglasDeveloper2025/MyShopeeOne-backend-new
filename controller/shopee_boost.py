from model.shopeeModel import db, Anuncios, Produtos, BoostLog, Configuracoes, get_br_now
from utils.shopee_client import ShopeeClient
from datetime import datetime, timedelta
from sqlalchemy import or_  # pyrefly: ignore [missing-import]
import logging
import random

logger = logging.getLogger(__name__)


class BoostController:
    def __init__(self):
        self.client = ShopeeClient()

    def sync_boost_status(self):
        """Sincroniza o status de boost da Shopee com o banco local."""
        resp = self.client.get_boosted_list()

        if resp.get("error") and resp.get("error") != "":
            self._log_boost(
                None, "sync_error", "error", f"Erro ao buscar lista de boost: {resp}"
            )
            return False

        boosted_items = resp.get("response", {}).get("item_list", [])
        boosted_ids = [str(item["item_id"]) for item in boosted_items]

        agora = get_br_now()

        # 1. Reset status de quem não está mais na lista da Shopee E cujo prazo já venceu
        # OU se o boost começou há mais de 3 minutos e ainda não aparece na lista da Shopee (limpeza de slots fantasmas)
        anuncios_ativos_db = Anuncios.query.filter(Anuncios.boost_end_at != None).all()
        for anuncio in anuncios_ativos_db:
            if anuncio.shopee_item_id not in boosted_ids:
                limite_propaga = agora + timedelta(minutes=237)
                if anuncio.boost_end_at <= agora or anuncio.boost_end_at < limite_propaga:
                    anuncio.boost_end_at = None
                    self._log_boost(
                        anuncio.shopee_item_id,
                        "boost_expired",
                        "info",
                        f"Boost de '{anuncio.nome}' não está ativo na Shopee e slot foi liberado.",
                        anuncio.nome
                    )

        # 2. Atualiza status de quem está na lista vinda da API
        for item in boosted_items:
            item_id = str(item["item_id"])
            anuncio = Anuncios.query.filter_by(shopee_item_id=item_id).first()
            if anuncio:
                remaining_seconds = item["cool_down_second"]
                end_time = agora + timedelta(seconds=remaining_seconds)
                anuncio.boost_end_at = end_time
                anuncio.last_boost_at = end_time - timedelta(hours=4)

        db.session.commit()

        # 3. Reforço de Segurança: O número de slots ocupados será a união dos IDs ativos na API
        # com os IDs ativos localmente no nosso banco de dados (que ainda não propagaram na API).
        # Isso impede tentativas duplicadas se a API falhar ou estiver com delay de propagação.
        local_active_ids = [
            anuncio.shopee_item_id 
            for anuncio in Anuncios.query.filter(Anuncios.boost_end_at > agora).all() 
            if anuncio.shopee_item_id
        ]
        all_active_ids = set(boosted_ids) | set(local_active_ids)
        true_active_count = len(all_active_ids)
        
        return true_active_count

    def run_boost_cycle(self):
        """Lógica principal do Worker: Sincroniza, verifica slots e impulsiona o próximo."""
        from config.redis_config import redis_conn
        
        # Verifica se estamos em cooldown de limite de slots antes de adquirir o lock
        try:
            if redis_conn and redis_conn.get("shopee_boost_slot_limit_cooldown"):
                logger.info("Ciclo de boost ignorado: Cooldown de limite de slots ativo (10 minutos).")
                return "Slots cheios (cooldown)"
        except Exception as e:
            logger.warning(f"Erro ao verificar cooldown do Redis: {e}")

        lock = None
        db_locked = False
        try:
            lock = redis_conn.lock("shopee_boost_cycle_lock", timeout=600, blocking_timeout=1)
            if not lock.acquire(blocking=False):
                logger.warning("Ciclo de boost já está em andamento. Ignorando execução sobreposta.")
                return "Já em andamento"
            
            # Verifica e define o timestamp da última execução para evitar duplicidade
            try:
                import time
                last_run = redis_conn.get("shopee_boost_last_run_timestamp")
                if last_run:
                    last_run_time = float(last_run)
                    if time.time() - last_run_time < 45: # 45 segundos de intervalo mínimo
                        logger.info("Ciclo de boost ignorado para evitar execução em duplicidade (intervalo menor que 45s).")
                        try:
                            lock.release()
                        except Exception:
                            pass
                        return "Ignorado (duplicidade/concorrência)"
                redis_conn.setex("shopee_boost_last_run_timestamp", 300, str(time.time()))
            except Exception as rex:
                logger.warning(f"Erro ao verificar/definir timestamp de execução no Redis: {rex}")
        except Exception as e:
            logger.warning(f"Aviso Redis Lock: {e}")
            lock = None

        # Adquire lock de banco adicional para garantir mutual exclusion absoluta
        from sqlalchemy import text
        if db.engine.dialect.name == "postgresql":
            try:
                result = db.session.execute(text("SELECT pg_try_advisory_lock(888888)"))
                db_locked = result.scalar()
                if not db_locked:
                    logger.warning("Ciclo de boost bloqueado por advisory lock no Postgres (execução concorrente).")
                    if lock:
                        try:
                            lock.release()
                        except Exception:
                            pass
                    return "Já em andamento"
            except Exception as dberr:
                logger.warning(f"Erro ao tentar adquirir advisory lock no banco: {dberr}")

        try:
            active_count = self.sync_boost_status()
            if active_count is False:
                return "Erro na sincronização"

            if active_count >= 5:
                return "Slots cheios"

            slots_available = 5 - active_count

            agora = get_br_now()
            quatro_horas_atras = agora - timedelta(minutes=240)

            base_query = (
                Anuncios.query.filter(Anuncios.status.in_(["NORMAL", "ATIVO"]))
                .filter(Anuncios.boost_end_at == None)
                .filter(Anuncios.estoque_total >= 3)
                .filter(
                    or_(
                        Anuncios.last_boost_at == None,
                        Anuncios.last_boost_at < quatro_horas_atras
                    )
                )
            )

            # 1. Busca produtos com PRIORIDADE habilitada
            priority_candidates = (
                base_query.filter_by(boost_priority=True)
                .order_by(Anuncios.last_boost_at.asc().nullsfirst())
                .all()
            )

            # 2. Busca produtos sem prioridade
            normal_query = base_query.filter_by(boost_priority=False)

            # Pega configuração de modo
            config = Configuracoes.query.first()
            mode = config.boost_mode if config else "sequential"

            if mode == "sequential":
                normal_candidates = normal_query.order_by(
                    Anuncios.last_boost_at.asc().nullsfirst(), Anuncios.sku_pai.asc()
                ).all()
            else:
                # Modo Aleatório
                normal_candidates = normal_query.all()
                random.shuffle(normal_candidates)

            # Lista final de candidatos: Prioridade vem primeiro
            all_candidates = priority_candidates + normal_candidates

            # Seleciona apenas os necessários para preencher os slots
            final_selection = all_candidates[:slots_available]

            if not final_selection:
                # Se não há candidatos SEM boost ativo, mas o modo é sequencial,
                # verificamos se já passamos por todos (fim de ciclo).
                if mode == "sequential":
                    # Se não há ninguém na fila (Anuncios.boost_end_at == None),
                    # significa que ou todos estão impulsionados ou o ciclo acabou.
                    # Mas se temos slots livres (active_count < 5) e ninguém para subir,
                    # e ainda temos produtos habilitados no banco, então é hora de resetar.
                    has_enabled = (
                        Anuncios.query.filter(Anuncios.status.in_(["NORMAL", "ATIVO"]))
                        .filter(Anuncios.estoque_total >= 3)
                        .first()
                    )
                    if has_enabled:
                        # Para resetar, limpamos o last_boost_at de todos para recomeçar o ciclo de antiguidade
                        self._log_boost(
                            None,
                            "boost_reset",
                            "info",
                            "Todos os Anuncios foram Impulsionandos. Resetando Todo o Processo no modo Sequencial.",
                        )
                        # Não limpamos last_boost_at (senão perdemos o histórico),
                        # o nullsfirst do query já garante o reinício natural.
                        return "Ciclo resetado"

                return "Sem candidatos"

            boosted_count = 0
            for candidate in final_selection:
                logger.info(
                    f"Tentando impulsionar {candidate.nome} ({candidate.shopee_item_id}) [Prioridade: {candidate.boost_priority}]"
                )
                resp = self.client.boost_item(candidate.shopee_item_id)

                if resp.get("error") and resp.get("error") != "":
                    msg = resp.get("message") or resp.get("error")
                    self._log_boost(
                        candidate.shopee_item_id,
                        "boost_error",
                        "error",
                        f"Falha ao impulsionar: {msg}",
                        candidate.nome,
                    )
                    
                    # Trata erro de item status not normal ou estoque zerado na shopee
                    if "item status not normal" in msg.lower() or "stock is 0" in msg.lower():
                        anuncio_db = Anuncios.query.filter_by(shopee_item_id=candidate.shopee_item_id).first()
                        if anuncio_db:
                            anuncio_db.boost_enabled = False
                            if anuncio_db.status in ["NORMAL", "ATIVO"]:
                                anuncio_db.status = "UNLIST"
                            anuncio_db.estoque_total = 0
                            
                            # Atualiza as variações correspondentes
                            variacoes = Produtos.query.filter_by(shopee_item_id=anuncio_db.shopee_item_id).all()
                            if len(variacoes) == 1 and variacoes[0].shopee_model_id == "0":
                                # Produto simples (sem variações reais)
                                variacoes[0].estoque = 0
                                variacoes[0].situacao = "UNLIST"
                                logger.info(f"Produto simples {anuncio_db.nome} atualizado para estoque 0 e UNLIST.")
                            else:
                                # Produto com variações
                                try:
                                    try:
                                        item_id_int = int(anuncio_db.shopee_item_id)
                                    except (ValueError, TypeError):
                                        item_id_int = 0

                                    if item_id_int > 0:
                                        model_resp = self.client.request(
                                            "GET",
                                            "/api/v2/product/get_model_list",
                                            params={"item_id": item_id_int}
                                        )
                                    else:
                                        model_resp = {"error": "Invalid item ID"}
                                    if model_resp and not model_resp.get("error"):
                                        models_list = model_resp.get("response", {}).get("model", [])
                                        updated_vars = []
                                        for m in models_list:
                                            mid = str(m.get("model_id"))
                                            m_stock_v2 = m.get("stock_info_v2", {})
                                            stock = m_stock_v2.get("summary_info", {}).get("total_available_stock", 0)
                                            if stock == 0:
                                                # Encontra variação local
                                                local_var = next((v for v in variacoes if v.shopee_model_id == mid), None)
                                                if local_var:
                                                    local_var.estoque = 0
                                                    local_var.situacao = "UNLIST"
                                                    updated_vars.append(mid)
                                        logger.info(f"Variacoes com estoque zerado atualizadas localmente: {updated_vars}")
                                    else:
                                        logger.error(f"Erro ao buscar get_model_list da Shopee para {anuncio_db.shopee_item_id}: {model_resp}")
                                except Exception as err:
                                    logger.error(f"Exceção ao buscar/atualizar variações de boost para {anuncio_db.shopee_item_id}: {err}")
                            
                            db.session.commit()
                            self._log_boost(
                                anuncio_db.shopee_item_id,
                                "boost_disabled",
                                "info",
                                f"Impulsionamento pausado e estoque zerado/status alterado para UNLIST devido ao erro: {msg}",
                                anuncio_db.nome
                            )
                    
                    # Trata erro de cooldown de 240min (do not bump same product under 240min)
                    elif "240min" in msg.lower() or "cooldown" in msg.lower() or "under 240" in msg.lower():
                        candidate.last_boost_at = get_br_now()
                        db.session.commit()
                    
                    # Trata erro de limite de slots (reached shop's bump slot limit)
                    elif "limit" in msg.lower() or "limite" in msg.lower() or "slot" in msg.lower():
                        logger.warning(f"Limite de slots atingido na Shopee ao tentar impulsionar {candidate.nome}. Interrompendo loop.")
                        try:
                            redis_conn.setex("shopee_boost_slot_limit_cooldown", 600, "1")
                            logger.info("Definido cooldown de 10 minutos para limite de slots no Redis.")
                        except Exception as rex:
                            logger.warning(f"Erro ao definir cooldown de limite de slots no Redis: {rex}")
                        break
                else:
                    candidate.last_boost_at = get_br_now()
                    candidate.boost_end_at = get_br_now() + timedelta(hours=4)
                    db.session.commit()
                    self._log_boost(
                        candidate.shopee_item_id,
                        "boost_start",
                        "success",
                        f"Produto '{candidate.nome}' impulsionado com sucesso!",
                        candidate.nome,
                    )
                    boosted_count += 1

            db.session.commit()
            return f"Ciclo concluído. Impulsionados com sucesso: {boosted_count}"

        except Exception as e:
            db.session.rollback()
            self._log_boost(
                None,
                "cycle_error",
                "error",
                f"Erro crítico no ciclo de boost: {str(e)}",
            )
            return f"Erro: {str(e)}"
        
        finally:
            if db_locked and db.engine.dialect.name == "postgresql":
                try:
                    from sqlalchemy import text
                    db.session.execute(text("SELECT pg_advisory_unlock(888888)"))
                    db.session.commit()
                except Exception as dberr:
                    logger.warning(f"Erro ao tentar liberar advisory lock no banco: {dberr}")
            if lock:
                try:
                    lock.release()
                except Exception:
                    pass

    def _log_boost(self, item_id, acao, status, mensagem, nome=None):
        log = BoostLog(
            shopee_item_id=item_id,
            nome_produto=nome,
            acao=acao,
            status=status,
            mensagem=mensagem,
        )
        db.session.add(log)
        db.session.commit()

    def get_next_boosts(self, limit=5):
        """Retorna a lista de próximos anúncios a serem impulsionados (os próximos 5 da fila)."""
        # Filtra apenas quem NÃO está com boost ativo no momento, tem status permitido e não foi impulsionado nos últimos 240min
        agora = get_br_now()
        quatro_horas_atras = agora - timedelta(minutes=240)
        
        base_query = (
            Anuncios.query.filter(Anuncios.status.in_(["NORMAL", "ATIVO"]))
            .filter(Anuncios.boost_end_at == None)
            .filter(Anuncios.estoque_total >= 3)
            .filter(
                or_(
                    Anuncios.last_boost_at == None,
                    Anuncios.last_boost_at < quatro_horas_atras
                )
            )
        )

        # Prioridade vem sempre na frente
        priority = (
            base_query.filter_by(boost_priority=True)
            .order_by(Anuncios.last_boost_at.asc().nullsfirst())
            .limit(limit)
            .all()
        )

        config = Configuracoes.query.first()
        mode = config.boost_mode if config else "sequential"

        # Calcula quanto ainda precisamos buscar
        remaining_limit = max(0, limit - len(priority))

        normal = []
        if remaining_limit > 0:
            if mode == "sequential":
                normal = (
                    base_query.filter_by(boost_priority=False)
                    .order_by(
                        Anuncios.last_boost_at.asc().nullsfirst(),
                        Anuncios.sku_pai.asc(),
                    )
                    .limit(remaining_limit)
                    .all()
                )
            else:
                # Para aleatório com muitos itens, pegamos uma amostra maior e embaralhamos
                normal = (
                    base_query.filter_by(boost_priority=False)
                    .limit(remaining_limit * 2)
                    .all()
                )
                random.shuffle(normal)
                normal = normal[:remaining_limit]

        return priority + normal


def run_boost_job():
    """Job a ser chamado pelo Worker/Scheduler."""
    from flask import current_app  # pyrefly: ignore [missing-import]

    # O current_app já tem o contexto no worker
    controller = BoostController()
    result = controller.run_boost_cycle()
    # print(f"--- [RQ BOOST] {result} ---")
