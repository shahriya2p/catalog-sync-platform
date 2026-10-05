from app.clients import product_api
def test_page_size_is_configured(): assert product_api.PAGE_SIZE>0
