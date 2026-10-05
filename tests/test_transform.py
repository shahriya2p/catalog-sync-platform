from app.services.warehouse_sync import transform
def test_transform():
    row={"id":"P0000001","name":"Test Product","category":"SPICES","price":"249.50","currency":"INR","updated_at":"2026-09-25T08:30:00Z"}
    result=transform(row); assert result["sku"]=="P0000001"; assert result["selling_price"]==249.50; assert result["category_code"]=="SPICES"
