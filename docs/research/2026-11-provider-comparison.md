# GPU rental providers: comparison for gpu-ops

Researched 2026-11-10. Prices change often. Re-check before relying on any figure after about a month.

## How to read this

- **Sources.** Every figure links to the page it came from. Every page was fetched on 2026-11-10.
- **Re-checked.** Figures marked ✓ I re-opened on the source page myself. Unmarked figures were read by
  a research subagent and are second-hand: likely right, but not re-checked.
- **Vast.ai prices are live marketplace data.** They come from Vast's public offer-search API
  (`console.vast.ai/api/v0/bundles/`), queried 2026-11-11 04:47 UTC (the evening of 2026-11-10 locally). They are
  a snapshot of up to 64 cheapest available offers per GPU model, not a published rate card.
- **"Not found"** means no page I fetched stated it. It does not mean the feature is absent.

### Terms (same wording each time)

- **VRAM**: the GPU's own memory; the model and its working data must fit in it.
- **On-demand**: you rent a GPU at a fixed hourly price, and nobody can take it from you while it runs.
- **Spot / interruptible**: a cheaper rental the provider can take back at any moment, stopping your job.
- **Secure cloud**: GPUs in provider-vetted data centres.
- **Community cloud**: GPUs in machines owned by third-party hosts, rented through the provider.
- **Egress / ingress**: data leaving / entering the provider's network; some providers charge per GB.
- **Container disk**: scratch disk that is wiped when the machine is stopped or deleted.
- **Network volume**: a disk that outlives any one machine, so model weights can be kept between jobs.
- **Batch inference**: running a model over many prompts in one go, as opposed to chatting with it live.
- **QLoRA**: fine-tuning a model stored in 4-bit form by training only a small add-on, the adapter.
- **Adapter**: the small set of trained weights that QLoRA produces; it is added to the unchanged base model.

## Recommendation

- **First adapter: RunPod.**
- **Runner-up: Modal**, and it is the likely second adapter.
- **Cheapest per hour, if you ever need it: Vast.ai.**

### Why RunPod first

1. **Its model is the common one.** A container on a GPU machine that you reach over SSH is how
   RunPod, Vast.ai, Lambda, Thunder, Prime Intellect and Hyperbolic all work. Building the core
   around this model means most later adapters are small.
2. **Prices are fixed and published, and data transfer is free.** Billing is per second, with no
   ingress or egress fees ✓. You can predict a job's cost before it starts, which the per-job cost
   cap needs.
3. **There is a cheap place to keep model weights.**
   - A network volume costs $0.07/GB-month ✓ and has an S3-compatible API that works without a
     running machine.
   - 150 GB therefore costs about $10.50/month to keep warm, and you can delete it in the months
     you don't need it.
4. **There is a provider-side spending backstop.**
   - The account is prepaid, and machines stop when the balance reaches $0 ✓.
   - There is also a default account limit of $80/hour ✓. That is too high to serve as a job cap,
     but it is a ceiling.
   - The backstop: leave auto-pay off and keep the balance near your monthly budget.
5. **It works from Windows.** `runpodctl` ships a Windows build, and gpu-ops will drive it from WSL anyway.

### Why Modal is the runner-up

1. **$30/month of free credit** on the Starter plan ✓. At your usage, a few jobs a month, much of
   your spend could be covered by it. Modal's list prices are higher than RunPod's, so this only
   wins while the credit lasts.
2. **Strongest safety features of anything reviewed.**
   - A monthly spend limit that stops work.
   - Server-side timeouts, so a machine dies on time even if your PC is off.
   - Tags you can list and filter by.
3. **1 TiB of volume storage free per month** ✓, which covers a large set of model weights.
4. **It is not first because its model is different.**
   - There is no documented SSH, and jobs are defined in Python, with data moved through volumes
     rather than rsync.
   - GPU sandboxes run under the gVisor runtime (a security layer between the container and the
     machine) according to its docs. Whether bitsandbytes and similar libraries work under gVisor
     is not verified.
   - Building it second is also the real test that the core is provider-agnostic.

### Why Vast.ai is not first despite the lowest prices

Vast's live prices are often well below RunPod's. Snapshot medians: A100 80GB SXM $0.83/hr, H100 SXM
$2.54/hr, RTX PRO 6000 $1.34–1.62/hr. Against that:

- **Bandwidth is billed per TB at each host's own rate, in both directions** ✓.
  - In the snapshot, most hosts charged $2.67–5.46/TB, but some charged up to $53/TB.
  - Pulling 150 GB of weights from a $40/TB host adds about $6.
- **Storage keeps billing while a machine is stopped** ✓.
- **Volumes are tied to one physical host** ✓, so you cannot keep a reusable weights store.
- **Host quality varies, which you accepted, since your data is not sensitive.**

A Vast adapter is worth adding once cost per hour matters more than predictability. Its offer data
already includes each host's bandwidth price, so the core could pick the cheapest offer including
transfer.

## Price comparison ($/GPU-hour, single GPU)

| GPU (VRAM) | RunPod secure ✓ | RunPod community ✓ | Vast.ai on-demand, live: min / median | Modal ✓ (per-sec × 3600) | HF Jobs ✓ | Lambda (from 16 Nov) |
|---|---|---|---|---|---|---|
| H100 SXM (80 GB) | 3.99 | 2.69 | 2.07 / 2.54 | 3.95 | – | 4.89 |
| H100 PCIe (80 GB) | 2.89 | 1.99 | – | – | – | 3.79 |
| H100 NVL (94 GB) | 3.19 | 2.59 | 2.34 / 2.54 | – | – | – |
| H200 (141 GB) | 5.29 | 3.59 | 2.90 / 3.49 | 4.54 | 5.00 | – |
| A100 80GB SXM | 1.79 | 1.39 | 0.38 / 0.83 | 2.50 | 2.50 | 3.19 (8× only) |
| A100 80GB PCIe | 1.79 | 1.19 | 0.40 / 0.74 | – | – | – |
| RTX PRO 6000 (96 GB) | 2.49 | 1.69 | 1.00–1.34 / 1.34–1.62 † | 3.03 | 2.75 | – |
| L40S (48 GB) | 1.09 | 0.79 | 0.42 / 0.77 | 1.95 | 1.80 | – |
| RTX 6000 Ada (48 GB) | 0.99 | 0.74 | 0.54 / 0.65 | – | – | – |
| RTX A6000 (48 GB) | 0.59 | 0.33 | 0.35 / 0.47 | – | – | 1.09 |
| RTX 5090 (32 GB) | 1.19 | 0.69 | – ‡ | – | – | – |
| Cheapest GPU (RTX A5000, 24 GB) | 0.27 | 0.16 | – | – | – | – |

**Table notes**

- † Vast lists three RTX PRO 6000 variants: Workstation, Server and Max-Q. The Max-Q started at $0.64.
- ‡ Not queried per model. The broad sample showed RTX 5090 from $0.40.
- **Modal** adds CPU and RAM charges on top of the GPU price ✓. It has no spot tier.
- **Lambda's** page says these prices take effect 16 Nov 2026, so they are not live today.
- **Spot / interruptible:**
  - RunPod's API has an `interruptible` flag, but no interruptible price was found on its pricing page.
  - Vast's lowest interruptible minimum bids were about 30–60% of on-demand, for example A100 SXM $0.14 and H100 SXM $1.00.
  - Together GPU clusters list H100 preemptible at $1.99.
- **Multi-GPU:**
  - HF Jobs publishes multi-GPU prices: 4× A100 $10.00, 2× H200 $10.00, 2× RTX PRO 6000 $5.50 ✓.
  - RunPod and Vast price multi-GPU per GPU. No multi-GPU discount was found.
  - Lambda: 2× H100 at $4.79 per GPU.

**Sources**

- RunPod: [runpod.io/pricing](https://www.runpod.io/pricing) ✓
- Vast.ai: live API data (see "How to read this")
- Modal: [modal.com/pricing](https://modal.com/pricing) ✓
- HF Jobs: [huggingface.co/docs/hub/jobs-pricing](https://huggingface.co/docs/hub/jobs-pricing) ✓
- Lambda: [lambda.com/pricing](https://lambda.com/pricing)
- Together: [together.ai/pricing](https://www.together.ai/pricing)

## Provider by provider

### RunPod

- **Billing.**
  - Per second for compute and storage. Network volumes are billed hourly ✓ ([docs](https://docs.runpod.io/pods/pricing)).
  - Deploying needs at least 1 hour of credit for the chosen setup ✓.
  - Savings plans are discontinued ✓.
- **Storage** ✓ ([pricing](https://www.runpod.io/pricing)):
  - Container disk: $0.10/GB-month.
  - Volume disk: $0.10/GB-month while running, $0.20/GB-month while stopped.
  - Network volume: $0.07/GB-month (under 1 TB) and $0.05/GB-month (over 1 TB). High-performance tier: $0.14/GB-month.
  - A stopped machine does not bill for container disk ✓.
- **Egress and ingress:** none ✓.
- **Getting weights onto the GPU:**
  - Pull from Hugging Face on the machine (free transfer, but you pay for GPU time while it downloads).
  - Or keep them on a network volume. The volume can be loaded through its S3 API without a running machine.
  - A volume must be attached when the machine is created
    ([storage](https://docs.runpod.io/pods/storage/types), [S3 API](https://docs.runpod.io/storage/s3-api)).
  - The S3 API is available in 15 data centres. Files over 500 MB need multipart upload.
  - "Cached models" exist for Serverless only ([docs](https://docs.runpod.io/serverless/endpoints/model-caching)).
- **API and CLI:**
  - `runpodctl` (open source; pod, network-volume, gpu, billing and ssh commands), a `runpod` Python SDK, and a REST API
    ([runpodctl](https://docs.runpod.io/runpodctl/overview), [POST /pods](https://docs.runpod.io/api-reference/pods/POST/pods)).
  - No create-time maximum runtime. Tags exist: the API-key screen (seen 2026-11-11) has a Tags permission, "Tags and the resources attached to them". gpu-ops will tag its own machines `gpuops` and also use a `gpuops-` name prefix as a fallback.
  - REST v1 retires on 15 Nov 2026 ([overview](https://docs.runpod.io/api-reference/overview)), so the adapter targets REST v2.
- **Self-termination:**
  - The docs show `runpodctl pod stop/delete $RUNPOD_POD_ID` ✓ ([manage pods](https://docs.runpod.io/pods/manage-pods)).
  - The docs do not say whether the CLI and an API key are present inside a pod. Forum posts say a
    limited key is injected. **Not verified; the kill test must prove it.**
- **Containers:** any image from Docker Hub, GHCR or ECR; SSH supported ([overview](https://docs.runpod.io/pods/overview)).
- **Spending:** prepaid. Machines stop at $0 ✓. Default limit $80/hour ✓. Low-balance emails and auto-pay exist.
- **Community host security:**
  - Community cloud is a "vetted peer-to-peer system". Secure cloud runs in Tier 3/4 data centres.
  - The terms of service put community use at your own risk ([ToS](https://www.runpod.io/legal/terms-of-service)).
- **Windows:** `runpodctl` has a Windows build. The Python SDK documents a Windows setup.
- **Quirk:** a stopped machine's GPU can be taken by someone else, so the machine may restart with
  zero GPUs ([FAQ](https://docs.runpod.io/references/faq)). gpu-ops will delete machines, not stop them.

#### Live API check (2026-11-11, read-only, with your restricted key)

- **REST v2 base URL:** `https://api.runpod.io`. Its OpenAPI spec is public at
  `https://api.runpod.io/v2/openapi.json`. Each operation names the key permission it needs
  (`x-runpod-scope`), for example `pods:write` and `billing:read`.
- **Call results:**

  | Call | Result |
  |---|---|
  | `GET /v2/pods` | 200, 0 pods |
  | `GET /v2/catalog/gpus` | 200, 48 GPU types |
  | `GET /v2/catalog/datacenters` | 200 |
  | `GET /v2/billing` | 200, $0 so far |
  | `GET /v2/billing/pods` | 200, $0 so far |
  | `GET /v2/account/ssh-keys` | 403. Expected: Authentication permission is off |
  | `GET /v2/account` | 404 |

- **Catalog prices match the pricing page** (for example RTX 5090 $0.69 community, A100 80GB PCIe $1.19,
  H200 $3.59).
- **Catalog quirk:** some tiers show a placeholder $0.50 price where that tier's `maxCount` is 0.
  The adapter must ignore any price on a tier with `maxCount` 0.
- **Confirmed 2026-11-12: `maxCount` is not stock.** It is the largest machine's GPU count. A create for an
  RTX A5000 with `maxCount` 10 was refused: "no longer any instances available".
  - Real stock comes from `GET /v2/catalog/gpus?include=AVAILABILITY&product=POD&cloud=…&count=…`, as `availability` NONE/LOW/MEDIUM/HIGH.
  - At that moment community cloud had **0** GPU types in stock, and the cheapest in-stock secure GPU was $0.59/hr (A40, RTX A6000).
  - Advertised low prices are often unavailable, so real costs run above the price table.
- **Rate limits** (from response headers): 180 calls/minute, 7,200/hour, 86,400/day.
- **What the create-pod request does not accept:**
  - Tags, despite the Tags key permission in the console.
  - A maximum runtime.
  - No published endpoint writes tags or reads account limits yet. So gpu-ops finds its machines
    by the `gpuops-` name prefix, and stops them on time with its own dead-man timer.
- **What it does accept:** `image`, `cmd`/`entrypoint`, `env`, `ports`, `disk`, `gpu.{id,count}`,
  `cloud` (SECURE by default), `dataCenterIds` and `mounts`.
- **SSH key per job:** `startSsh` injects a `PUBLIC_KEY` env var unless the request sets one. So a
  per-job SSH key can be passed in at creation without the Authentication permission.
- **Billing history:** `GET /v2/billing/pods?podId=…` gives per-pod billing, which can check
  gpu-ops' own cost estimates.

### Vast.ai

- **Prices:** live marketplace. See the table, and "How to read this" for how they were gathered.
- **Rental types:**
  - On-demand.
  - Interruptible, "often 50%+ cheaper".
  - Reserved, up to 50% off ([rental types](https://docs.vast.ai/documentation/instances/rental-types)).
- **Billing** ✓ ([billing](https://docs.vast.ai/documentation/reference/billing)):
  - Per second, and not while the machine shows "Loading".
  - Storage bills while the machine is stopped.
  - Bandwidth is billed per byte, both directions, at each host's rate.
- **Bandwidth in the snapshot:** median download cost $2.67–5.46/TB depending on GPU, worst
  $53/TB. Median download speed varied from about 650 to 17,000 Mbps by GPU model.
- **Storage:**
  - Container disk is fixed at creation.
  - Volumes are tied to one physical host.
  - Cloud Sync to S3 or Drive is available
    ([storage](https://docs.vast.ai/documentation/instances/storage/types)).
- **API and CLI:** `pip install vastai` gives the CLI and the Python SDK. There is a REST API, and API keys can be scoped
  ([CLI](https://docs.vast.ai/cli/get-started)). Offer search is public and needs no key (used above).
- **Self-termination:**
  - A special instance API key is pre-installed, and `vastai stop instance $CONTAINER_ID` works from inside the machine ✓
    ([FAQ](https://docs.vast.ai/documentation/reference/faq/instances)).
  - The doc page shows `stop` but not `destroy`.
- **Spending** ✓:
  - Prepaid. Machines stop at $0 after a grace period.
  - Without a saved card, the machines and their data are destroyed.
  - No hard spend limit is documented.
- **Community host security:**
  - Verified datacentre hosts claim ISO 27001.
  - Other hosts "may have less formal security measures".
  - Containers isolate renters from each other, not from the host
    ([security](https://docs.vast.ai/documentation/reference/faq/security)).
- **Windows:** `pip install vastai` works on Windows. The curl installer covers Linux, macOS and WSL.

### Modal

- **Pricing** ✓:
  - Starter plan: $0/month plus compute, $30/month free credits, 10 concurrent GPUs.
  - Team plan: $250/month with $100 credits (agent-read).
  - CPU: $0.0000131 per core-second. RAM: $0.00000222 per GiB-second.
  - Volumes: $0.09/GiB-month, 1 TiB/month free.
  - Egress: $0.04/GiB, 1 TiB/month free.
- **Spend limits:** a workspace budget and a spend limit. Work that would cost money out of pocket stops when the limit is reached
  ([budgets](https://modal.com/docs/guide/budgets); agent-read).
- **Jobs:**
  - Python-defined functions with a 1 s to 24 h timeout ([timeouts](https://modal.com/docs/guide/timeouts)).
  - Or Sandboxes: GPU only under gVisor, lifetime up to 24 h, `idle_timeout`, up to 10 tags, listable by tag, registry
    images ([sandboxes](https://modal.com/docs/guide/sandboxes)).
  - Up to 8 GPUs per container ([GPU guide](https://modal.com/docs/guide/gpu)).
- **Weights:**
  - `modal.Volume`, designed for up to 2.5 GB/s ([volumes](https://modal.com/docs/guide/volumes)).
  - Or mount S3, R2 or GCS buckets ([bucket mounts](https://modal.com/docs/guide/cloud-bucket-mounts)).
- **Gaps:** no documented SSH. No official Windows statement (it is a pip package; it works in WSL).

### Hugging Face Jobs: worth knowing, not first

- **Prices and billing** ✓: per minute, only while Starting or Running. Failed jobs suspend
  themselves. Default timeout is 30 minutes, set with `--timeout`.
- **No spend cap for individuals.** Monthly spend limits exist only for Enterprise resource groups ✓.
- **Its standout feature for teacher jobs:** a Hugging Face model repo can be **mounted directly as a
  volume**, so there is no download step and no weights store to pay for
  ([jobs guide](https://huggingface.co/docs/huggingface_hub/guides/jobs); agent-read).
- **Other features:** bring-your-own Docker image, `hf jobs ssh`, and labels with filtering.
- **No H100 and no spot tier.**

### Lambda

- **Prices** start 16 Nov 2026. 1-minute billing, invoiced weekly.
- **Filesystems:**
  - Must be in the same region as the machine and attached at launch.
  - Keep billing after the machine is deleted.
  - Not charged for ingress or egress.
- **API:** REST, with launch limited to 1 call per 12 s. No spend limits found. VMs with SSH and Docker preinstalled
  ([pricing](https://lambda.com/pricing), [billing](https://docs.lambda.com/public-cloud/billing/),
  [API](https://docs.lambda.com/api/cloud)).
- **Not first:** the highest prices of the SSH providers. No L40S, RTX 6000 Ada or RTX PRO 6000 listed, and the only A100 80GB is in 8× bundles.

### Together AI and Fireworks: fine-tuning APIs

These are a different kind of product. You upload a dataset, they train, and you download the
result. Your `train_qlora.py` would not run there.

- **Together** ([pricing](https://www.together.ai/pricing), [models](https://docs.together.ai/docs/fine-tuning-models)):
  - Price per million training tokens with a per-job minimum. Llama 3.3 70B SFT costs $2.03 per million (minimum $4).
  - LoRA for about two dozen models, full fine-tuning for 10.
  - The trained adapter or merged weights can be downloaded with `tg fine-tuning download`.
  - **Use when:** a fine-tune of a supported model is far bigger than the 5090 handles, and the
    model list fits. Pay per token, with no machines to manage.
- **Fireworks:**
  - Managed fine-tuning **no longer accepts new jobs**. It has been replaced by a Training API whose rates were not found
    ([overview](https://docs.fireworks.ai/fine-tuning/overview)).
  - Its pricing page still advertises managed training, so the two pages contradict each other.
  - Weight download: not found. **Not recommended now.**

### Briefly checked

- **Thunder Compute** ([pricing](https://www.thundercompute.com/pricing)):
  - H100 SXM $3.20/hr. A100 80GB $1.09/hr.
  - Billing per minute. Free egress. Never preempted.
  - A CLI and an MCP server.
- **Hyperbolic** and **Prime Intellect:** both offer SSH and a CLI or API. Their pricing pages
  returned 404, so no prices were verified.
- **Shadeform** and **TensorDock:** pricing pages returned 404. Nothing verified.

## Which provider for which job (list prices, today)

### 1. Batch teacher inference (80–141 GB VRAM)

- **Cheapest per hour:** Vast H200 (from $2.90) or H100 (from $2.07).
- **Predictable:** RunPod community H200 at $3.59 or A100 80GB at $1.19.
- **Least weight handling:** HF Jobs H200 at $5.00 with the model mounted from the Hub.
- **Within the free credit:** Modal, until the monthly $30 runs out.

### 2. Fine-tune bigger than the 5090 handles

- **A100 80GB:** Vast $0.38–0.83, or RunPod community $1.19.
- **96 GB RTX PRO 6000:** Vast about $1.00–1.62, or RunPod community $1.69.
- **Your QLoRA script unchanged:** RunPod or Vast.
- **Supported model and per-token pricing is fine:** Together.

### 3. One-hour serving session

- **Needs an exposed port.** RunPod (ports set at creation) or HF Jobs ($0.01/hr extra per exposed port) ✓.
- **Cost** is one GPU-hour at the rates above.

### The two proof jobs in the plan

Rough estimates, my arithmetic:

- **Smoke job:** RunPod community RTX A5000 at $0.16/hr for about 10 minutes, about **$0.03**.
- **HS-AI-Coach QLoRA proof:**
  - The 4B model peaked at 10.7 GB locally, so any 24–32 GB GPU fits it.
  - RunPod community RTX 5090 at $0.69/hr for 30–45 minutes, including the 8 GB model
    download and setup, is about **$0.35–0.55**.

## Moving 50–150 GB of model weights

| Route | Cost to keep | Cost per job | Notes |
|---|---|---|---|
| Pull from the HF Hub on every job (RunPod) | $0 | GPU time while downloading | Free transfer. At about 1 Gbps (assumed; not measured), 150 GB takes about 20 min, roughly $0.90 on a $2.69/hr H100. |
| RunPod network volume | 150 GB × $0.07 = $10.50/month ✓ | Near zero | Same data centre as the machine. Pays off at roughly 12 or more large-model jobs a month (my arithmetic). |
| Vast: pull on every job | $0 | 150 GB × host rate: $0.40 at median, up to $8 | gpu-ops can filter offers by bandwidth price. |
| Modal volume | Free up to 1 TiB/month ✓ | Near zero | Needs the Modal adapter. |
| HF Jobs Hub mount | None found | None found | No download step. Throughput not documented. |

**Conclusion:**
- At a few jobs a month, **pulling from the Hub on every job is cheapest**. This is what v1 does.
- Add a provider's volume route only when the per-run download time becomes the bigger cost. The
  spend tracker will show when that happens.

## Spend controls and safety layers

| Provider | Hard spend cap | Server-side runtime limit | Self-terminate from inside | Find my machines |
|---|---|---|---|---|
| RunPod | Prepaid balance; $80/hr account limit ✓ | No | Documented command; key presence not verified | Tags, plus a name prefix |
| Vast.ai | Prepaid balance | No | Yes, with an injected key ✓ | Name or label (not verified) |
| Modal | Monthly spend limit | Yes (timeout, up to 24 h) | n/a | Tags |
| HF Jobs | Enterprise only | Yes (`--timeout`, 30 min default) ✓ | n/a | Labels |

**Practical rule:** don't enable auto-pay on any provider, and keep each prepaid balance near one
month's budget. Then the worst case is losing that balance, not an open-ended bill.

## Rent vs buy: what the spend tracker needs from you

You have done the buying research yourself. To keep comparing as you go, `gpu-ops spend` needs
two numbers from you, in the user-level config:

1. **The card's price** (RTX PRO 6000 Max-Q) and any other costs you count, such as power.
2. **A warning point**, for example "warn me when trailing three-month spend reaches $X a month".

It will also report:
- **Jobs a single card would not have replaced:** those needing more than 96 GB or more than one GPU.
- **Rental price of the same card:** the Vast.ai RTX PRO 6000 Max-Q median was $1.34/hr in this
  snapshot, a useful comparison.
