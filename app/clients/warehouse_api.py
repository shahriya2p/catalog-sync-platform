import time, httpx
from app.config import WAREHOUSE_API_URL,WAREHOUSE_API_KEY,BATCH_SIZE
def send_products(products):
    results=[]
    for start in range(0,len(products),BATCH_SIZE):
        batch=products[start:start+BATCH_SIZE]; payload={"products":batch}
        r=httpx.post(f"{WAREHOUSE_API_URL}/products/batch",json=payload,headers={"X-API-Key":WAREHOUSE_API_KEY},timeout=30)
        if r.status_code in (429,500,502,503,504):
            time.sleep(1)
            r=httpx.post(f"{WAREHOUSE_API_URL}/products/batch",json=payload,headers={"X-API-Key":WAREHOUSE_API_KEY},timeout=30)
        r.raise_for_status(); results.append(r.json())
    return results
