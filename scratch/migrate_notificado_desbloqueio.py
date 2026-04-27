import os
import sys

# Adicionar o diretório atual ao path para importar os modelos
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app
from model.shopeeModel import db
from sqlalchemy import text

def migrate():
    with app.app_context():
        try:
            print("--- Iniciando migracao: adicionando coluna notificado_desbloqueio ---")
            
            # Comando SQL para adicionar a coluna se ela não existir
            sql = text("ALTER TABLE produtos ADD COLUMN IF NOT EXISTS notificado_desbloqueio BOOLEAN DEFAULT FALSE;")
            
            db.session.execute(sql)
            db.session.commit()
            
            print("Sucesso! Coluna adicionada ou ja existente.")
        except Exception as e:
            db.session.rollback()
            print(f"Erro na migracao: {e}")

if __name__ == "__main__":
    migrate()
