import os
from datetime import datetime, timezone
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
app=FastAPI(title="Mock Product Information API",version="1.0")
API_KEY=os.getenv("PRODUCT_API_KEY","challenge-product-key"); TOTAL_PRODUCTS=12000; MAX_PAGE_SIZE=500; request_count=0
categories=["ELECTRONICS","HOME","SPICES","GROCERY","SPORTS"]
def product_for(i):
    return {"id":f"P{i:07d}","name":f"Product {i}","category":categories[(i-1)%5],"price":round(100+((i*1.25)%900),2),"currency":"INR","updated_at":datetime(2026,9,25,8,30,tzinfo=timezone.utc).isoformat().replace("+00:00","Z")}
@app.get("/health")
def health(): return {"status":"ok"}
@app.get("/products")
def products(page:int=1,page_size:int=500,x_api_key:str|None=Header(default=None)):
    global request_count
    if x_api_key!=API_KEY: raise HTTPException(401,"Invalid API key")
    if page<1 or page_size<1 or page_size>MAX_PAGE_SIZE: raise HTTPException(400,"Invalid pagination")
    request_count+=1
    if request_count%13==0: return JSONResponse(429,{"detail":"Rate limit exceeded"},headers={"Retry-After":"1"})
    if request_count%19==0: return JSONResponse(503,{"detail":"Product API temporarily unavailable"},headers={"Retry-After":"2"})
    start=(page-1)*page_size+1; items=[] if start>TOTAL_PRODUCTS else [product_for(i) for i in range(start,min(start+page_size,TOTAL_PRODUCTS+1))]
    has_next=start+len(items)<=TOTAL_PRODUCTS
    return {"products":items,"page":page,"page_size":page_size,"total":TOTAL_PRODUCTS,"has_next":has_next,"next_page":page+1 if has_next else None}
