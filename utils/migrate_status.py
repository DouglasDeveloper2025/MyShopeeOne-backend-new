import os
from flask import Flask
from model.shopeeModel import db
from sqlalchemy import text
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
database_url = os.environ.get("DATABASE_URL")
if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
db.init_app(app)

with app.app_context():
    try:
        db.session.execute(text("ALTER TABLE anuncios ADD COLUMN status VARCHAR(50) DEFAULT 'NORMAL'"))
        db.session.commit()
        print("Coluna status adicionada com sucesso.")
    except Exception as e:
        db.session.rollback()
        print(f"Erro ao adicionar coluna status: {e}")
