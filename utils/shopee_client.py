import hmac
import hashlib
import time
import random
import requests
from requests.exceptions import ConnectionError, Timeout, ReadTimeout, ConnectTimeout
import json
import logging
from datetime import datetime
from model.shopeeModel import db, IntegracaoShopee

logger = logging.getLogger(__name__)

# Timeout de conexão (segundos) e timeout de leitura (segundos)
# Separados para tratar cenários diferentes:
# - connect_timeout: tempo máximo para estabelecer conexão TCP
# - read_timeout: tempo máximo para receber a resposta após a conexão ser estabelecida
DEFAULT_CONNECT_TIMEOUT = 15
DEFAULT_READ_TIMEOUT = 45
MAX_RETRIES_TIMEOUT = 3  # Retries extras para erros de timeout

class ShopeeClient:
    def __init__(self, integracao_id=None):
        self.host = "https://partner.shopeemobile.com"
        self._integracao = None
        self._integracao_id = integracao_id

    @property
    def integracao(self):
        if not self._integracao:
            if self._integracao_id:
                self._integracao = db.session.get(IntegracaoShopee, self._integracao_id)
            else:
                self._integracao = IntegracaoShopee.query.filter_by(status="Ativo").first()
        return self._integracao

    def _generate_sign(self, path, timestamp, access_token=None, shop_id=None):
        partner_id = str(self.integracao.partner_id)
        partner_key = self.integracao.partner_key
        
        if access_token and shop_id:
            # Business API Signature
            base_string = f"{partner_id}{path}{timestamp}{access_token}{shop_id}"
        else:
            # Public/Auth API Signature
            base_string = f"{partner_id}{path}{timestamp}"
            
        sign = hmac.new(
            partner_key.encode(), base_string.encode(), hashlib.sha256
        ).hexdigest()
        return sign

    def request(self, method, path, params=None, json_data=None, use_auth=True, retries=2):
        if not self.integracao:
            return {"error": "Integração não configurada ou ativa."}

        timestamp = int(time.time())
        url = f"{self.host}{path}"
        
        common_params = {
            "partner_id": int(self.integracao.partner_id),
            "timestamp": timestamp,
        }

        if use_auth:
            # Garante que o token de acesso seja válido antes de realizar a requisição
            from controller.auth.authShopee import TokenShopee
            tokens_service = TokenShopee()
            creds, err = tokens_service.ensure_valid_token(self.integracao.id)
            if err:
                logger.error(f"Erro ao validar/renovar token Shopee antes da requisição: {err}")
                access_token = self.integracao.last_access_token
                shop_id = int(self.integracao.shop_id)
            else:
                access_token = creds["access_token"]
                shop_id = int(creds["shop_id"])

            common_params["access_token"] = access_token
            common_params["shop_id"] = shop_id
            sign = self._generate_sign(path, timestamp, access_token, shop_id)
        else:
            sign = self._generate_sign(path, timestamp)

        common_params["sign"] = sign
        
        if params:
            common_params.update(params)

        # Para erros de timeout, usamos mais tentativas que o padrão
        max_attempts = max(retries + 1, MAX_RETRIES_TIMEOUT + 1)

        for attempt in range(max_attempts):
            try:
                response = requests.request(
                    method=method,
                    url=url,
                    params=common_params,
                    json=json_data,
                    timeout=(DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT)
                )
                
                data = response.json()
                
                # Trata expiração de token (error_token ou similar conforme Shopee API)
                if data.get("error") in ["error_auth", "error_param", "error_token", "invalid_acceess_token", "invalid_access_token"]:
                    logger.warning(f"Shopee API Auth Error: {data}")
                    # Se obtivermos erro de token, forçamos um refresh e tentamos novamente
                    if use_auth and attempt < retries:
                        logger.info("Forçando a renovação do token Shopee após erro de autenticação...")
                        from controller.auth.authShopee import TokenShopee
                        tokens_service = TokenShopee()
                        
                        # Recarrega o objeto do banco para evitar conflitos/dados obsoletos
                        db.session.refresh(self.integracao)
                        
                        creds, err = tokens_service._refresh_token(self.integracao)
                        if not err and creds:
                            logger.info("Token renovado com sucesso. Atualizando parâmetros e tentando novamente...")
                            access_token = creds["access_token"]
                            shop_id = int(creds["shop_id"])
                            
                            common_params["access_token"] = access_token
                            common_params["shop_id"] = shop_id
                            
                            # Recalcula a assinatura e timestamp para evitar problemas de assinatura expirada
                            new_timestamp = int(time.time())
                            common_params["timestamp"] = new_timestamp
                            common_params["sign"] = self._generate_sign(path, new_timestamp, access_token, shop_id)
                            continue
                
                return data

            except (ConnectTimeout, ConnectionError) as e:
                # Erros de conexão/timeout de conexão — a Shopee pode estar instável
                # Usa backoff exponencial com jitter para evitar thundering herd
                if attempt < MAX_RETRIES_TIMEOUT:
                    base_delay = min(2 ** (attempt + 1), 30)  # 2s, 4s, 8s, max 30s
                    jitter = random.uniform(0, base_delay * 0.5)
                    wait_time = base_delay + jitter
                    logger.warning(
                        f"Shopee API ConnectTimeout/ConnectionError (tentativa {attempt + 1}/{MAX_RETRIES_TIMEOUT + 1}): {str(e)}. "
                        f"Aguardando {wait_time:.1f}s antes de retry..."
                    )
                    time.sleep(wait_time)
                    # Recalcula timestamp e assinatura para o retry (evita assinatura expirada)
                    new_timestamp = int(time.time())
                    common_params["timestamp"] = new_timestamp
                    if use_auth:
                        common_params["sign"] = self._generate_sign(path, new_timestamp, common_params.get("access_token"), common_params.get("shop_id"))
                    else:
                        common_params["sign"] = self._generate_sign(path, new_timestamp)
                    continue
                else:
                    logger.error(f"Shopee API ConnectTimeout/ConnectionError FINAL (após {MAX_RETRIES_TIMEOUT + 1} tentativas): {str(e)}")
                    return {"error": str(e), "error_type": "timeout"}

            except (ReadTimeout, Timeout) as e:
                # Timeout de leitura — conexão foi estabelecida mas resposta demorou demais
                if attempt < MAX_RETRIES_TIMEOUT:
                    base_delay = min(3 ** (attempt + 1), 45)  # 3s, 9s, 27s, max 45s
                    jitter = random.uniform(0, base_delay * 0.3)
                    wait_time = base_delay + jitter
                    logger.warning(
                        f"Shopee API ReadTimeout (tentativa {attempt + 1}/{MAX_RETRIES_TIMEOUT + 1}): {str(e)}. "
                        f"Aguardando {wait_time:.1f}s antes de retry..."
                    )
                    time.sleep(wait_time)
                    # Recalcula timestamp e assinatura
                    new_timestamp = int(time.time())
                    common_params["timestamp"] = new_timestamp
                    if use_auth:
                        common_params["sign"] = self._generate_sign(path, new_timestamp, common_params.get("access_token"), common_params.get("shop_id"))
                    else:
                        common_params["sign"] = self._generate_sign(path, new_timestamp)
                    continue
                else:
                    logger.error(f"Shopee API ReadTimeout FINAL (após {MAX_RETRIES_TIMEOUT + 1} tentativas): {str(e)}")
                    return {"error": str(e), "error_type": "timeout"}

            except Exception as e:
                if attempt >= retries:
                    logger.error(f"Shopee API Final Failure: {str(e)}")
                    return {"error": str(e)}
                # Backoff linear para erros genéricos
                time.sleep(1 * (attempt + 1))
        
        return {"error": "Max retries reached"}

    # --- Endpoints de Boost ---
    
    def get_boosted_list(self):
        """Retorna a lista de itens atualmente impulsionados."""
        return self.request("GET", "/api/v2/product/get_boosted_list")

    def boost_item(self, item_id):
        """Impulsiona um item específico."""
        # Tentamos enviar ambos os formatos para garantir compatibilidade
        resp = self.request("POST", "/api/v2/product/boost_item", json_data={
            "item_id": int(item_id),
            "item_id_list": [int(item_id)]
        })
        logger.info(f"Resposta Boost Manual: {resp}")
        return resp
