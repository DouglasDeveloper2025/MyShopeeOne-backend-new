from model.shopeeModel import db, Anuncios, BoostLog, Configuracoes, get_br_now
from utils.shopee_client import ShopeeClient
from datetime import datetime, timedelta
from sqlalchemy import or_
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
        anuncios_ativos_db = Anuncios.query.filter(Anuncios.boost_end_at != None).all()
        for anuncio in anuncios_ativos_db:
            # Se não está na lista da Shopee OU se o tempo já passou localmente
            if anuncio.shopee_item_id not in boosted_ids and anuncio.boost_end_at <= agora:
                anuncio.boost_end_at = None
                self._log_boost(
                    anuncio.shopee_item_id,
                    "boost_expired",
                    "info",
                    f"Boost de '{anuncio.nome}' expirou e slot foi liberado.",
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

        # 3. Reforço de Segurança: O número de slots ocupados será o MAIOR valor 
        # entre o que a API relata e o que nosso banco de dados sabe que ainda não venceu.
        # Isso impede tentativas duplicadas se a API falhar em retornar a lista.
        ativos_locais = Anuncios.query.filter(Anuncios.boost_end_at > agora).count()
        true_active_count = max(len(boosted_ids), ativos_locais)
        
        return true_active_count

    def run_boost_cycle(self):
        """Lógica principal do Worker: Sincroniza, verifica slots e impulsiona o próximo."""
        from config.redis_config import redis_conn
        lock = None
        try:
            lock = redis_conn.lock("shopee_boost_cycle_lock", timeout=600, blocking_timeout=1)
            if not lock.acquire(blocking=False):
                logger.warning("Ciclo de boost já está em andamento. Ignorando execução sobreposta.")
                return "Já em andamento"
        except Exception as e:
            logger.warning(f"Aviso Redis Lock: {e}")
            lock = None

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
                    
                    # Trata erro de cooldown de 240min (do not bump same product under 240min)
                    if "240min" in msg.lower() or "cooldown" in msg.lower() or "under 240" in msg.lower():
                        candidate.last_boost_at = get_br_now()
                    
                    # Trata erro de limite de slots (reached shop's bump slot limit)
                    if "limit" in msg.lower() or "limite" in msg.lower() or "slot" in msg.lower():
                        logger.warning(f"Limite de slots atingido na Shopee ao tentar impulsionar {candidate.nome}. Interrompendo loop.")
                        break
                else:
                    candidate.last_boost_at = get_br_now()
                    candidate.boost_end_at = get_br_now() + timedelta(hours=4)
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
    from flask import current_app

    # O current_app já tem o contexto no worker
    controller = BoostController()
    result = controller.run_boost_cycle()
    # print(f"--- [RQ BOOST] {result} ---")
