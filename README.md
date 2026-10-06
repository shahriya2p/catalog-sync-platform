# Product Catalogue Integration (PIM → WMS)

Daily synchronisation of the product catalogue from the Product Information
Management system into the Warehouse Management System.

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — the design, the diagram, and why it is this way.
- **[UNFINISHED.md](UNFINISHED.md)** — what is not done, the risk, and what I would do next.

The implementation runs end to end locally with no AWS account: object storage
is the local filesystem and run state is SQLite, both behind the same interfaces
the S3 and DynamoDB implementations use.

---

## Quick start

Prerequisites: Docker (for the supplied mock services) and Python 3.10+.

```bash
# 1. Start the mock PIM and WMS (unmodified, as supplied)
docker compose up --build -d          # PIM on :8001, WMS on :8002

# 2. Install
python -m venv .venv
source .venv/bin/activate             # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. Test
pytest

# 4. Run a full synchronisation
python -m app.main run
```

A full run of the 12,000-product mock catalogue takes about 8 seconds and ends
with a JSON summary on stdout:

```json
{
  "run_id": "20261005T0931Z-3f9c",
  "status": "COMPLETED",
  "export": { "product_count": 12000, "page_count": 24, "pages_failed": [], "complete": true },
  "delivery": { "accepted": 11988, "rejected": 12, "unknown": 0, "batches_sent": 120 },
  "metrics": { "PimRequests": 27, "PimRetries": 3, "WmsRequests": 139, "WmsRetries": 19 }
}
```

The 12 rejections are expected: the mock WMS rejects every SKU ending in `999`
as a business validation failure. `PimRetries` and `WmsRetries` are also
expected: the mocks inject a failure on a fixed request cadence.

---

## Commands

```bash
python -m app.main run [--run-id ID]      # start a new run (the default with no arguments)
python -m app.main resume RUN_ID          # continue a failed or interrupted run
python -m app.main export RUN_ID          # run only the export stage (the ECS task in AWS)
python -m app.main status [RUN_ID]        # is it running, completed, partial or failed?
python -m app.main reconcile RUN_ID --confirm-resend-unknown
```

Options (accepted before or after the subcommand): `--page-size`, `--batch-size`,
`--pim-rps`, `--wms-rps`, `--log-level`, `--log-format {json,text}`.

Exit codes, so a scheduler can act on them:

| Code | Meaning |
|---|---|
| `0` | `COMPLETED` — every product reached a definite outcome |
| `2` | `PARTIAL` — needs attention; successful work is preserved and `resume` continues it |
| `1` | `FAILED` — no usable export was produced |

### Try the interesting behaviour

**Resume does not redo successful work.** Run twice with the same id:

```bash
python -m app.main run --run-id demo-1
python -m app.main resume demo-1
```

The second run reports `"skipped_already_delivered": 12000`, `"batches_sent": 0`
and `PagesSkippedAlreadyStored: 24`. No product is sent to the WMS again.

**Interrupt a run and continue it.** Press Ctrl+C during a run, then:

```bash
python -m app.main status <run-id>    # shows RUNNING/PARTIAL with page and batch counts
python -m app.main resume <run-id>    # continues from the last checkpoint
```

**See the durable artefacts.**

```bash
ls runtime/s3/raw/<run-id>/pages/     # one immutable JSON object per PIM page
cat runtime/s3/exports/<run-id>/manifest.json
sqlite3 runtime/state/catalogue_sync.db "select state, count(*) from ledger group by state"
```

**Ambiguous outcomes are never resent automatically.** If a WMS request times
out, those products are recorded `UNKNOWN`, the run ends `PARTIAL` (exit 2), and
`reconcile` refuses to act without `--confirm-resend-unknown`, because resending
them is the one operation that can create a real duplicate in the warehouse.
See ARCHITECTURE.md section 8.

---

## Configuration

Everything is environment driven; the defaults match the supplied mocks, and the
documented API limits are validated rather than trusted (a page size above 500
or a batch size above 100 is a startup error, not a runtime 400/413).

| Variable | Default | Notes |
|---|---|---|
| `PRODUCT_API_URL` / `WAREHOUSE_API_URL` | `http://localhost:8001` / `:8002` | |
| `PRODUCT_API_KEY` / `WAREHOUSE_API_KEY` | the documented mock keys | |
| `PRODUCT_API_KEY_SECRET_ID` / `WAREHOUSE_API_KEY_SECRET_ID` | unset | When set, the key is read from Secrets Manager instead. Nothing contacts AWS when unset. |
| `PAGE_SIZE` / `BATCH_SIZE` | `500` / `100` | Hard API limits; validated. |
| `PIM_RPS` / `WMS_RPS` | `10` / `20` | Documented rate ceilings. |
| `PIM_CONCURRENCY` / `WMS_CONCURRENCY` | `8` / `16` | Enough in-flight requests to reach the rate limit at 3 s latency. |
| `RETRY_MAX_ATTEMPTS` / `RETRY_BASE_DELAY_SECONDS` / `RETRY_MAX_DELAY_SECONDS` | `6` / `0.5` / `30` | Bounded exponential backoff with full jitter. |
| `STORAGE_BACKEND` | `local` | `local` or `s3` (`S3_BUCKET`, `S3_PREFIX`, `S3_KMS_KEY_ID`). |
| `STATE_BACKEND` | `sqlite` | `sqlite` or `dynamodb` (`RUNS_TABLE`, `PAGES_TABLE`, `BATCHES_TABLE`, `LEDGER_TABLE`, `EXCEPTIONS_TABLE`). |
| `LOCAL_STORAGE_ROOT` / `SQLITE_PATH` / `SCRATCH_DIR` | under `runtime/` | Local-only paths. |
| `RETENTION_DAYS` | `90` | Recorded in the manifest; enforced by the S3 lifecycle rule. |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `json` | |
| `EMIT_EMF_METRICS` | `false` | `true` emits CloudWatch Embedded Metric Format. |
| `RESEND_UNKNOWN` | `false` | Never change this to `true` as a default; see ARCHITECTURE.md section 8. |

No secrets are committed. The mock keys above are the documented local test
values, not credentials.

---

## Tests

```bash
pytest                      # 187 tests, about 20 seconds
pytest -q tests/test_warehouse_sync.py    # duplicate protection and partial failure
pytest -q tests/test_state_backends.py    # the same state contract on SQLite and DynamoDB
```

The tests drive the **supplied mock services in process** (via Starlette's
`TestClient`), so they exercise the real pagination, the real injected
throttling and the real partial-success responses with no ports and no
containers. The mock modules are loaded fresh per test so their request
counters make failures reproducible. Their behaviour is not modified.

What the suite is actually asserting:

| Area | Examples |
|---|---|
| Rate limiting | sustained rate never exceeds the limit (fake clock); one bucket is shared across threads; every retry consumes a token |
| Error classification | 429/5xx retried, 4xx not retried, a read timeout on a **write** is ambiguous and never repeated, a connect failure is safe to repeat |
| Backoff | full jitter within the exponential bound, capped, `Retry-After` honoured and still capped |
| Export | 12,000 products across 24 pages; raw pages retained; manifest checksum and completeness; a failed page does not lose the others and is retried on resume; 401 aborts instead of retrying 2,000 times |
| Delivery | batches never exceed 100; accepted/rejected/unreported split per SKU; rejects not retried; transient failure returns SKUs to `PENDING`; ambiguous outcome recorded `UNKNOWN` and never resent; a crashed in-flight batch becomes `UNKNOWN` |
| Duplicate protection | nothing is ever written to the WMS again after it was accepted (the recording client checks request *and* response ordering); a second pass sends zero products; a terminal ledger state cannot be moved backwards; concurrent claims produce exactly one winner |
| Backend parity | the state contract passes identically on SQLite and on DynamoDB (moto) |
| AWS adapters | S3 object store against moto; SQS page fan-out; the SQS worker reports per-message failures |

`moto` (in `requirements.txt`) is only needed for the AWS adapter tests;
they skip themselves if it is absent, so `pytest` passes with
`requirements.txt` alone.

---

## Infrastructure

```bash
cd infra/terraform
terraform init -backend=false
terraform fmt -check
terraform validate
```

`validate` passes. Nothing has been applied — there is no AWS account in play —
so `plan` is untested; see UNFINISHED.md.

To deploy you would build and push the image, copy
`terraform.tfvars.example` to `terraform.tfvars`, fill in the image URI,
subnets, security group and API URLs, apply, and then write the two API keys
into the Secrets Manager secrets the module creates (Terraform never holds a
key, so none appears in state).

### The container image

One image serves the ECS exporter task, the Lambda workers and a local run; the
entrypoint dispatches on `AWS_LAMBDA_RUNTIME_API`, so there is a single artefact
to build and promote.

```bash
docker build -t catalogue-sync .
docker run --rm --network host \
  -e PRODUCT_API_URL=http://localhost:8001 \
  -e WAREHOUSE_API_URL=http://localhost:8002 \
  catalogue-sync run
```

This was verified: the image builds and completes a full 12,000-product run in
CLI mode. The Lambda mode (`awslambdaric` with the handler as the image command)
has not been invoked, because that needs Lambda; see UNFINISHED.md item 2.

---

## Repository layout

```
app/
  cli.py, main.py            entry points (run, resume, export, status, reconcile)
  config.py                  typed settings; validates the documented API limits
  models.py                  run/page/batch/SKU states, batch results
  observability.py           JSON logging with run context, metrics (EMF in AWS)
  aws_handlers.py            Lambda/ECS entry points over the same services
  clients/
    rate_limiter.py          shared token bucket
    retry.py                 transient vs permanent vs ambiguous, backoff with jitter
    product_api.py           PIM paging
    warehouse_api.py         WMS batching, partial success, ambiguity
  services/
    catalogue_export.py      page checkpoints, streamed CSV, manifest
    warehouse_sync.py        transform, batching, ledger, recovery
    runner.py                run lifecycle and final status
  storage/                   object store interface, local and S3
  state/                     run state interface, SQLite and DynamoDB
infra/terraform/             S3, DynamoDB, SQS, ECS, Lambda, Step Functions, IAM, alarms
mock-services/               supplied PIM and WMS mocks (unmodified)
tests/
runtime/                     local output (gitignored)
```

The mock external APIs are part of the test environment and have not been
modified.
