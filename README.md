# Product Catalogue Integration

This repository contains the existing implementation of a product catalogue integration.

The implementation currently works for the existing small-scale environment.

You have been asked to take ownership of the solution and prepare it for the next stage of the platform.

See the assessment document supplied separately for the business context, requirements and deliverables.

## Running locally

Prerequisites: Docker Desktop, Python 3.11+, Terraform 1.5+.

Start the mock external systems:

```bash
docker compose up --build
```

Product API: http://localhost:8001 (Swagger: /docs)
Warehouse API: http://localhost:8002 (Swagger: /docs)

Install Python dependencies:

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
pytest
python -m app.main
```

The current implementation uses local filesystem storage for its S3 abstraction so it can be exercised without an AWS account.

## Important

This is an intentionally imperfect existing solution. You are not expected to preserve the current architecture. You may refactor, replace or add components where justified.

The mock external APIs are part of the test environment. Do not modify their behaviour.
