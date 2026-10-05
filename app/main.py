from app.services.catalogue_export import export_catalogue
from app.services.warehouse_sync import sync_catalogue
def run():
    export_path=export_catalogue(); result=sync_catalogue(export_path); print({"export":export_path,"result":result})
if __name__=="__main__": run()
