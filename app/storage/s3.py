from pathlib import Path
class S3Store:
    """Local stand-in for the S3 boundary used by the current implementation."""
    def __init__(self,root="./runtime/s3"): self.root=Path(root)
    def put_file(self,source,key):
        destination=self.root/key; destination.parent.mkdir(parents=True,exist_ok=True); destination.write_bytes(Path(source).read_bytes()); return str(destination)
    def get_file(self,key): return self.root/key
