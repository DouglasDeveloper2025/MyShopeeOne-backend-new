import hmac
import hashlib
import time
import requests
import json
import logging
from datetime import datetime
from model.shopeeModel import db, IntegracaoShopee

logger = logging.getLogger(__name__)

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
            access_token = self.integracao.last_access_token
            shop_id = int(self.integracao.shop_id)
            common_params["access_token"] = access_token
            common_params["shop_id"] = shop_id
            sign = self._generate_sign(path, timestamp, access_token, shop_id)
        else:
            sign = self._generate_sign(path, timestamp)

        common_params["sign"] = sign
        
        if params:
            common_params.update(params)

        for attempt in range(retries + 1):
            try:
                response = requests.request(
                    method=method,
                    url=url,
                    params=common_params,
                    json=json_data,
                    timeout=30
                )
                
                data = response.json()
                
                # Trata expiração de token (error_token ou similar conforme Shopee API)
                if data.get("error") in ["error_auth", "error_param", "error_token"]:
                    # Se for erro de token, poderíamos tentar renovar aqui, 
                    # mas o background_checker já faz isso.
                    logger.warning(f"Shopee API Auth Error: {data}")
                
                return data
            except Exception as e:
                if attempt == retries:
                    logger.error(f"Shopee API Final Failure: {str(e)}")
                    return {"error": str(e)}
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
