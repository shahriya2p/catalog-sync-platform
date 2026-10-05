import os
PRODUCT_API_URL=os.getenv("PRODUCT_API_URL","http://localhost:8001")
WAREHOUSE_API_URL=os.getenv("WAREHOUSE_API_URL","http://localhost:8002")
PRODUCT_API_KEY=os.getenv("PRODUCT_API_KEY","challenge-product-key")
WAREHOUSE_API_KEY=os.getenv("WAREHOUSE_API_KEY","challenge-warehouse-key")
EXPORT_DIR=os.getenv("EXPORT_DIR","./runtime/exports")
PAGE_SIZE=int(os.getenv("PAGE_SIZE","500")); BATCH_SIZE=int(os.getenv("BATCH_SIZE","100"))
