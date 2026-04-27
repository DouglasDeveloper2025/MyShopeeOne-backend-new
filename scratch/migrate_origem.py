import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import app
from model.shopeeModel import db
from sqlalchemy import text

def migrate():
    with app.app_context():
        try:
            # Tentar adicionar a coluna 'origem' na tabela 'historico_precos'
            db.session.execute(text("ALTER TABLE historico_precos ADD COLUMN IF NOT EXISTS origem VARCHAR(50)"))
            db.session.commit()
            print("Coluna 'origem' adicionada com sucesso!")
        except Exception as e:
            db.session.rollback()
            print(f"Erro ao adicionar coluna: {e}")

if __name__ == "__main__":
    migrate()
