from functools import wraps
from flask import request, jsonify, g
import jwt
import os

from model.shopeeModel import Usuario


def _get_secret():
    return os.environ.get("JWT_SECRET_KEY", "shopee-price-manager-jwt-secret-2026")


def token_required(f):
    """Decorator que valida o JWT e injeta current_user em flask.g"""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None

        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1]

        if not token:
            return jsonify({"status": "erro", "mensagem": "Token de autenticação não fornecido"}), 401

        try:
            payload = jwt.decode(token, _get_secret(), algorithms=["HS256"])
            user = Usuario.query.get(payload["user_id"])

            if not user:
                return jsonify({"status": "erro", "mensagem": "Usuário não encontrado"}), 401

            if not user.ativo:
                return jsonify({"status": "erro", "mensagem": "Conta desativada pelo administrador"}), 403

            g.current_user = user

        except jwt.ExpiredSignatureError:
            return jsonify({"status": "erro", "mensagem": "Token expirado. Faça login novamente."}), 401
        except jwt.InvalidTokenError:
            return jsonify({"status": "erro", "mensagem": "Token inválido"}), 401

        return f(*args, **kwargs)

    return decorated


def admin_required(f):
    """Decorator que exige role 'admin'. Deve ser usado APÓS @token_required."""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = getattr(g, "current_user", None)
        if not user or user.role != "admin":
            return jsonify({"status": "erro", "mensagem": "Acesso restrito a administradores"}), 403
        return f(*args, **kwargs)

    return decorated


def permission_required(permission_name):
    """Decorator que exige uma permissão específica. Deve ser usado APÓS @token_required."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            user = getattr(g, "current_user", None)
            if not user:
                return jsonify({"status": "erro", "mensagem": "Autenticação necessária"}), 401
            
            # Admins têm todas as permissões implicitamente
            if user.role == "admin":
                return f(*args, **kwargs)
                
            perms = user.permissoes or {}
            # Se a permissão não for explicitamente True, bloqueia
            if not perms.get(permission_name):
                return jsonify({
                    "status": "erro", 
                    "mensagem": f"Você não tem permissão para realizar esta ação ({permission_name})"
                }), 403
                
            return f(*args, **kwargs)
        return decorated
    return decorator
