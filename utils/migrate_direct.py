
import os
import psycopg2
from dotenv import load_dotenv

# Carrega as variáveis do .env
load_dotenv()

def migrate_direct():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL não encontrada no .env")
        return

    # Corrige o prefixo se necessário (postgres:// para postgresql://)
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    print("Conectando ao banco de dados...")
    try:
        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        cur = conn.cursor()
        
        # Lista de colunas para adicionar
        columns = [
            ("hora_sincronizacao", "INTEGER DEFAULT 0"),
            ("minuto_sincronizacao", "INTEGER DEFAULT 15"),
            ("intervalo_refresh_token", "INTEGER DEFAULT 230")
        ]
        
        for col_name, col_type in columns:
            try:
                print(f"Tentando adicionar coluna '{col_name}'...")
                cur.execute(f"ALTER TABLE configuracoes ADD COLUMN {col_name} {col_type}")
                print(f"Coluna '{col_name}' adicionada.")
            except Exception as e:
                if "already exists" in str(e).lower():
                    print(f"Coluna '{col_name}' já existe. Pulando...")
                else:
                    print(f"Erro ao adicionar '{col_name}': {e}")
        
        # Garante que exista ao menos uma linha
        cur.execute("SELECT COUNT(*) FROM configuracoes")
        count = cur.fetchone()[0]
        if count == 0:
            cur.execute("INSERT INTO configuracoes (dias_espera_simples, hora_sincronizacao, minuto_sincronizacao, intervalo_refresh_token) VALUES (15, 0, 15, 230)")
            print("Linha inicial de configurações criada.")

        cur.close()
        conn.close()
        print("Migração concluída com sucesso!")
        
    except Exception as e:
        print(f"Falha na conexão ou execução: {e}")

if __name__ == "__main__":
    migrate_direct()
