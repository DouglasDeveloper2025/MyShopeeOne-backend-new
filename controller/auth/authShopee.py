import hmac
import hashlib
import time
import requests
import os
from datetime import datetime, timedelta
import pytz
from urllib.parse import quote
from model.shopeeModel import db, IntegracaoShopee

tz_br = pytz.timezone("America/Sao_Paulo")


class TokenShopee:
    def __init__(self):
        self.host_base = "https://partner.shopeemobile.com"
        self.redirect_uri = os.getenv("URL_REDIRECT_SHOPEE")

    def gerar_url_autenticacao(self, integration_name, partner_id, partner_key):
        return self.generate_auth_url(integration_name, partner_id, partner_key)

    def generate_auth_url(self, integration_name, partner_id, partner_key):
        """Gera a URL de autorização da Shopee para o vendedor."""
        try:
            # Busca a primeira integração se não houver nome específico
            if not integration_name or integration_name == "":
                integracao = IntegracaoShopee.query.first()
            else:
                integracao = IntegracaoShopee.query.filter_by(
                    name=integration_name
                ).first()

            if integracao:
                integracao.status = "Pendente"
                integracao.partner_id = str(partner_id)
                integracao.partner_key = str(partner_key)
            else:
                integracao = IntegracaoShopee(
                    name=integration_name or "Loja Principal",
                    status="Pendente",
                    partner_id=str(partner_id),
                    partner_key=str(partner_key),
                )
                db.session.add(integracao)

            db.session.commit()

            path = "/api/v2/shop/auth_partner"
            timestamp = int(time.time())
            base_string = f"{partner_id}{path}{timestamp}".encode()
            sign = hmac.new(
                partner_key.encode(), base_string, hashlib.sha256
            ).hexdigest()

            url = f"{self.host_base}{path}?partner_id={partner_id}&timestamp={timestamp}&sign={sign}&redirect={quote(self.redirect_uri or '')}"
            return {"status": "sucesso", "url": url}
        except Exception as e:
            db.session.rollback()
            return {"status": "erro", "mensagem": f"Erro ao gerar URL: {str(e)}"}

    def obter_tokens_via_callback(self, code, shop_id, integration_id=None):
        return self.get_tokens_via_callback(code, shop_id, integration_id)

    def get_tokens_via_callback(self, code, shop_id, integration_id=None):
        """Obtém os tokens de acesso e refresh após o redirecionamento do callback."""
        try:
            if integration_id:
                integracao = db.session.get(IntegracaoShopee, integration_id)
            else:
                integracao = (
                    IntegracaoShopee.query.filter_by(status="Pendente").first()
                    or IntegracaoShopee.query.first()
                )

            if not integracao:
                return {"status": "erro", "mensagem": "Integração não encontrada"}

            # Salva o code recebido
            integracao.code = code

            path = "/api/v2/auth/token/get"
            timestamp = int(time.time())
            base_string = f"{integracao.partner_id}{path}{timestamp}".encode()
            sign = hmac.new(
                integracao.partner_key.encode(), base_string, hashlib.sha256
            ).hexdigest()

            payload = {
                "code": code,
                "shop_id": int(shop_id),
                "partner_id": int(integracao.partner_id),
            }

            response = requests.post(
                f"{self.host_base}{path}",
                json=payload,
                params={
                    "partner_id": integracao.partner_id,
                    "timestamp": timestamp,
                    "sign": sign,
                },
            )
            data = response.json()

            if "access_token" in data:
                integracao.last_access_token = data["access_token"]
                integracao.refresh_token = data["refresh_token"]
                integracao.shop_id = str(shop_id)
                integracao.status = "Ativo"
                integracao.expire_in = data.get("expire_in", 14400)
                integracao.last_access_update_at = datetime.utcnow()
                db.session.commit()
                return {"status": "sucesso"}

            # Idempotência: Se o erro for 'invalid_code', mas a integração já estiver 'Ativo'
            # e tiver sido atualizada recentemente (últimos 60 segundos), consideramos sucesso.
            # Isso evita erros em chamadas duplicadas do frontend.
            if data.get("error") == "invalid_code" and integracao.status == "Ativo":
                if integracao.last_access_update_at and (
                    datetime.utcnow() - integracao.last_access_update_at
                ) < timedelta(seconds=60):
                    return {"status": "sucesso"}

            return {"status": "erro", "detalhes": data}

        except Exception as e:
            db.session.rollback()
            return {"status": "erro", "mensagem": str(e)}

    def ensure_valid_token(self, integration_id=None):
        """Garante que o token de acesso seja válido, renovando-o se necessário."""
        if integration_id:
            integracao = db.session.get(IntegracaoShopee, integration_id)
        else:
            integracao = IntegracaoShopee.query.first()

        if not integracao:
            # Tenta fallback para env se não existir no banco
            p_id = os.getenv("SHOPEE_PARTNER_ID")
            p_key = os.getenv("SHOPEE_PARTNER_KEY")
            if p_id and p_key:
                return (
                    None,
                    "Integração pendente. Por favor, autorize sua loja Shopee na página de configurações para atualizar preços.",
                )
            return (
                None,
                "A integração com a Shopee não foi encontrada. Configure suas chaves no painel de configurações.",
            )

        if integracao.last_access_token and integracao.last_access_update_at:
            # Usar UTC para evitar problemas de fuso horário que fazem o token parecer válido quando não é
            agora_utc = datetime.utcnow()
            if (agora_utc - integracao.last_access_update_at) < timedelta(
                hours=3, minutes=50
            ):
                return {
                    "access_token": integracao.last_access_token,
                    "shop_id": int(integracao.shop_id),
                    "partner_id": integracao.partner_id,
                    "partner_key": integracao.partner_key,
                }, None

        return self._refresh_token(integracao)

    def _refresh_token(self, integracao):
        """Renova o access_token utilizando o refresh_token."""
        path = "/api/v2/auth/access_token/get"
        timestamp = int(time.time())
        base_string = f"{integracao.partner_id}{path}{timestamp}".encode()
        sign = hmac.new(
            integracao.partner_key.encode(), base_string, hashlib.sha256
        ).hexdigest()

        body = {
            "refresh_token": integracao.refresh_token,
            "shop_id": int(integracao.shop_id),
            "partner_id": int(integracao.partner_id),
        }

        try:
            resp = requests.post(
                f"{self.host_base}{path}",
                json=body,
                params={
                    "partner_id": integracao.partner_id,
                    "timestamp": timestamp,
                    "sign": sign,
                },
            ).json()

            if "access_token" in resp:
                integracao.last_access_token = resp["access_token"]
                integracao.refresh_token = resp["refresh_token"]
                integracao.expire_in = resp.get("expire_in", 14400)
                integracao.last_access_update_at = datetime.utcnow()
                db.session.commit()
                return {
                    "access_token": integracao.last_access_token,
                    "shop_id": int(integracao.shop_id),
                    "partner_id": integracao.partner_id,
                    "partner_key": integracao.partner_key,
                }, None

            return None, f"Erro renovação: {resp}"
        except Exception as e:
            return None, str(e)


def run_token_refresh_job():
    """Job do RQ para renovar o token da Shopee automaticamente."""
    from app import app
    from model.shopeeModel import IntegracaoShopee
    from controller.auth.authShopee import TokenShopee
    from datetime import datetime, timedelta

    with app.app_context():
        integracao = IntegracaoShopee.query.first()
        if not integracao:
            print("--- [RQ TOKEN] Nenhuma integração encontrada para renovar. ---")
            return

        # Verifica se realmente precisa renovar (evita renovações duplicadas se o job rodar colado com outro)
        agora = datetime.utcnow()
        if integracao.last_access_update_at:
            tempo_desde_update = agora - integracao.last_access_update_at
            if tempo_desde_update < timedelta(hours=3, minutes=45):
                print(
                    f"--- [RQ TOKEN] Token ainda é recente ({tempo_desde_update}). Pulando renovação. ---"
                )
                return

        print(
            f"--- [RQ TOKEN] Iniciando renovação automática para: {integracao.name} ---"
        )
        tokens = TokenShopee()
        creds, erro = tokens._refresh_token(integracao)

        if erro:
            print(f"--- [RQ TOKEN ERROR] Falha ao renovar: {erro} ---")
        else:
            print(
                f"--- [RQ TOKEN SUCCESS] Token renovado com sucesso às {datetime.now()} ---"
            )
