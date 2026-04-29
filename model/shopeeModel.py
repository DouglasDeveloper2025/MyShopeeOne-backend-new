from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import pytz
import bcrypt

# Fuso horário padrão
tz_br = pytz.timezone("America/Sao_Paulo")

def get_br_now():
    """Retorna o horário de Brasília sem fuso (naive)."""
    return datetime.now(tz_br).replace(tzinfo=None)

# Instância do SQLAlchemy que será vinculada ao app no main.py
db = SQLAlchemy()


class Usuario(db.Model):
    """Entidade que representa um usuário do sistema."""
    __tablename__ = "usuarios"

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    senha_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="operador")  # 'admin' ou 'operador'
    ativo = db.Column(db.Boolean, default=False, nullable=False)
    criado_em = db.Column(db.DateTime, default=get_br_now)
    permissoes = db.Column(db.JSON, nullable=False, default={})

    def set_senha(self, senha: str):
        self.senha_hash = bcrypt.hashpw(senha.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    def verificar_senha(self, senha: str) -> bool:
        return bcrypt.checkpw(senha.encode("utf-8"), self.senha_hash.encode("utf-8"))

    def to_dict(self):
        # Default permissions se for vazio ou None
        default_perms = {
            "update_price": True,
            "view_history": False,
            "view_promotions": True,
            "view_settings": False
        }
        
        # Se for admin, concede tudo implicitamente
        if self.role == "admin":
            perms = {k: True for k in default_perms.keys()}
        else:
            perms = self.permissoes if self.permissoes else default_perms
            # Garante que todas as chaves existam mesmo se o JSON antigo não tiver
            perms = {**default_perms, **(perms if isinstance(perms, dict) else {})}

        return {
            "id": self.id,
            "nome": self.nome,
            "email": self.email,
            "role": self.role,
            "ativo": self.ativo,
            "criado_em": self.criado_em.isoformat() if self.criado_em else None,
            "permissoes": perms
        }

    def __repr__(self):
        return f"<Usuario {self.email} [{self.role}]>"

class Configuracoes(db.Model):
    __tablename__ = "configuracoes"
    id = db.Column(db.Integer, primary_key=True)
    dias_espera_simples = db.Column(db.Integer, default=15)
    # Novos campos de agendamento
    hora_sincronizacao = db.Column(db.Integer, default=0)
    minuto_sincronizacao = db.Column(db.Integer, default=15)
    intervalo_refresh_token = db.Column(db.Integer, default=230) # minutos (padrão ~3h50m)

class HistoricoPreco(db.Model):
    """
    Entidade que representa o histórico de alteração de preços de um produto.
    """

    __tablename__ = "historico_precos"

    id = db.Column(db.Integer, primary_key=True)
    shopee_item_id = db.Column(db.String(255), nullable=False)
    shopee_model_id = db.Column(db.String(255), nullable=True, default="0")
    nome_produto = db.Column(db.String(255), nullable=True)
    preco_anterior = db.Column(db.Float, nullable=False)
    preco_atual = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(50), nullable=False)  # 'success' ou 'error'
    mensagem = db.Column(db.Text, nullable=True)

    sku = db.Column(db.String(255), nullable=True)
    origem = db.Column(db.String(50), nullable=True) # 'Anuncios', 'Promocoes', 'Alertas'
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuarios.id"), nullable=True)
    criado_em = db.Column(db.DateTime, default=get_br_now)

    usuario = db.relationship("Usuario", backref="historicos", lazy=True)

    def __repr__(self):
        return f"<HistoricoPreco {self.shopee_item_id} [{self.status}]: {self.preco_anterior} -> {self.preco_atual}>"


class NotificacaoSistema(db.Model):
    """
    Tabela para armazenar notificações do sistema, como alertas de bloqueio de 15 dias.
    """
    __tablename__ = "notificacoes_sistema"
    id = db.Column(db.Integer, primary_key=True)
    tipo = db.Column(db.String(50), nullable=False)  # 'bloqueio', 'desbloqueio', 'info', 'sucesso'
    shopee_item_id = db.Column(db.String(255), nullable=True)
    shopee_model_id = db.Column(db.String(255), nullable=True)
    sku = db.Column(db.String(255), nullable=True)
    titulo = db.Column(db.String(255), nullable=False)
    mensagem = db.Column(db.Text, nullable=False)
    lida = db.Column(db.Boolean, default=False)
    criado_em = db.Column(db.DateTime, default=get_br_now)

    def to_dict(self):
        return {
            "id": self.id,
            "tipo": self.tipo,
            "itemId": self.shopee_item_id,
            "modelId": self.shopee_model_id,
            "sku": self.sku,
            "titulo": self.titulo,
            "mensagem": self.mensagem,
            "lida": self.lida,
            "criado_em": self.criado_em.strftime("%d/%m/%Y %H:%M:%S") if self.criado_em else ""
        }



class IntegracaoShopee(db.Model):
    """
    Entidade que representa a integração com a API da Shopee.
    """

    __tablename__ = "integracoes_shopee"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), unique=True)
    shop_id = db.Column(db.String(100))
    partner_id = db.Column(db.String(100))
    partner_key = db.Column(db.String(100))
    refresh_token = db.Column(db.String(200))
    last_access_token = db.Column(db.String(500))
    code = db.Column(db.String(100))
    status = db.Column(db.String(50))
    expire_in = db.Column(db.Integer, default=14400)
    last_access_update_at = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return f"<IntegracaoShopee {self.name}>"


class Anuncios(db.Model):
    """
    Entidade Pai (Anuncio/Item) que pode ter várias variações.
    """
    __tablename__ = "anuncios"
    id = db.Column(db.Integer, primary_key=True)
    shopee_item_id = db.Column(db.String(255), unique=True, nullable=False)
    nome = db.Column(db.String(255), index=True, nullable=False)
    sku_pai = db.Column(db.String(255), index=True, nullable=True)
    created_at = db.Column(db.DateTime, default=get_br_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=get_br_now, nullable=False)

    # Relacionamento com as variações
    variacoes = db.relationship("Produtos", backref="anuncio", lazy=True, cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "shopee_item_id": self.shopee_item_id,
            "nome": self.nome,
            "sku": self.sku_pai,
            "variacoes": [v.to_dict() for v in self.variacoes]
        }

    def __repr__(self):
        return f"<Anuncio {self.shopee_item_id}: {self.nome}>"


class Produtos(db.Model):
    """
    Entidade Filho (Variação/Model) que pertence a um Anúncio Pai.
    """
    __tablename__ = "produtos"
    id = db.Column(db.Integer, primary_key=True)
    anuncio_id = db.Column(db.Integer, db.ForeignKey("anuncios.id"), nullable=False)
    shopee_item_id = db.Column(db.String(255), index=True, nullable=False) # Redundante para performance
    shopee_model_id = db.Column(db.String(255), nullable=True, default="0")
    nome_variacao = db.Column(db.String(255), index=True, nullable=True)
    sku = db.Column(db.String(255), index=True, nullable=True)
    preco_base = db.Column(db.Float, nullable=False)
    preco_promocional = db.Column(db.Float, nullable=True) # Cache da promoção
    promotion_id = db.Column(db.String(100), index=True, nullable=True) # Cache do ID de promoção
    ean = db.Column(db.String(100), nullable=True)
    situacao = db.Column(db.String(50), nullable=True) # Ativo ou Inativo


    created_at = db.Column(db.DateTime, default=get_br_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=get_br_now, nullable=False)
    preco_modificado_em = db.Column(db.DateTime, nullable=True) # Exclusivo para trava de 15 dias
    notificado_desbloqueio = db.Column(db.Boolean, default=False) # Flag para evitar notificações duplicadas

    __table_args__ = (
        db.UniqueConstraint("shopee_item_id", "shopee_model_id", name="uq_item_model"),
    )

    def __repr__(self):
        return f"<Variação {self.shopee_model_id} do Item {self.shopee_item_id}>"

    def to_dict(self):
        return {
            "id": self.id,
            "modelId": self.shopee_model_id,
            "nome_variacao": self.nome_variacao,
            "sku": self.sku,
            "price_base": self.preco_base,
            "price_promo": self.preco_promocional,
            "promotion_id": self.promotion_id,
            "ean": self.ean,
            "situacao": self.situacao,
            "preco_modificado_em": self.preco_modificado_em.isoformat() if self.preco_modificado_em else None
        }

class Promocoes(db.Model):
    """
    Entidade que representa as Campanhas de Promoção (Discounts) da Shopee.
    """
    __tablename__ = "promocoes"
    id = db.Column(db.Integer, primary_key=True)
    discount_id = db.Column(db.BigInteger, unique=True, nullable=False)
    discount_name = db.Column(db.String(255), nullable=False)
    start_time = db.Column(db.DateTime, nullable=False)
    end_time = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(50)) # 'upcoming', 'ongoing', 'expired'
    updated_at = db.Column(db.DateTime, default=get_br_now)

    def __repr__(self):
        return f"<Promocao {self.discount_id}: {self.discount_name}>"
