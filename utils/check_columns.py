import os
from flask import Flask
from model.shopeeModel import db
from sqlalchemy import inspect
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
database_url = os.environ.get("DATABASE_URL")
if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
db.init_app(app)

with app.app_context():
    inst = inspect(db.engine)
    columns = [c['name'] for c in inst.get_columns('anuncios')]
    print(f"Colunas de anuncios: {columns}")
