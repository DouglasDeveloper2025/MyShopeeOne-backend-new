
from app import app
from model.shopeeModel import db
from sqlalchemy import text

def migrate():
    with app.app_context():
        print("Iniciando migração manual de colunas...")
        
        # Lista de colunas para adicionar
        columns = [
            ("hora_sincronizacao", "INTEGER DEFAULT 0"),
            ("minuto_sincronizacao", "INTEGER DEFAULT 15"),
            ("intervalo_refresh_token", "INTEGER DEFAULT 230")
        ]
        
        for col_name, col_type in columns:
            try:
                # Tenta adicionar a coluna
                db.session.execute(text(f"ALTER TABLE configuracoes ADD COLUMN {col_name} {col_type}"))
                db.session.commit()
                print(f"Coluna '{col_name}' adicionada com sucesso.")
            except Exception as e:
                db.session.rollback()
                # Se o erro for que a coluna já existe, ignoramos
                if "already exists" in str(e).lower():
                    print(f"Coluna '{col_name}' já existe. Pulando...")
                else:
                    print(f"Erro ao adicionar '{col_name}': {e}")
        
        # Garante que exista ao menos uma linha de configuração
        try:
            from model.shopeeModel import Configuracoes
            if not Configuracoes.query.first():
                config = Configuracoes(
                    dias_espera_simples=15,
                    hora_sincronizacao=0,
                    minuto_sincronizacao=15,
                    intervalo_refresh_token=230
                )
                db.session.add(config)
                db.session.commit()
                print("Linha inicial de configurações criada.")
        except Exception as e:
            print(f"Erro ao criar linha inicial: {e}")

if __name__ == "__main__":
    migrate()
