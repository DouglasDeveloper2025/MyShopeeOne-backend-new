from model.shopeeModel import db, Anuncios, BoostLog, Configuracoes, get_br_now
from utils.shopee_client import ShopeeClient
from datetime import datetime, timedelta
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

        # 1. Reset status de quem não está mais na lista da Shopee
        anuncios_ativos = Anuncios.query.filter(Anuncios.boost_end_at != None).all()
        for anuncio in anuncios_ativos:
            if anuncio.shopee_item_id not in boosted_ids:
                anuncio.boost_end_at = None
                self._log_boost(
                    anuncio.shopee_item_id,
                    "boost_expired",
                    "info",
                    f"Boost de '{anuncio.nome}' expirou na Shopee.",
                )

        # 2. Atualiza status de quem está na lista
        for item in boosted_items:
            item_id = str(item["item_id"])
            anuncio = Anuncios.query.filter_by(shopee_item_id=item_id).first()
            if anuncio:
                # Na v2, cool_down_second é o tempo restante em segundos
                remaining_seconds = item["cool_down_second"]
                end_time = get_br_now() + timedelta(seconds=remaining_seconds)
                anuncio.boost_end_at = end_time
                # Define o início aproximado (4 horas antes do fim)
                anuncio.last_boost_at = end_time - timedelta(hours=4)

        db.session.commit()
        return len(boosted_ids)

    def run_boost_cycle(self):
        """Lógica principal do Worker: Sincroniza, verifica slots e impulsiona o próximo."""
        try:
            active_count = self.sync_boost_status()
            if active_count is False:
                return "Erro na sincronização"

            if active_count >= 5:
                return "Slots cheios"

            slots_available = 5 - active_count

            base_query = (
                Anuncios.query.filter(Anuncios.status.in_(["NORMAL", "ATIVO"]))
                .filter(Anuncios.boost_end_at == None)
                .filter(Anuncios.estoque_total >= 3)
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

            db.session.commit()
            return f"Ciclo concluído. Impulsionados: {len(final_selection)}"

        except Exception as e:
            db.session.rollback()
            self._log_boost(
                None,
                "cycle_error",
                "error",
                f"Erro crítico no ciclo de boost: {str(e)}",
            )
            return f"Erro: {str(e)}"

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
        # Filtra apenas quem NÃO está com boost ativo no momento e tem status permitido
        base_query = (
            Anuncios.query.filter(Anuncios.status.in_(["NORMAL", "ATIVO"]))
            .filter(Anuncios.boost_end_at == None)
            .filter(Anuncios.estoque_total >= 3)
        )

        # Prioridade vem sempre na frente (os que estão na fila de prioridade)
        priority = (
            base_query.filter_by(boost_priority=True)
            .order_by(Anuncios.last_boost_at.asc().nullsfirst())
            .all()
        )

        config = Configuracoes.query.first()
        mode = config.boost_mode if config else "sequential"

        if mode == "sequential":
            normal = (
                base_query.filter_by(boost_priority=False)
                .order_by(
                    Anuncios.last_boost_at.asc().nullsfirst(), Anuncios.sku_pai.asc()
                )
                .all()
            )
        else:
            normal = base_query.filter_by(boost_priority=False).all()
            random.shuffle(normal)

        combined = priority + normal
        return combined[:limit]


def run_boost_job():
    """Job a ser chamado pelo Worker/Scheduler."""
    from flask import current_app

    # O current_app já tem o contexto no worker
    controller = BoostController()
    result = controller.run_boost_cycle()
    # print(f"--- [RQ BOOST] {result} ---")
