# What is not finished yet


**What "finished" means here:** the local version runs from start to end against the supplied test servers. Tests cover the behaviour that matters most: resuming after a failure, partial failures, unclear outcomes, and never sending a product twice.

**What is still missing:** two kinds of things.
- Anything that can only be proven in a real AWS account.
- Proof at real size. The test catalogue has 12,000 products, not 250,000 or 1 million.

---

## 1. One shared speed limit across all the senders in AWS

**What is missing.** Each sender gets a fixed share of the warehouse's speed limit, because the senders cannot see each other's counts. The total never goes over the limit, but when fewer senders are running, the run is slower than it needs to be.

**Why it matters.** Example: with 10 senders set up and only 3 running, the run uses 6 of the 20 requests per second allowed. That makes it about three times slower than it needs to be. The setting for how many senders run is also tied to the speed limit, so a well-meaning change to one setting can break the other.

**What I would do.** Use a shared counter in the database. Each sender borrows a small block of requests for a short time and uses them. This keeps the database work low and makes the total exact.

**How to check it.** Run several counters against a test database and check that the total never goes over the limit in any one-second window. Then run a full test with the real senders and watch the warehouse's rate of "slow down" answers.

**Why it is not done.** A deliberate choice. Splitting the limit is safe. I judged the ledger and recovery rules to be riskier to get wrong than some lost speed.

**Risk.** Medium. The limit is never broken. But at 1 million products, a run could miss the 30-minute target if the senders are set up generously and most of them sit idle.

**Priority.** High. This is the first thing I would build next.


---


## 2. Unclear results still need a person, and the warehouse could help more

**What is missing.** Sometimes a request times out, and we do not know whether the warehouse processed it. Those products are marked *unknown*, and a person must decide what to do. Two things would reduce this, but neither is built:

1. **An idempotency key agreed with the warehouse team.** A unique ID per batch, so the warehouse can say "already done" when asked again. Our program already sends this ID, but the warehouse ignores it. Until they use it, unknown products cannot be sorted out automatically.
2. **A way to ask the warehouse what it already has.** The warehouse has no such endpoint, so we cannot check before re-sending.

**What I would do.** Raise both with the warehouse team. They are conversations with another team, not code changes. In the meantime, unknown products are limited, flagged with an alarm, and only re-sent after a person confirms it. The question to ask first is whether a 503 error means "not processed". This is the weakest assumption in the design. If the answer is no, a bad run could affect thousands of products.

**How to check it.** Against a warehouse that uses the key: send a batch, force a timeout, send again with the same key, and confirm the product appears only once.

**Why it is not done.** It depends on another team's system.

**Risk.** Low in volume, because it only affects timed-out batches. High in impact each time, because a duplicate in the warehouse is exactly what the business said must not happen.

**Priority.** High, as a conversation with the warehouse team. Not a code task.

---

## 3. Copying only what changed

**What is missing.** Every run copies the whole catalogue. At 1 million products, that is about 12 minutes of reading every day, to send mostly unchanged data.

**What I would do.** The PIM already gives a "last updated" time for each product. Remember the time of the last good run, and only ask for products changed since then. Do a full copy once a week, or whenever the saved time looks wrong, for example after a failed run or a change in the data format. The ledger would then track products across days, not just within one run. The content fingerprint, already stored, is there for this.

**How to check it.** Compare a changed-only run with a full run on the same day, and confirm the warehouse ends in the same state. Check that a skipped day is caught by the weekly full copy.

**Why it is not done.** A deliberate choice. It changes what counts as a duplicate, from one run to across runs. The PIM mock also does not support a "changed since" request, so it could not be tested honestly.

**Risk.** Low today, but it grows with the catalogue. This is the main thing to change if the 30-minute target is ever at risk.

**Priority.** Medium. Revisit as the catalogue approaches 1 million.

---

## 4. Tools for handling rejected products

**What is missing.** Rejected products are recorded with their reason, counted, logged, and shown in the run summary. In AWS they are also written to the exceptions table. But there is no limit that raises an alarm when too many are rejected. A run with 12 rejections and a run with 12,000 rejections both finish as *completed*.

**What I would do.** An alarm when the rejection rate goes above a set level, for example 1% of the catalogue, tuned after a baseline is known. Also a `report` command that writes one run's rejections to a CSV file for the data team.

**How to check it.** Put in a known number of rejections and check that the alarm fires and the report lists the right products.

**Why it is not done.** A deliberate choice. A sensible threshold needs real production numbers. A guessed threshold would either miss problems or send too many alerts.

**Risk.** Medium. If rejections rise quietly, products go missing from the warehouse while every run still says *completed*.

**Priority.** Medium.

---

## 5. Smaller gaps

Each of these is small. The risk for most is low.

- **Retry dates.** The system understands "wait N seconds" but not a specific date and time in a "wait until" answer. Those fall back to a random wait. This is deliberate. A date read wrongly could make the system wait for hours.
  - *Fix:* read the date form and set a maximum wait, so a bad date cannot stall the run.
  - *Check:* tests for both forms, a date in the past, and a date far in the future.
  - *Priority:* low.

- **Checksums are not re-checked when read.** Each page and the CSV have a fingerprint saved when they are written. Nothing checks it again when the pages are read back. Storage already protects against most corruption.
  - *Fix:* check the fingerprint when reading, and stop the run if it does not match.
  - *Check:* damage a stored page in a test and confirm the run stops with an error.
  - *Priority:* low.

- **Old database records are never deleted.** The database tables are set up to delete old records, but the program never sets the date that triggers it. So pages, batches and ledger entries stay forever. This only costs money. Ledger records should be kept longer than the 90-day storage window, so a decision is needed too.
  - *Fix:* set the expiry date on each record, with a longer period for the ledger.
  - *Check:* a test that every write sets the expiry date.
  - *Priority:* medium.

- **No automatic checks on each change.** Tests, Terraform format and validity, and code style checks are not run automatically when code is pushed. Nothing is set up yet.
  - *Fix:* a build pipeline that runs all of them on every change.
  - *Check:* a deliberately broken change turns the pipeline red.
  - *Priority:* medium.

- **Metric labels.** The run ID is stored as a detail, not as a label on the metric. This keeps costs down, but a graph for a single run needs a query in the logs rather than a simple metric.
  - *Fix:* keep it this way and add a saved query for single-run graphs.
  - *Check:* compare the query's numbers with the run summary.

- **No queue in the local version.** Locally, the work is done in one process within a limited window, not through a queue. The rules for batches, the ledger and claims are the same, and those rules are what matter. See the note in the architecture document.
  - *Fix:* add a local queue behind the same interface.
  - *Check:* the full test suite passes with both queues.

- **Listing runs scans the table.** Listing past runs reads the whole table. That is fine at about one run a day. If runs ever become frequent, an index on the date would fix it.
  - *Fix:* add that index when the number of runs justifies it.
  - *Check:* time the listing with a year of sample runs before and after.