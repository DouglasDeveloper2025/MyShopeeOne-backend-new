from flask import Blueprint, request, jsonify, g
import jwt
import os
import re
from datetime import datetime, timedelta

from model.shopeeModel import db, Usuario

user_bp = Blueprint("user", __name__)

from middleware.authMiddleware import token_required, admin_required


def _get_secret():
    return os.environ.get("JWT_SECRET_KEY", "shopee-price-manager-jwt-secret-2026")


def _generate_token(user: Usuario) -> str:
    payload = {
        "user_id": user.id,
        "email": user.email,
        "role": user.role,
        "exp": datetime.utcnow() + timedelta(hours=24),
    }
    return jwt.encode(payload, _get_secret(), algorithm="HS256")


# ───────────────────── REGISTER ─────────────────────
@user_bp.route("/user/register", methods=["POST"])
def register():
    """Cadastro de novo usuário. O primeiro usuário é admin + ativo automaticamente."""
    dados = request.get_json()
    nome = (dados.get("nome") or "").strip()
    email = (dados.get("email") or "").strip().lower()
    senha = dados.get("senha") or ""

    if not nome or not email or not senha:
        return (
            jsonify(
                {"status": "erro", "mensagem": "Nome, email e senha são obrigatórios"}
            ),
            400,
        )

    # Validação básica de email
    email_regex = r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
    if not re.match(email_regex, email):
        return (
            jsonify({"status": "erro", "mensagem": "Por favor, insira um e-mail válido"}),
            400,
        )

    if len(senha) < 6:
        return (
            jsonify(
                {
                    "status": "erro",
                    "mensagem": "A senha deve ter no mínimo 6 dígitos",
                }
            ),
            400,
        )

    if Usuario.query.filter_by(email=email).first():
        return (
            jsonify({"status": "erro", "mensagem": "Este email já está cadastrado"}),
            409,
        )

    user = Usuario(nome=nome, email=email)
    user.set_senha(senha)

    # Lógica de Role e Ativação
    total_users = Usuario.query.count()
    if total_users == 0:
        user.role = "admin"
        user.ativo = True
    else:
        user.role = "operador"
        user.ativo = False

    db.session.add(user)
    db.session.commit()

    if user.ativo:
        return (
            jsonify(
                {
                    "status": "sucesso",
                    "mensagem": "Conta criada com sucesso! Você já pode fazer login.",
                    "user": user.to_dict(),
                }
            ),
            201,
        )
    else:
        return (
            jsonify(
                {
                    "status": "sucesso",
                    "mensagem": "Conta criada! Aguarde a aprovação do administrador para acessar o sistema.",
                    "pendente": True,
                }
            ),
            201,
        )


# ───────────────────── LOGIN ─────────────────────
@user_bp.route("/user/login", methods=["POST"])
def login():
    """Autentica o usuário e retorna o JWT."""
    dados = request.get_json()
    email = (dados.get("email") or "").strip().lower()
    senha = dados.get("senha") or ""

    if not email or not senha:
        return (
            jsonify({"status": "erro", "mensagem": "Email e senha são obrigatórios"}),
            400,
        )

    user = Usuario.query.filter_by(email=email).first()

    if not user or not user.verificar_senha(senha):
        return jsonify({"status": "erro", "mensagem": "Email ou senha incorretos"}), 401

    if not user.ativo:
        return (
            jsonify(
                {
                    "status": "erro",
                    "mensagem": "Sua conta ainda não está ativa. Aguarde a aprovação do administrador.",
                    "inativo": True,
                }
            ),
            403,
        )

    token = _generate_token(user)
    return jsonify({"status": "sucesso", "token": token, "user": user.to_dict()}), 200


# ───────────────────── ME ─────────────────────
@user_bp.route("/user/me", methods=["GET"])
@token_required
def get_me():
    """Retorna dados do usuário autenticado."""
    return jsonify({"status": "sucesso", "user": g.current_user.to_dict()}), 200


@user_bp.route("/user/me", methods=["PUT"])
@token_required
def update_me():
    """Atualiza o perfil do próprio usuário."""
    dados = request.get_json()
    user = g.current_user

    nome = (dados.get("nome") or "").strip()
    email = (dados.get("email") or "").strip().lower()
    senha = dados.get("senha") or ""

    if nome:
        user.nome = nome

    if email and email != user.email:
        # Check if email is already taken
        existente = Usuario.query.filter_by(email=email).first()
        if existente:
            return (
                jsonify({"status": "erro", "mensagem": "Este email já está em uso"}),
                409,
            )
        user.email = email

    if senha:
        if len(senha) < 6:
            return (
                jsonify(
                    {
                        "status": "erro",
                        "mensagem": "A senha deve ter no mínimo 6 caracteres",
                    }
                ),
                400,
            )
        user.set_senha(senha)

    db.session.commit()

    # Re-generate token since email or name might have changed
    novo_token = _generate_token(user)

    return (
        jsonify(
            {
                "status": "sucesso",
                "mensagem": "Perfil atualizado com sucesso",
                "user": user.to_dict(),
                "token": novo_token,
            }
        ),
        200,
    )


# ───────────────────── ADMIN: LIST USERS ─────────────────────
@user_bp.route("/user/admin/users", methods=["GET"])
@token_required
@admin_required
def list_users():
    """Lista todos os usuários do sistema (somente admin)."""
    users = Usuario.query.order_by(Usuario.criado_em.desc()).all()
    return jsonify({"status": "sucesso", "users": [u.to_dict() for u in users]}), 200


# ───────────────────── ADMIN: UPDATE USER ─────────────────────
@user_bp.route("/user/admin/users/<int:user_id>", methods=["PUT"])
@token_required
@admin_required
def update_user(user_id):
    """Atualiza role e/ou status de um usuário (somente admin)."""
    user = Usuario.query.get(user_id)
    if not user:
        return jsonify({"status": "erro", "mensagem": "Usuário não encontrado"}), 404

    dados = request.get_json()

    if "ativo" in dados:
        user.ativo = bool(dados["ativo"])

    if "role" in dados and dados["role"] in ("admin", "operador"):
        # Não permite remover o próprio admin
        if user.id == g.current_user.id and dados["role"] != "admin":
            return (
                jsonify(
                    {
                        "status": "erro",
                        "mensagem": "Você não pode remover seu próprio acesso admin",
                    }
                ),
                400,
            )
        user.role = dados["role"]

    if "permissoes" in dados and isinstance(dados["permissoes"], dict):
        user.permissoes = dados["permissoes"]

    db.session.commit()
    return jsonify({"status": "sucesso", "user": user.to_dict()}), 200


# ───────────────────── ADMIN: DELETE USER ─────────────────────
@user_bp.route("/user/admin/users/<int:user_id>", methods=["DELETE"])
@token_required
@admin_required
def delete_user(user_id):
    """Remove um usuário do sistema (somente admin)."""
    user = Usuario.query.get(user_id)
    if not user:
        return jsonify({"status": "erro", "mensagem": "Usuário não encontrado"}), 404

    if user.id == g.current_user.id:
        return (
            jsonify(
                {
                    "status": "erro",
                    "mensagem": "Você não pode excluir sua própria conta",
                }
            ),
            400,
        )

    db.session.delete(user)
    db.session.commit()
    return jsonify({"status": "sucesso", "mensagem": "Usuário removido"}), 200
