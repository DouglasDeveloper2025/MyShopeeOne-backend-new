import os
from flask import Flask
from model.shopeeModel import db, IntegracaoShopee
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
database_url = os.environ.get("DATABASE_URL")
if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
db.init_app(app)

with app.app_context():
    count = IntegracaoShopee.query.count()
    print(f"Integracao Count: {count}")
    if count > 0:
        integ = IntegracaoShopee.query.first()
        print(f"Shop ID: {integ.shop_id}")
        print(f"Status: {integ.status}")
