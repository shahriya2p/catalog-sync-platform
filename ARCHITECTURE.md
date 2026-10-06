# How the product sync works


This describes the **planned production design**. Parts that are built and tested locally are marked as such in section 14. What is not built yet is listed in [UNFINISHED.md](UNFINISHED.md).

---

## 1. The problem

Every morning, the company copies its full product list from the **PIM** (the system where products are described) into the **WMS** (the warehouse system that stores stock and product details).

- The product list used to be under 10,000 products. It is now about **250,000**, and is expected to reach about **1 million** within 18 months.
- The copy must normally finish within **30 minutes**.
- Temporary failures must recover on their own, without someone stepping in.
- **The same product must never be sent to the warehouse twice.**
- The original export of each day must be kept for **90 days**.
- Operations must be able to tell, at any moment, whether a run is still going, finished, or failed.

## 2. The numbers that shape the design

| What | Limit | What it means |
|---|---|---|
| PIM speed limit | 10 requests per second, 500 products each | At most 5,000 products per second. 1 million products take at least about 200 seconds just to read. |
| WMS speed limit | 20 requests per second, 100 products each | At most 2,000 products per second. 1 million products take at least about 500 seconds (8 to 9 minutes) just to send. |
| Time allowed | 30 minutes (1,800 seconds) | At 1 million products, sending uses about 28% of that time at the WMS speed limit. At 250,000, about 7%. |
| PIM response time | 0.1 to 3 seconds | Many requests have to be in progress at once to keep up with the speed limit. |
| WMS response time | Varies a lot | Same idea. The system must not wait for one request before starting the next. |

Two rules follow from these numbers:

1. **All the workers share one speed limit.** If each worker simply waits its own turn, ten workers would together send ten times the allowed speed. The system counts requests across all workers so the total stays under the limit.
2. **Retries use up the speed limit too.** Every retry is a request, so retries must be limited. Otherwise a run that hits many temporary errors would miss the 30-minute target.

At 1 million products, the fastest possible run takes about 8 to 9 minutes. The 30-minute target leaves room for the system to run about three times slower than ideal because of throttling and slow responses.

## 3. The big picture

![Architecture diagram](docs/architecture.png)

*The diagram source is [docs/architecture.mmd](docs/architecture.mmd). To redraw it, open [docs/architecture.html](docs/architecture.html) in a browser.*

Think of the system as three parts that only pass messages to each other. None of them reaches into another part's memory. That means any part can be restarted without breaking the others.

- **Control (the manager):** starts the daily run, keeps track of its steps, sets time limits, and reports the final result. It never touches product data.
- **Download (the reader):** reads every product page from the PIM and saves each page as a file in storage before anything else happens.
- **Upload (the sender):** takes the saved pages, sends the products to the WMS in small groups, and records what happened to each product.

## 4. The parts and why each one is there

| Part | What it does | Why it is this way |
|---|---|---|
| **Daily timer** | Starts one run each morning. | Built into AWS. Nothing to maintain. |
| **Run manager** (AWS Step Functions) | Runs the steps in order, waits for them, handles time limits, and shows a visual history of each run. | Answers "is it running, done or failed?" without custom tools. It is for the run as a whole, not for each product. |
| **Page reader** (one container task) | Reads every PIM page, saves it, and notes in a database that the page is saved. | Safe to restart: a page that is already saved is skipped. One task means the PIM speed limit is counted exactly. |
| **Raw page storage** (S3) | A copy of every PIM page exactly as received. | If the later steps have a bug, we can run them again from these copies without asking the PIM again. Kept for 90 days. |
| **Export storage** (S3) | The CSV file built from the raw pages, plus a `manifest.json` that lists how many products were in it and a fingerprint of the file. | The manifest is the proof that nothing is missing. |
| **Run record** (DynamoDB) | One record per run: its status, its current step, its counts and its times. | Operators check this to see the status of a run. |
| **Page record** (DynamoDB) | One record per saved page. | After a failure, only the missing pages are read again. |
| **Work queue** (SQS) with a dead-letter queue | Holds one message per saved page, telling a sender which page to send. | Lets the sending speed be set separately from the reading speed. The dead-letter queue catches messages that keep failing so they do not block the rest. |
| **Sender** (Lambda) | Reads a page, groups products into batches of 100, applies the WMS speed limit, sends them, and records each product's result. | Several senders can run at once, each with a fair share of the WMS speed limit. |
| **Product ledger** (DynamoDB) | One record per product per run: where it is in the process, how many times it was tried, and the last reason. | This is what stops a product being sent twice. |
| **Exceptions table** (DynamoDB) | Only the products that were rejected, could not be confirmed, or were invalid. | Gives operations one list of "what went wrong in this run" without searching the whole ledger. |
| **Monitoring** (CloudWatch) | Counts, a dashboard, and alarms that send alerts. | Built into AWS. |

### Why use both the run manager and the queue?

The run manager handles the **run**: about a few dozen steps a day. The queue handles the **work**: about 10,000 batches for 1 million products. Putting 10,000 batches into the run manager would make its history unreadable and hit its limits. Putting the run steps into the queue would lose the simple status view. Each tool does what it is built for.

## 5. What happens during a run, step by step

1. **Start.** A run record is created with status *running*. Each run has an ID made of the date, the time and a short random code, for example `20261005T0931Z-3f9c`. This means runs sort by date, and a manual re-run never clashes with the scheduled one. If the same ID is started twice, the system refuses and tells the operator to use "resume" instead.
2. **Read all the pages.** One task reads every page from the PIM. Before it reads a page, it checks whether that page is already saved. If it is, the page is skipped. Each page is saved to storage first, and only then is it marked as saved. If the task crashes in between, the page looks unsaved and is simply read again. The other order could mark a page as saved when it is not.
   The total number of pages is not known at the start. The first page response says how many products there are in total, and that gives the number of pages.
3. **Build the CSV and manifest.** The saved pages are joined, in order, into one CSV file. A manifest records the product count, the total the PIM reported, a fingerprint of the file, and any pages that failed. If a page is missing, or the count does not match, the manifest says the export is incomplete, and the run is marked *partial*. It is never quietly short.
4. **Queue the work.** One message is queued per saved page, not per batch. A page message is tiny. A message per batch would have to point into a file that can be hundreds of megabytes. Each page of 500 products becomes five batches of 100 inside the sender. Queuing again is harmless, because the ledger decides what can be sent.
5. **Wait for the senders to finish.** The run manager checks, at intervals, that no batch is still waiting, being sent, or waiting to be retried. It stops at the time limit.
6. **Final result.**
   - **Completed:** every product has a clear outcome.
   - **Partial:** something is missing or unclear: a failed page, a failed batch, a product with an unknown outcome, or an incomplete export.
   - **Failed:** no usable export was produced.

   Products rejected by the warehouse are reported but do not make a run partial. The warehouse has already said clearly what it thinks of those products.
7. **Resolve unknowns (only by hand).** There is a separate operator command, `reconcile`, that re-sends only products with an unknown outcome. It only runs with an explicit confirmation flag, because re-sending is the one step that can create a real duplicate. It is never part of the daily run.

### Data flow in one picture

```
PIM products ──► raw page files ──► S3: raw/{run}/pages/
                                        │
                                        ▼
                  CSV + manifest ──► S3: exports/{run}/catalogue.csv, manifest.json
                                        │
                                        ▼
                  queue messages ──► sender ──► WMS
                                        │
                                        ▼
                  ledger: PENDING → SENT → ACCEPTED, REJECTED or UNKNOWN
```

The CSV is **a view of the raw pages**, not the original. The raw pages are the source of truth. If the way the CSV is built changes, we rebuild it from the raw pages without asking the PIM again.

## 6. When things go wrong

| What happened | What it means | What the system does |
|---|---|---|
| PIM says "slow down" (429) | Temporary. Nothing was processed. | Waits as long as the PIM asks, or backs off with a random wait, then tries again. |
| PIM server error (5xx) or timeout | Temporary. Reading has no side effects, so trying again is safe. | Tries the page again. |
| PIM says the request is wrong (400, 401) | Permanent. Retrying will not help. | Fails that page right away. A "not allowed" (401) answer stops the whole run, because it means the setup is wrong. |
| WMS says "slow down" (429) | Temporary. Nothing was processed. | Same backing off. The product states do not change. |
| WMS server error (503) | Probably temporary. We do not know for sure whether the warehouse processed it. | Backs off and tries again. See the duplicate section (8) for why this is an assumption. |
| WMS says the request is wrong (400, 413) | Our data or request is wrong. Retrying will not help. | Does not retry. Marks those products as failed validation and reports them. This is a bug to fix, not a temporary problem. |
| WMS answers with a list of accepted and rejected products | Partial success. This is normal. | Accepted products are marked done and are **never sent again**. Rejected products get their reason recorded and are not retried, because retrying a rejection cannot succeed. |
| A product is in neither the accepted nor the rejected list | We do not know what happened to it. | Marked *unknown*. Never treated as accepted. |
| The connection drops after the request was sent | We do not know whether the warehouse processed it. | Marks the products *unknown* and does **not** send them again automatically. See section 8. |
| The sender crashes in the middle of a batch | Same situation as above. | The message is delivered again later. Products already accepted or rejected are skipped. Products left in *sent* with no answer become *unknown*. |

### Retry rules (used for both the PIM and the WMS)

- Each retry waits a random time that grows with each attempt, up to 30 seconds. The random part spreads retries out, so many workers do not all retry at the same moment.
- If the PIM or WMS says how long to wait, that wait is used if it is longer.
- After 6 attempts on one request, that page or batch is marked failed for this round. The message goes back into the queue to try again later. After 5 rounds, it moves to the dead-letter queue for a person to look at.
- Each attempt, including retries, counts against the speed limit.

### A note on the supplied test servers

The supplied PIM and WMS test servers have a bug: when they mean to send "slow down" or "unavailable", they actually send a generic server error with no wait time. Because of this, the local tests cover the general retry path, not the specific "slow down" path. The "slow down" path is covered by separate unit tests instead. The test servers were not changed, as the brief asks.

## 7. Speed and the speed limits

### How the speed limit is counted

- The PIM and WMS each have one counter, shared by all the threads that talk to them. The counter lets 10 requests per second through to the PIM and 20 to the WMS.
- A request that arrives when the counter is empty waits its turn. Requests are served in the order they arrived.
- Retries count as requests, so they use up the limit too.

**In AWS, the WMS limit is split between workers, not shared.** The page reader is a single task, so its counter is exact. The senders run as separate Lambda functions that cannot see each other's counters. So each sender gets a fixed share of the limit, based on how many can run at once. This is safe, because the total never goes over the limit. But if only a few senders are running, the run is slower than it needs to be. A proper shared counter is the next thing to build (see [UNFINISHED.md](UNFINISHED.md)). It is not built yet, so this document does not claim it exists.

Adding more senders does not make the run faster by itself. The WMS speed limit is the real ceiling.

### How the run time grows

| Step | 250,000 products | 1 million products | What limits it |
|---|---|---|---|
| Read from the PIM | about 50 seconds | about 200 seconds | PIM speed limit |
| Build the CSV | a few seconds | a few minutes at most | Storage speed, not a real limit |
| Send to the WMS | about 125 seconds | about 500 seconds | WMS speed limit |
| Total, ideal | about 3 minutes | about 12 minutes | |
| Total, with about 20% extra for retries and slowness | about 4 minutes | about 15 minutes | Well within 30 minutes |

The design grows in a straight line with the number of products because **the system never holds the whole product list in memory**. Pages are saved to storage as they arrive, the CSV is built from those saved pages, and the sender works through a limited number of batches at a time. An earlier version held every product in one list. At 1 million products that would have needed about 1 to 2 GB of memory, and a single failure would lose everything.

These are estimates from arithmetic. They have not been measured at this size. See section 15.

## 8. Partial failures and duplicates

The business rule is: *the same product must never be sent twice.* The warehouse does not state what this means exactly, so we split it into four cases.

### 8.1 The four cases

| # | Case | What happens | What we guarantee |
|---|---|---|---|
| 1 | The same product is processed again in the same run, for example after a restart. | The ledger records each product's state. Moving a product from *sent* to *accepted* only happens if its state is still the earlier one. A product already in a final state is skipped. | **Strong.** Within our system, a product that reached a final state in a run is never sent again in that run. |
| 2 | The warehouse said "not processed" (429, 503, 400, 413) and we try again. | We record the attempt and send again. For 400 and 413 we do not send again, because the data is wrong. | **Strong** for 429 and 400/413, if "not processed" is what those answers mean. 503 depends on an assumption (section 8.3). |
| 3 | The connection timed out after the request was sent. | The warehouse may or may not have processed it. We mark the products *unknown* and do **not** send them again automatically. | We cannot prove the product was not sent. We also do not send it twice. These products are visible and need a deliberate decision. |
| 4 | Tomorrow's run sends a product that was sent today. | This is expected. The catalogue is a daily full copy, so it is an update, not a duplicate. | "Never twice" means never twice within one run. We assume the warehouse updates a product when it gets the same product code again. |

### 8.2 What the warehouse API gives us, and what it does not

- It has **no way to say "I already have this request"**. There is no idempotency key, which is a unique ID that makes repeat requests safe.
- It does not say what happens if the same product code appears twice in one batch or across batches. We checked the test server: sending the same product twice in one batch returns it twice as accepted. So **the warehouse does not stop duplicates itself. Our ledger has to.**
- It does not say what it does with a request that timed out.
- It has **no way to ask what it already holds**, so we cannot check before we resend.

This means **case 3 cannot be fully closed from our side**. There are three possible fixes:

1. **Best option, needs a change by the warehouse team:** an `Idempotency-Key` header, a unique ID per batch. The warehouse stores the result for 24 hours and returns the same result on a repeat. With this, case 3 becomes a safe retry. Our client already sends this header. The warehouse ignores it today, so it gives no protection yet. The day they support it, no change on our side is needed. A header the server ignores is not a guarantee, and this document does not count it as one.
2. **What is built now, no warehouse change needed:** keep *unknown* as a visible, limited state. Raise an alarm whenever there are any unknown products. Provide a `reconcile` command that re-sends only unknown products, and only with an explicit flag. The remaining risk is a few duplicates, at most one per timed-out batch whose request actually reached the warehouse.
3. **Ask the warehouse for a read endpoint:** check before re-sending. Not available today. Listed in [UNFINISHED.md](UNFINISHED.md).

### 8.3 Assumptions behind these claims

- **A1:** A 429 answer means the request was **not processed**. If this is false, case 2 becomes case 3 for 429 answers.
- **A2:** A 503 answer means the request was **not processed**. This is weaker than A1 and is the first assumption to check with the warehouse team.
- **A3:** Within one run, the same product appearing in two batches is a bug. Across runs, a repeat is expected as an update.
- **A4:** A 200 answer is correct for every product it names as accepted or rejected.

### 8.4 A partial success, concretely

The warehouse answers "OK" but with two lists:

```json
{"accepted": ["P0000001", "P0000002"],
 "rejected": [{"sku": "P0000999", "reason": "Invalid warehouse product"}],
 "message": "Processed"}
```

The accepted list is plain codes. The rejected list has a code and a reason. The reader must handle both shapes and must not assume the two lists cover the whole batch. Any product that is in neither list becomes *unknown*.

Accepted products are never sent again. A batch of 100 with one rejection updates one product to *rejected* and 99 to *accepted*. None are sent again.

A rejection can also come back with no product code, for example `{"sku": null, "reason": "sku is required"}`. We cannot tell which product that is, so we do not guess. We count it, log it, and leave the affected products as *unknown*. Before sending, every row is checked locally, so a product with no code is recorded as a failed validation against its row. It never reaches the warehouse as an unexplained rejection.

### 8.5 Why the order of writes matters

The ledger records a product as *sent* **before** the request goes out. This is what makes a crash visible:

1. Claim the batch. Only a batch that is waiting or waiting to retry can be claimed, so a repeated message or a second sender stops here.
2. Skip products already in a final state. This is what lets a resumed run send only what is missing.
3. Mark the remaining products as *sent*.
4. Send the batch, and record each product's result.

If the process dies between steps 3 and 4, the products are left as *sent* with no result. On the next pass they become *unknown*, never *waiting*. The request may already have been applied, so they must not be sent again automatically. This is tested directly in [tests/test_warehouse_sync.py](tests/test_warehouse_sync.py).

## 9. Which AWS services and why

| What we need | Service | Why this one |
|---|---|---|
| Daily timer | EventBridge Scheduler | Needs no server to run. |
| Run steps and history | Step Functions (Standard) | Shows the history of each run in a visual page. A simpler tool would not keep that history. Airflow is heavier to run for one daily job. |
| Reading pages | ECS Fargate task | Reading a million products can take many minutes. Lambda stops after 15 minutes, so it is not used here. |
| Sending batches and run steps | Lambda (container image) | Many short, separate pieces of work. Limiting how many run at once is the simplest way to limit the speed sent to the WMS. |
| Packaging | One image for both | Two images would mean two builds and the risk that they run different code. |
| Raw pages and exports | S3, with versioning, encryption and a 90-day rule | Required by the brief. The 90-day rule is enforced by the storage itself, not by our code. |
| Run status, page records, product ledger | DynamoDB (pay per request) | Fast lookups and safe conditional updates. A relational database would add connection work for no benefit. |
| Work queue | SQS (standard) with a dead-letter queue | Standard, not FIFO. The order is enforced by the ledger. FIFO's limit of 300 messages per second would slow down the 10,000 batch messages. |
| Passwords and API keys | Secrets Manager | Keys are changed outside the code and are never stored in code or configuration files. |
| Monitoring | CloudWatch (metrics, alarms, dashboard), SNS for alerts | Built into AWS. No extra vendor. |
| Infrastructure as code | Terraform | Required by the brief. |

## 10. Security and access

- **Each part has its own permissions.** The page reader, the sender, the control functions, the run manager and the timer each have a separate role. Each role can only reach the storage, tables and queues it needs ([infra/terraform/iam.tf](infra/terraform/iam.tf)).
- **The permissions are set to limit mistakes.** The page reader can write the raw pages and exports, but cannot send to the warehouse or change the ledger. The sender can read the raw pages and write to the ledger, but **has no permission to write to storage at all**. A bug in sending therefore cannot damage the 90-day export. Nothing has permission to delete files. Expiry is done by the storage's own rule.
- **Keys and passwords** are stored in Secrets Manager and read when the function starts. They are never in code, in configuration files that go to git, or in plain environment variables. The repository already ignores `.env` files and `*.tfvars` files.
- **Storage** blocks all public access, encrypts files with a managed key, keeps old versions, only accepts secure connections, and deletes files after 90 days.
- **Data type:** the product data is commercial, not personal. No personal data is in scope. Logs record counts and product codes for rejections and unknowns. They never contain full product records or keys.
- **Network:** the functions run inside a private network, and reach the PIM and WMS through a fixed outgoing address if the partners need an allow list. Traffic to S3 and DynamoDB stays inside AWS.
- **Connections** to the PIM and WMS use HTTPS, and the certificates are checked.

## 11. Monitoring

### The numbers we track

The metrics are kept in a single namespace called `CatalogueSync`. Run IDs are not used as labels, to keep costs low.

- **Reading:** requests to the PIM, retries, "slow down" answers, server errors, pages read, pages failed, pages skipped because they were already saved, products exported.
- **Sending:** requests to the WMS, retries, "slow down" answers, server errors, batches sent, batches skipped, unknown outcomes.
- **Outcomes:** products accepted, rejected, unknown, failed validation.
- **Time and status:** run duration, reading duration, sending duration, runs completed, partial and failed.

The counts are gathered during each step and written out once per step. Locally they go to the log as a line of JSON. In AWS they go to CloudWatch in a format it reads directly, so one set of counters serves both. The `PagesSkippedAlreadyStored` count shows that a resumed run really did avoid downloading the same pages again.

### Logs

- Each log line is one event in JSON, with the run ID, the step, the page or batch number, the attempt number, and the outcome.
- The run ID travels with every queue message, so one run can be followed from start to finish.
- Normal steps are logged as information. Retries are warnings. Permanent failures are errors. Product codes are logged only for rejections and unknowns.

### Alarms

| Alarm | When it fires | Why |
|---|---|---|
| Run failed | The run stops with an error or times out | The run did not finish. |
| Run late | No completed run within 30 minutes of the scheduled start | The business deadline has been missed. |
| Unknown products | Any unknown products at the end of a run | Someone needs to decide what to do. |
| Partial run | Failed pages, or too many rejections | The data is incomplete. The threshold is set after a baseline is measured. |
| Dead-letter queue has messages | Any message in it | Something keeps failing. |
| Too many "slow down" answers | A high count for 10 minutes | An early warning that a partner is slowing us down. |

### What an operator sees

`python -m app.main status <run_id>` locally, and the run's page in Step Functions in AWS, show whether the run is running, completed, partial or failed, with the counts.

## 12. Trade-offs

Every design choice has a cost. These are the main ones.

| Decision | What we chose | The alternative | Why | What it costs us |
|---|---|---|---|---|
| Keep raw pages | Yes | Send straight to the warehouse | We can replay and audit without asking the PIM again | Storage grows with the catalogue. It is limited by the 90-day rule, and the size has not been measured yet. |
| CSV as a view | Yes | CSV as the original | The raw pages stay the source of truth | Building the CSV adds a few minutes at 1 million products. |
| Never send *unknown* products again automatically | Yes | Retry on timeout | Avoids silent duplicates | Some products need a person to decide. This risk remains until the warehouse supports idempotency keys. |
| Shared speed counter across workers | **Planned, not built.** Today the WMS limit is split between workers. | Split the limit (what we have) | The shared counter gives the exact limit and uses it fully | Added complexity, and more writes to the database. |
| Run steps plus a queue | Yes | Queue only, or run steps only | A status view plus a queue with no size limit | Two services to understand. |
| Standard queue | Yes | FIFO | Higher throughput. The ledger enforces the order. | Duplicate messages are possible. The ledger handles them. |
| Lambda plus container task | Yes | Container tasks only | Lambda is cheap and fast for short work. The container task handles the long read. | Two ways to package the code. |
| Random backoff | Yes | Fixed waits or no waits | Spreads retries out | Run times are a little less predictable. |
| Do not retry rejected products | Yes | Retry them | Retrying a rejection cannot succeed, and it wastes the speed limit | Fixing bad data needs a separate process. |

## 13. Assumptions, and what changes if they are wrong

Each row has four parts: the problem, the assumption we made, what that assumption affects, and what we would change if it turned out to be false.

| # | Problem | Assumption | What it affects | What we would change if it is false |
|---|---|---|---|---|
| 1 | The warehouse does not define "never twice" | It means never twice in one run. Updating a product across runs is expected (A3). | The ledger is per run. Daily updates are allowed. | Track each product across runs, by its code and a fingerprint of its content, and check before every send. |
| 2 | We do not know what a 503 means | A 503 means "not processed" (A2). | 503 is retried, the same as 429. | Treat 503 like a timeout and mark the products *unknown*. This means more manual checking. |
| 3 | The warehouse has no idempotency key | We cannot add one ourselves. | Duplicates in unclear cases are limited, but not impossible. | Use the `Idempotency-Key` header once the warehouse supports it (section 8.2). |
| 4 | The PIM total stays the same during a run | The catalogue does not change while it is being read. | Pages are consistent with one total. | Use a snapshot or a "changed since" cursor. Both are listed in UNFINISHED.md. |
| 5 | A full copy every day is acceptable | We copy the whole catalogue each day, not just changes. | Simple. Takes about 12 to 15 minutes at 1 million. | Copy only changed products, using the PIM's "last updated" time. This is the first thing to do after 1 million. |
| 6 | The test servers behave like the real ones | Their fixed pattern of errors is a fair stand-in. | The tests use it. | Real timing and errors change how we tune the system, not the design. |
| 7 | The 30-minute target covers the whole run | It includes reading, building the CSV, and sending. | The time budget in section 5 applies. | If sending alone must take 30 minutes, the budget still holds. |
| 8 | The PIM product ID is the warehouse product code | The `id` from the PIM is the warehouse's code. | The ledger uses this code. | Use a combined key if the warehouse uses a different one. |

## 14. What is built, and where

This table shows where each part lives in the code, and whether it runs on a laptop or only in AWS.

| Part | Code | Runs locally? | Runs in AWS? |
|---|---|---|---|
| Speed limiting | [app/clients/rate_limiter.py](app/clients/rate_limiter.py) | Yes, exactly | Exact for the page reader. **Split between senders** for the WMS (section 7). |
| Deciding what to retry, waits, random backoff | [app/clients/retry.py](app/clients/retry.py) | Yes | Yes |
| Reading PIM pages | [app/clients/product_api.py](app/clients/product_api.py) | Yes | Yes |
| Batching, partial success, unknowns | [app/clients/warehouse_api.py](app/clients/warehouse_api.py) | Yes | Yes |
| Saved page records, CSV, manifest | [app/services/catalogue_export.py](app/services/catalogue_export.py) | Yes | Yes |
| Ledger, claiming, resume, reconcile | [app/services/warehouse_sync.py](app/services/warehouse_sync.py) | Yes | Yes |
| Run steps and final status | [app/services/runner.py](app/services/runner.py) | Yes (steps run in one process) | Yes (Step Functions calls the same code) |
| File storage | [app/storage/](app/storage/) | Local folder | S3, tested with a fake S3 (moto) |
| Run state | [app/state/](app/state/) | SQLite database | DynamoDB, tested against the same checks as SQLite |
| Queue handling | [app/aws_handlers.py](app/aws_handlers.py) | Runs in process instead of a queue | SQS |
| Infrastructure | [infra/terraform/](infra/terraform/) | Checked, not deployed | Not deployed |

The one real difference between the local run and AWS is the queue. Locally, the work is handled in one process with a limited number of batches at a time. The rules for batches, the ledger and claims are the same in both, and those rules are what keep products from being sent twice.


---

## Glossary

- **PIM:** Product Information Management. The system where products are described and maintained.
- **WMS:** Warehouse Management System. The system that stores stock and product details for the warehouse.
- **SKU:** a product's code, its unique identifier.
- **Batch:** a group of 100 products sent to the WMS in one request.
- **Idempotency key:** a unique ID sent with a request so that repeating the same request is safe.
- **Ledger:** the record of what happened to each product in a run.
- **Speed limit (rate limit):** the maximum number of requests allowed per second.
- **Backoff:** waiting longer and longer between retries.
- **Dead-letter queue:** where messages go after they keep failing, so a person can check them.
- **Reconcile:** the manual step that re-sends products with an unknown outcome, after explicit confirmation.
