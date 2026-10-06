# Unfinished work

Written for: the engineering reviewers, as an honest account of what is not done.

Everything below is genuinely incomplete. Where something is deliberately out of
scope rather than unfinished, it says so. The items are ordered by priority.

A note on what "done" means here: the local implementation runs end to end
against the supplied mocks and the test suite covers the behaviour the
requirements turn on (resume, partial failure, ambiguity, duplicate
protection). The gaps are concentrated in two places: **anything that can only
be proven in a real AWS account**, and **scale, which the 12,000-product mock
cannot demonstrate**.

---

## 1. Globally shared rate limiting across delivery workers

**What remains.** The token bucket is per process. The exporter is a single ECS
task, so its PIM bucket is exact. The delivery workers are independent Lambda
invocations, so Terraform divides the WMS budget by the reserved concurrency and
gives each worker a fixed share (`WMS_RPS = 20 / delivery_worker_concurrency`).

**Why that is not good enough.** It is safe but wasteful: with 10 workers
configured and 3 running, the run uses 6 req/s of a 20 req/s budget and takes
over three times longer than it needs to. It also couples a performance
decision (concurrency) to a correctness constraint (the rate ceiling), which is
exactly the kind of coupling that gets broken by a well-meaning change to a
Terraform variable.

**Proposed solution.** A DynamoDB-backed token bucket with short leases: one
item per API holding `tokens` and `last_refill_ms`, updated with a conditional
write; each worker leases a small block (say 5 tokens for 250 ms) and spends it
in process. That keeps the DynamoDB write rate to a few per second while making
the global rate exact. The `TokenBucket` interface already fits — only the
acquire path changes — and the Terraform table and IAM grant would be added
alongside.

**How I would validate it.** A test that runs N limiter instances against a
moto-backed table with a fake clock and asserts the aggregate issue rate never
exceeds the ceiling over any sliding window; then a load test with the real
Lambda fleet, watching `WmsRequests` per second and the WMS's own 429 rate.

**Why it is not done.** Deliberate prioritisation. The per-worker division is
correct (never exceeds the limit), and I judged the ledger and recovery
semantics to be a higher risk to get wrong than throughput efficiency.

**Risk.** Medium. Not a correctness risk — the limit is never breached — but at
one million products it could push a run past the 30-minute SLA if the
concurrency is set generously and most workers are idle.

**Priority.** High (it is the first thing I would pick up).

---

## 2. Nothing has been deployed, so the AWS wiring is unproven

**What remains.** `terraform fmt -check` and `terraform validate` pass
(checked locally with Terraform 1.9.8, with `init -backend=false`). No
`plan` and no `apply` have been run, because there is no account. That means
the following are reviewed-by-reading only:

- IAM policies — least privilege is easy to get *wrong in the strict
  direction*, and the first apply typically surfaces two or three missing
  actions;
- the Step Functions definition — JSONPath expressions (`$.run.run_id`,
  `States.JsonToString($)`) are not validated by `terraform validate`;
- the `ecs:runTask.sync` integration, which needs the managed EventBridge rule
  permissions to be exactly right;
- the Lambda container image contract — `awslambdaric` plus the handler as the
  image command is a pattern I have used, but this specific image has not been
  built and invoked.

**Proposed solution.** Apply into a sandbox account; run the state machine once
end to end against the mocks hosted somewhere reachable; fix what the first
apply reveals. Then add `tflint` and `checkov` to CI, and a smoke test that
invokes each Lambda with a canned event.

**How I would validate it.** A full scheduled execution in the sandbox, ending
with the run record in DynamoDB showing `COMPLETED`, the CSV in S3, and the
dashboard showing accepted/rejected counts that match the ledger.

**Why it is not done.** Implementation limitation: the exercise explicitly does
not require deployment and I had no account.

**Risk.** Medium-high for a first deployment (expect iteration on IAM and the
state machine), low for the design itself.

**Priority.** High.

---

## 3. Nothing proves behaviour at 250k, let alone 1M products

**What remains.** The mock catalogue is 12,000 products, so the scale claims in
ARCHITECTURE.md section 2 and 7 are arithmetic, not measurement. Specifically
untested:

- the 2,000-page export and the 10,000-batch delivery;
- CSV assembly time and the S3 multipart path for a file of several hundred
  megabytes;
- DynamoDB behaviour under a million ledger writes in one run, which is the
  reason for the single-hash-key design — a design I believe is right but have
  not seen throttle or not throttle;
- SQLite's ledger at a million rows, which is the local path only.

**Proposed solution.** A load generator standing in for the PIM that serves a
synthetic one-million-product catalogue with the documented latency
distribution, and a WMS stub with the documented rate limit and latency. Run the
full pipeline against both, measure stage durations and the retry rate, and
compare with the 30-minute budget.

**How I would validate it.** Run duration under 30 minutes with headroom; no
DynamoDB throttling events; memory flat across the run (the streaming design
should hold peak memory at one page plus one batch window, and that is the
claim most worth checking).

**Why it is not done.** Deliberate: writing a credible load harness is most of
a day, and I judged correctness under failure to be the higher risk for this
exercise.

**Risk.** Medium. The design is explicitly built for this scale and the memory
profile is bounded by construction, but an unmeasured performance claim is a
claim, not a fact.

**Priority.** High.

---

## 4. Ambiguous outcomes still require a human, by design — but reconciliation could be better

**What remains.** The residual duplicate risk described in ARCHITECTURE.md
section 8.3 cannot be closed from our side. Two things that *would* help are not
implemented:

1. **Negotiating an `Idempotency-Key` with the warehouse team.** The client
   already sends the header; the WMS ignores it. Until the WMS honours it,
   `UNKNOWN` cannot be resolved automatically.
2. **Reconciliation by querying the WMS.** The WMS exposes no read endpoint, so
   "ask what you already hold" is impossible today.

**Proposed solution.** Raise both with the warehouse team; they are API
conversations, not code. In the meantime, the `UNKNOWN` state is bounded,
alarmed and operator-owned, and `reconcile` refuses to act without an explicit
flag. The clarification worth asking for first is assumption **A2** (does a 503
mean "not processed"?), because it is the weakest assumption in the design and
the answer changes how several thousand products would be treated in a bad run.

**How I would validate it.** Against a WMS that honours the key: send a batch,
force a client-side timeout, resend with the same key, and assert the product
appears once in the warehouse.

**Why it is not done.** Out of my control — it needs a change to a system owned
by another team. Documented rather than hidden.

**Risk.** Low in volume (proportional to timed-out batches), but high in
consequence per occurrence, since a duplicate in the WMS is exactly what the
business said must not happen.

**Priority.** High as a conversation, not as code.

---

## 5. Delta synchronisation

**What remains.** Every run is a full catalogue export. At a million products
that is about 12 minutes of pure API time every day to re-send data that mostly
has not changed.

**Proposed solution.** Use the `updated_at` the PIM already returns: keep a
high-water mark per run, request only products changed since the last successful
run, and fall back to a full export weekly or whenever the watermark is
untrustworthy (a failed run, a schema change). The ledger would then be keyed by
SKU and content hash across runs rather than per run — the `content_hash` is
already stored for exactly this purpose.

**How I would validate it.** Compare a delta run against a full run on the same
day and assert the WMS ends in the same state; test that a missed day is caught
by the fallback.

**Why it is not done.** Deliberate. It changes the duplicate semantics
(cross-run rather than per-run), and the PIM mock has no `updated_since`
parameter, so it could not be exercised honestly.

**Risk.** Low now, rising with catalogue size. This is the main lever if the
30-minute SLA is ever threatened.

**Priority.** Medium (revisit as the catalogue approaches a million).

---

## 6. Operational tooling around rejected products

**What remains.** Business rejections are recorded with their reason, counted,
logged, surfaced in the run summary and (in AWS) written to the exceptions
table. There is no report or alert *threshold* for them: 12 rejects and 12,000
rejects both end as `COMPLETED`.

**Proposed solution.** A rejection-rate alarm (for example > 1% of the
catalogue, tuned after a baseline) plus a `report` command that writes the
exceptions for a run to a CSV in S3 for the data team to act on.

**How I would validate it.** Inject a known rejection rate and assert the alarm
state and the report contents.

**Why it is not done.** Deliberate prioritisation; it needs a production
baseline to set a sensible threshold, and guessing one creates alert fatigue.

**Risk.** Medium. A silent rise in rejections means products are missing from
the warehouse while every run still reports `COMPLETED`.

**Priority.** Medium.

---

## 7. Smaller gaps

| Item | Note | Proposed solution | How to validate | Risk | Priority |
|---|---|---|---|---|---|
| `Retry-After` as an HTTP date | Only the seconds form is parsed; a date falls back to jittered backoff. Deliberate: a misparsed date could sleep for hours inside a 30-minute budget. | Parse the HTTP-date form and clamp the sleep to a fixed maximum, so a bad date cannot stall the run. | Unit tests for both forms, a past date, and an absurdly distant date. | Low | Low |
| Checksums are recorded but not verified on read | Page and CSV SHA-256 values are stored in the state store and manifest; nothing re-verifies them when a page is read back during CSV assembly. | Recompute the SHA-256 when a page is read back and fail the run on mismatch. | Corrupt a stored page in a test store and assert the run fails with a checksum error. | Low (S3 and the local store both checksum internally) | Low |
| DynamoDB TTL attribute is not written | The tables declare `expires_at` TTL but the application never sets it, so page, batch and ledger items persist indefinitely. One line per write plus a retention decision: ledger retention should probably exceed the 90-day S3 window. | Write `expires_at` on each item from a retention setting, with ledger retention longer than the S3 window. | Unit test that each write sets `expires_at`; check the TTL setting in the Terraform plan. | Low (cost only) | Medium |
| No CI pipeline | `pytest`, `terraform fmt -check`, `terraform validate`, and a lint/type pass should run on every commit. Nothing is wired up. | Add a CI workflow that runs these four steps on each commit. | A failing change to each step turns the pipeline red. | Low | Medium |
| No type checking or linting configuration | `pyflakes` is clean (no unused imports, no undefined names; one undefined-name finding in a test was fixed), but the code is fully annotated and nothing verifies the annotations: `mypy --strict` and `ruff` are not configured. | Add `ruff` and `mypy --strict` configuration and run them in CI. | Run both locally; they must pass with no suppressions added. | Low | Medium |
| Metrics cardinality in EMF | `run_id` is carried as a property, not a dimension, which is right for cost but means a per-run metric graph needs a Logs Insights query rather than a metric filter. | Keep `run_id` as a property; add a saved Logs Insights query for per-run graphs. | Run the query against a sample run's logs and compare the figures with the run summary. | Low | Low |
| Local runner has no SQS equivalent | Delivery is dispatched in process with a bounded window instead of a queue. Structural difference between local and AWS, called out in ARCHITECTURE.md section 14. The batch/ledger/claim semantics are shared, which is the part that carries the guarantees. | Put a local queue behind the same interface, such as an SQLite-backed queue. | The same test suite passes against both queue backends. | Low | Low |
| `list_runs` scans in DynamoDB | One bounded scan for an operator listing of roughly one run per day. Fine now; a GSI on a constant partition key with a date sort key would be the fix if run volume ever grows. | Add the GSI described in the note when run volume justifies it. | Load a year of synthetic run records and check the query latency against the GSI version. | Low | Low |
| Mock `429`/`503` paths cannot be exercised | The supplied mocks surface their throttling paths as HTTP 500 (argument order in `JSONResponse`). Not modified, as instructed; `Retry-After` and 429 handling are covered by unit tests with an injected transport instead. | Report the argument-order bug to whoever maintains the mocks; no change on our side. | Once the mocks return 429 and 503, run the end-to-end test against them unchanged. | Low | Low: worth telling whoever maintains the mocks |
