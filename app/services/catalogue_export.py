import csv, os
from app.config import EXPORT_DIR
from app.clients.product_api import fetch_all_products
def export_catalogue():
    products=fetch_all_products(); os.makedirs(EXPORT_DIR,exist_ok=True); path=os.path.join(EXPORT_DIR,"catalogue.csv")
    with open(path,"w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=["id","name","category","price","currency","updated_at"]); w.writeheader(); w.writerows(products)
    return path
