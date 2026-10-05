import time, httpx
from app.config import PRODUCT_API_URL,PRODUCT_API_KEY,PAGE_SIZE
def fetch_all_products():
    products=[]; page=1
    while True:
        r=httpx.get(f"{PRODUCT_API_URL}/products",params={"page":page,"page_size":PAGE_SIZE},headers={"X-API-Key":PRODUCT_API_KEY},timeout=30)
        if r.status_code in (429,500,502,503,504):
            time.sleep(1)
            r=httpx.get(f"{PRODUCT_API_URL}/products",params={"page":page,"page_size":PAGE_SIZE},headers={"X-API-Key":PRODUCT_API_KEY},timeout=30)
        r.raise_for_status(); p=r.json(); products.extend(p["products"])
        if not p["has_next"]: return products
        page=p["next_page"]
