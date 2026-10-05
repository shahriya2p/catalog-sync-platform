import os
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
app=FastAPI(title="Mock Warehouse Management API",version="1.0")
API_KEY=os.getenv("WAREHOUSE_API_KEY","challenge-warehouse-key"); MAX_BATCH_SIZE=100; request_count=0
@app.get("/health")
def health(): return {"status":"ok"}
@app.post("/products/batch")
def process_batch(payload:dict,x_api_key:str|None=Header(default=None)):
    global request_count
    if x_api_key!=API_KEY: raise HTTPException(401,"Invalid API key")
    products=payload.get("products")
    if not isinstance(products,list): raise HTTPException(400,"products must be an array")
    if len(products)>MAX_BATCH_SIZE: raise HTTPException(413,"Maximum batch size is 100")
    request_count+=1
    if request_count%11==0: return JSONResponse(429,{"detail":"Warehouse API rate limit exceeded"},headers={"Retry-After":"1"})
    if request_count%17==0: return JSONResponse(503,{"detail":"Warehouse API temporarily unavailable"},headers={"Retry-After":"2"})
    accepted=[]; rejected=[]
    for item in products:
        sku=item.get("sku")
        if not sku: rejected.append({"sku":None,"reason":"sku is required"})
        elif str(sku).endswith("999"): rejected.append({"sku":sku,"reason":"Invalid warehouse product"})
        else: accepted.append(sku)
    return {"accepted":accepted,"rejected":rejected,"message":"Processed"}
