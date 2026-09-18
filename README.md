# Media Archive Pipeline

Automatically compresses newly-ingested JSON processing results in S3 into ZIP
archives, using a containerized AWS Lambda running inside a private VPC, deployed
with AWS SAM.

This repo satisfies a 5-part take-home assignment; each task maps directly to a
section below so it's easy to verify against the brief:

| Task | Where it's satisfied |
|---|---|
| 1 — Lambda that zips a new object and deletes the original | [`src/app.py`](src/app.py), tested in [Test it](#test-it) |
| 2 — SAM stack (`template.yaml`), private VPC, Dockerized, versioned/rollback-capable | [`template.yaml`](template.yaml), [Architecture](#architecture), [Versioning & rollback](#versioning--rollback) |
| 3 — Incremental commits + public repo + README | `git log --oneline` |
| 4 — Cost analysis at 1M files/hour, 10MB avg | [Cost analysis](#cost-analysis) |
| 5 — Scalability/bottleneck concerns | [Scalability & bottlenecks](#scalability--bottlenecks) |

**Status:** this stack has been built, deployed to a real AWS account, and
validated end-to-end — a file uploaded to `incoming/` triggers the real S3
event notification (not a manual invoke), the Lambda runs inside the private
VPC, and the archive/delete completes correctly. Versioning and alias-based
rollback have also been exercised against a live deployment. See
[Test it](#test-it) for the exact steps.

## Table of contents

- [Architecture](#architecture)
- [Project layout](#project-layout)
- [Prerequisites](#prerequisites)
- [Deploy](#deploy)
- [Test it](#test-it)
- [Versioning & rollback](#versioning--rollback)
- [Cost analysis](#cost-analysis)
- [Scalability & bottlenecks](#scalability--bottlenecks)

## Architecture

```
                 ┌────────────────────────────────────────────────────────────┐
                 │                     Custom VPC (private-only)              │
                 │                                                            │
  incoming/*.json│    ┌───────────────┐        ┌──────────────────────────┐   │
  ───────────────┼──▶│  S3 event      │───────▶│  ArchiveFunction (Lambda)│  │
     (on-prem    │    │  notification │        │  container image, in      │  │
      exporter)  │    └───────────────┘        │  2x private subnets       │  │
                 │                              └──────────┬───────────────┘  │
                 │        ┌────────────── ┐   ┌──────────────┴────────┐       │
                 │        │ S3 Gateway    │   │ Interface endpoints:  │       │
                 │        │ Endpoint      │◀─│ ECR api/dkr           │        │
                 │        │               │   │                       │       │
                 │        └───────┬───────┘   └───────────────────────┘       │
                 │                ▼                                           │   
                 │    ┌────────────────────────┐                              │
                 │    │   S3 bucket            │                              │
                 │    │   incoming/*.json  ────┼──▶ deleted after archiving  │
                 │    │   archive/*.json.zip  ◀┼──  written by Lambda        │
                 │    └────────────────────────┘                              │
                 └───────────────────────────────────────────────────────────┘
```

No NAT Gateway exists in this stack. The Lambda's only outbound needs — S3,
ECR (to pull its own container image), and CloudWatch Logs — are satisfied
entirely by VPC endpoints, which is the main cost decision explained in
[Cost analysis](#cost-analysis).

## Project layout

```
.
├── template.yaml                             # SAM/CloudFormation: VPC, endpoints, S3 bucket, Lambda, trigger
├── src/
│   ├── app.py                                # Lambda handler (download → zip → upload → delete)
│   ├── Dockerfile                            # Container image definition
│   └── requirements.txt
├── events/
│   └── s3-put-event.json                     # Sample event for `sam local invoke`
├── docs/
│   └── AWS Infrastructure Screenshots.pdf    # Screenshots of AWS infrastructure with CLI outputs 
└── README.md
```

## Deploy

```cmd
# Build the container image
sam build --use-container

# First deploy - walks you through stack name, region, and saves the
# answers to samconfig.toml for subsequent deploys
sam deploy --guided
```

`sam deploy --guided` will ask for:

- **Stack Name**: e.g. `media-archive-pipeline`
- **AWS Region**: e.g. `us-east-1`
- **Confirm changes before deploy**: `Y` (recommended)
- **Allow SAM CLI IAM role creation**: `Y`
- **Save arguments to samconfig.toml**: `Y`

Subsequent deploys are just `sam build --use-container && sam deploy`.
`samconfig.toml.example` shows the shape of the config `--guided` generates,
if you'd rather deploy non-interactively.

> **Prompt order matters.** `sam deploy --guided` asks for the **Region**
> before it asks any yes/no questions — answer it with a region name (or
> just press Enter to accept the bracketed default) and save the `Y`/`N`
> answers for the actual confirm/rollback/save-config prompts further down.
> Typing `Y` at the Region prompt produces a confusing
> `Could not connect to the endpoint URL: "https://cloudformation.Y.amazonaws.com/"`
> error — it's just a misplaced answer, not a real connectivity issue;
> re-run `sam deploy --guided` and answer Region correctly.

> **If deploy fails with `No such image: archivefunction:latest`**, an
> intervening `sam local invoke` run can leave a different local image tag
> (`archivefunction:rapid-x86_64`) in place of the one `sam deploy` expects.
> Fix: `sam build --use-container` again immediately before `sam deploy`.

## Test it

Two levels of testing, cheapest/fastest first. Both were exercised in
building this project — the steps below are what actually worked, gotchas
included.

### Level 1 — local container sanity check (no AWS needed, $0)

`public.ecr.aws/lambda/python` images ship the Lambda Runtime Interface
Emulator, so you can run the built image directly and hit it with a fake
invoke:

```cmd
sam build --use-container
docker run -d -p 9000:8080 --name archive-test archivefunction:latest

curl -XPOST "http://localhost:9000/2015-03-31/functions/function/invocations" \
  -d "@events/s3-put-event.json"
```

The sample event points at a bucket/key that doesn't really exist, so a
`NoCredentialsError` or `HeadObject`/`404` response here is the **expected,
correct** result — it proves the runtime found `app.handler`, `boto3`
imported cleanly, and your code reached the real S3 call before failing.
`Unable to import module 'app'` or the container exiting immediately is the
actual failure signal to watch for.

```cmd
docker logs archive-test
docker rm -f archive-test
```

### Level 2 — functional test against real S3 ($0.01 or less)

This is the one that proves the zip → upload → delete logic actually works.
Create a scratch bucket and a test object:

```cmd
aws s3 mb s3://my-scratch-test-bucket --region us-east-1

echo '{"job":"test"}' > test.json
aws s3 cp test.json s3://my-scratch-test-bucket/incoming/test.json
```

> **Gotcha:** the object **must** be under the `incoming/` prefix — the
> template's S3 event filter and the sample event's key both expect it
> there. Uploading to the bucket root instead of `incoming/` produces a
> `404 HeadObject` error that looks like a credentials problem but isn't —
> it's a missing-prefix problem.

Edit `events/s3-put-event.json`'s `bucket.name` and `object.key` to match,
then invoke through **`sam local invoke`** (not raw `docker run` — SAM
forwards your local AWS credentials into the container automatically):

```cmd
sam local invoke ArchiveFunction --event events/s3-put-event.json
```

A clean run logs `Downloading` → `Compressing` → `Uploading` →
`Deleting` in order, ending in `{"processed": [{"status": "ok", ...}]}`.
Confirm on S3's side:

```cmd
aws s3 ls s3://my-scratch-test-bucket/archive/     # test.json.zip should be here
aws s3 ls s3://my-scratch-test-bucket/incoming/    # should be empty
aws s3 rb s3://my-scratch-test-bucket --force       # clean up when satisfied
```

### End-to-end test against the real deployed stack

Once both local levels pass, deploy (see [Deploy](#deploy)) and test the
**actual trigger path** — nothing invokes this manually; the S3 event
notification fires it:

```cmd
aws cloudformation describe-stacks --stack-name media-archive-pipeline \
  --query "Stacks[0].Outputs" --output table
# note to take ArchiveBucketName and ArchiveFunctionName from the output

aws s3 cp results.json s3://<ArchiveBucketName>/incoming/results.json

# first invocation includes a cold start (image pull through the ECR
# interface endpoints) - allow 20-30s, not the near-instant local response
aws s3 ls s3://<ArchiveBucketName>/archive/
aws s3 ls s3://<ArchiveBucketName>/incoming/   # original is gone

sam logs --stack-name media-archive-pipeline --name ArchiveFunction --tail
```

## Versioning & rollback

`AutoPublishAlias: live` in `template.yaml` makes SAM publish a new,
immutable numbered Lambda version on every `sam deploy` and move the `live`
alias to point at it — the function is never overwritten in place.

Full verified loop — publish, confirm, roll back, confirm again:

```cmd
# 1. Baseline: confirm current versions and what 'live' points to
aws lambda list-versions-by-function --function-name <ArchiveFunctionName> \
  --query "Versions[].Version" --output table
aws lambda get-alias --function-name <ArchiveFunctionName> --name live

# 2. Change the code, rebuild, redeploy - publishes a new version automatically
sam build --use-container && sam deploy

# 3. Confirm the new version exists and 'live' moved to it
aws lambda list-versions-by-function --function-name <ArchiveFunctionName> \
  --query "Versions[].Version" --output table
aws lambda get-alias --function-name <ArchiveFunctionName> --name live

# 4. Roll back - no rebuild, no redeploy, takes effect immediately
aws lambda update-alias --function-name <ArchiveFunctionName> \
  --name live --function-version <N>

# 5. Confirm the rollback by triggering the function again and checking
#    the logs match the OLDER version's behavior
aws s3 cp test.json s3://<ArchiveBucketName>/incoming/test.json
sam logs --stack-name media-archive-pipeline --name ArchiveFunction --tail
```

Step 5 is the part worth not skipping — `get-alias` proves the pointer
moved, but tailing the logs after a real invocation proves the *behavior*
actually reverted, which is what "reliably roll back" really means.

## Cost analysis

**Workload:** 1,000,000 files/hour, 10 MB average file size. Figures below
use 730 hours/month (AWS's standard monthly average) and current (Sep 2026)
**us-east-1** on-demand pricing — no Savings Plans/commitments assumed. All
numbers are back-of-envelope estimates, not a quote.

**Volume:**
| | |
|---|---|
| Files/month | 1,000,000 × 730 = **730,000,000** |
| Raw data ingested/month | 730,000,000 × 10 MB ≈ **7,300 TB (≈7.3 PB)** |

### Line items

**1. Lambda compute** (512 MB, assumed ~2s average duration to download,
compress, and re-upload a 10 MB file over the S3 endpoint):

| | |
|---|---|
| GB-seconds | 730,000,000 × 0.5 GB × 2s = 730,000,000 |
| Duration cost | 730,000,000 × $0.0000166667 = **$12,167** |
| Request cost | 730,000,000 × $0.20 / 1M = **$146** |
| **Lambda subtotal** | **≈ $12,313/month** |

**2. S3 requests** (incremental cost added by this feature — the GET of the
original object and the PUT of the ZIP; the DELETE is free):

| | |
|---|---|
| GET (730M × $0.0004/1,000) | **$292** |
| PUT (730M × $0.005/1,000) | **$3,650** |
| **S3 requests subtotal** | **≈ $3,942/month** |

**3. S3 storage** — this is the line that matters most, and it's where
naive design breaks down. Assuming ~5:1 compression on JSON (typical for
structured/repetitive text, so archives land at ~20% of original size),
this feature adds **~1,460 TB of new compressed data every single month**,
on top of whatever was stored the month before:

| Storage class | Cost of *one month's* new archive data |
|---|---|
| S3 Standard (tiered: $0.023/$0.022/$0.021 per GB) | **≈ $31,210** |
| S3 Glacier Deep Archive ($0.00099/GB) | **≈ $1,445** |
| *For reference: leaving files uncompressed in Standard* | *≈ $153,850 (i.e. compression alone is already a ~5x saving)* |

Crucially, storage is **cumulative** — month 2's bill includes month 1's
data plus month 2's new data, month 3 includes both prior months, and so
on. Left in S3 Standard, the storage line alone grows by another ~$31k
every month with no ceiling. This makes a **lifecycle policy** (see
Recommendations below) the single most important cost decision here, far
more impactful than any Lambda tuning.

**4. VPC networking** — no NAT Gateway anywhere in this design:

| | |
|---|---|
| S3 Gateway Endpoint | **$0** (no hourly or per-GB charge) |
| ECR + CloudWatch Logs interface endpoints (3 endpoints × 2 AZs × $0.01/hr) | **≈ $44/month** |
| *For reference: a NAT Gateway carrying this feature's S3 traffic instead* | *≈ $0.045/hr + $0.045/GB × ~8.7 PB/month ≈ hundreds of thousands of dollars/month — using Gateway/Interface endpoints instead of NAT is not optional at this scale* |

**5. CloudWatch Logs** (rough estimate at ~500 bytes of log output per
invocation): **≈ $170/month**.

### Estimated final monthly figure

| Scenario | Monthly total (steady state, after lifecycle optimization kicks in) |
|---|---|
| **Naive** — everything stays in S3 Standard | **≈ $47,700 in month 1, and climbing by ~$31k every subsequent month** (unbounded) |
| **Recommended** — archives transition to Glacier Deep Archive shortly after creation | **≈ $17,900/month**, growing only ~$1.4k/month thereafter (bounded, sustainable) |

**Headline estimate: ~$18,000/month**, assuming the cost-saving
recommendations below are applied. Without them, this feature's storage
bill alone becomes unsustainable within a few months.

### Recommendations to reduce cost further

1. **Lifecycle-transition archives to S3 Glacier Deep Archive immediately**
   (or after a short 1-day Standard buffer if you want same-day
   re-download capability first). This is the single biggest lever —
   ~95% cheaper storage than Standard, and it fits the stated use case
   (long-term archive of processing results, not hot data).

2. **Batch multiple JSON files into one ZIP instead of a strict 1:1
   compress-per-object model.** At 730M objects/month, per-object
   overhead (S3 PUT/GET requests, Lambda invocations, and — if you use
   Glacier — per-object lifecycle-transition requests, which alone would
   cost **≈ $36,500/month** billed one-at-a-time at $0.05/1,000
   transitions) dominates the bill more than raw GB stored does.
   Aggregating e.g. 100–1,000 files per archive (via a short buffering
   window, S3 Batch Operations, or Step Functions) cuts Lambda
   invocations, S3 requests, and transition requests by the same factor,
   and JSON compresses better in bulk (shared keys/structure across
   files) than file-by-file. See [Scalability & bottlenecks](#scalability--bottlenecks)
   for the architectural version of this recommendation.

3. **Use Arm/Graviton2 (`Architectures: [arm64]`)** — ~20% lower
   GB-second rate than x86 for the same code, no application changes
   needed for a pure Python workload like this one.

4. **Tune memory allocation.** Lambda CPU scales with memory; a quick
   `MemorySize` sweep (256 MB–1024 MB) against real file sizes often
   finds a sweet spot where the function runs enough faster that total
   cost (duration × memory) goes *down* even as the per-GB-second rate
   goes up.

5. **Right-size CloudWatch Logs retention and volume** — set an explicit
   log retention period (this template can add
   `LoggingConfig`/`RetentionInDays`) and keep log statements minimal, since
   log ingestion cost scales with invocation count just like everything
   else here.

## Scalability & bottlenecks

**Short answer: this design is scalable and cost-efficient at moderate
volume, but the strict 1-file-in → 1-invocation → 1-ZIP-out pattern it
implements (as per Task 1's requirement) is the wrong shape for the
full 1,000,000 files/hour target, and I'd change it before running this in
production at that scale.** Concretely:

- **Per-object overhead dominates at this volume, not data volume.**
  730 million objects/month means every fixed per-request cost (S3 PUT/GET,
  Lambda invocations, and especially per-object Glacier lifecycle
  transitions at $0.05/1,000) gets multiplied 730 million times. The cost
  analysis above already shows this: batching files before compressing
  would cut Lambda invocations, S3 requests, *and* the ~$36.5k/month
  lifecycle-transition cost by whatever factor you batch at, with almost
  no downside for an archival use case that doesn't need per-file
  millisecond turnaround. **My main recommendation is to move from
  "compress on every single ObjectCreated event" to a short buffering
  window** (e.g., an SQS queue fed by S3 events, drained every 1–5 minutes
  by a batching Lambda that zips everything it finds into one archive) or
  **S3 Batch Operations** for a fully managed batch pipeline. This is the
  biggest architectural change I'd make.

- **Lambda concurrency is tight, not comfortable.** At 1,000,000 files/hour
  (≈278 files/sec) and a ~2s average duration, steady-state concurrency
  needs to sit around **~550 concurrent executions** just to keep up —
  and that's the *average*, not burst peaks. The default account-level
  concurrency limit is 1,000, **shared across every Lambda function in the
  account/region**. Any other workload sharing the account, or any burst
  above average ingest rate, risks throttling, which under a
  synchronous S3 → Lambda event source means **retries and eventually
  dropped events** if retries are exhausted before Lambda's internal
  retry budget runs out. Before going anywhere near this volume in
  production I would: request a concurrency limit increase, set
  **Reserved Concurrency** on this function so it can't be starved by
  other functions, and add an **on-failure destination (DLQ/SQS)** so a
  failed record is captured for reprocessing instead of silently lost.
  A queue-backed batching design (previous point) also naturally
  decouples ingest rate from Lambda concurrency, which is the more
  robust long-term fix.

- **S3 request-rate limits are *not* a bottleneck here**, worth calling
  out since it's a common assumption. S3 auto-scales to 3,500
  PUT/COPY/POST/DELETE and 5,500 GET/HEAD requests/sec *per prefix*, and
  our ~278 files/sec (producing roughly that many GETs, PUTs, and
  DELETEs) is comfortably under that per-prefix ceiling. If a future
  batching redesign concentrates writes onto very few keys per second
  this stops being true, but for the current pattern it's fine.

- **Cold starts on a container-image function.** Container images are
  slower to cold-start than ZIP-packaged Lambdas (larger image = slower
  pull, even with the ECR interface endpoints in place). At sustained
  high concurrency this mostly self-heals (most invocations reuse warm
  execution environments once traffic is steady), but sudden traffic
  spikes will see a burst of colder, slower invocations right when
  throughput matters most. Keeping the image as small and dependency-free
  as reasonably possible (this one only needs `boto3`, already in the
  base image) minimizes this.

- **Storage growth is unbounded without lifecycle management** (detailed
  in the cost analysis above) — this is a cost bottleneck more than a
  technical one, but at ~1,460 TB of new compressed data every month it
  will eventually affect operational concerns too (bucket-wide
  operations like inventory reports or cross-region replication get
  slower and more expensive as object count climbs into the billions).
  A lifecycle policy to Glacier Deep Archive addresses both the cost and
  the long-term object-count growth.

- **Single points of coordination.** The VPC/subnet/security-group layout
  here has no single point of failure (2 AZs, no NAT to lose), but the
  Lambda function itself is a single logical consumer of all S3 events.
  At extreme scale, splitting ingestion across multiple prefixes/buckets
  with independent Lambda functions (or the batching redesign above)
  also gives you natural horizontal sharding if one region or one
  function's concurrency ever becomes the ceiling.

**Bottom line:** the VPC/networking and IAM design here scale fine as-is —
they were built cost-consciously from the start (no NAT, scoped IAM,
Gateway/Interface endpoints). The part that needs to evolve before hitting
the full 1M files/hour target is the *processing pattern*: move from
strict per-object compression to a batched/buffered pipeline, add Reserved
Concurrency and a failure destination, and put a lifecycle policy on the
archive prefix from day one.
