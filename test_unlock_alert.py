import sys
import os
from datetime import datetime, timedelta

# Adicionar o diretório atual ao path para evitar problemas de importação
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

try:
    from app import app
    from model.shopeeModel import db, Produtos
except ImportError:
    # Fallback se rodar de fora da pasta backend
    sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    from backend.app import app
    from backend.model.shopeeModel import db, Produtos

def simulate_old_block():
    # Garantir que estamos usando o app correto e o db vinculado a ele
    with app.app_context():
        # Buscar o produto mais recente que teve alteração de preço
        prod = Produtos.query.filter(Produtos.preco_modificado_em.isnot(None)).order_by(Produtos.updated_at.desc()).first()
        
        if not prod:
            print("Nenhum produto com trava de 15 dias encontrado no banco.")
            print("Dica: Altere o preco base de algum produto primeiro para gerar a trava.")
            return

        # Usar a mesma lógica do backend para o tempo de Brasília
        from controller.shopeeUpdate.shopeeUpdateController import ShopeeService
        sc = ShopeeService()
        agora = sc._get_brasilia_time()
        
        # Retroceder a data em 15 dias e 1 hora
        nova_data = agora - timedelta(days=15, hours=1)
        
        print(f"--- Simulando expiracao de bloqueio ---")
        print(f"Produto: {prod.sku or prod.shopee_item_id}")
        print(f"Data original (Brasilia): {prod.preco_modificado_em}")
        print(f"Nova data (simulada): {nova_data}")
        
        prod.preco_modificado_em = nova_data
        db.session.add(prod)
        db.session.commit()
        
        print("\nSucesso! A trava foi retrocedida para 15 dias atras.")
        print("Agora, va no sistema e clique em 'SINCRONIZAR' na pagina de anuncios.")

if __name__ == "__main__":
    simulate_old_block()
