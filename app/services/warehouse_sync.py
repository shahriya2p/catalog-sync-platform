from app.clients.warehouse_api import send_products
import csv
def transform(row):
    return {"sku":row["id"],"description":row["name"],"selling_price":float(row["price"]),"currency":row["currency"],"category_code":row["category"],"source_updated_at":row["updated_at"]}
def sync_catalogue(path):
    products=[]
    with open(path,newline="",encoding="utf-8") as f:
        for row in csv.DictReader(f): products.append(transform(row))
    return send_products(products)
