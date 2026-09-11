# New Grad SWE Alert Engine

A production-grade alerting system that turns time-sensitive Instagram job-drop stories into reliable, intelligent mobile notifications.

`Python` · `APScheduler` · `Redis` · `Gemini` · `ffmpeg` · `Docker` · `Azure` · `Terraform`

## Why I Built This

[@zero2sudo](https://www.instagram.com/zero2sudo/) is often one of the first places I see competitive new-grad software engineering openings announced. For roles that attract thousands of applicants, finding the posting early can be the difference between applying near the front of the queue and discovering it after the opportunity has already spread everywhere else.

The problem was that Instagram notifications were too inconsistent for something this time-sensitive. Story notifications frequently arrived late or did not arrive at all, and even a working notification still required me to open every story and decide whether it contained an actual new-grad SWE opening.

I built this system to replace that unreliable workflow with an always-on pipeline. It checks the public story feed throughout the day, recognizes which stories are genuine job drops, ignores unrelated content, and sends a high-priority phone notification that opens the original Instagram story. This creates a practical advantage during a competitive job search while turning a real frustration into an opportunity to learn how production systems fit together.

## What It Does

- Polls the target account every minute during waking hours and once per hour overnight.
- Deduplicates story media before doing expensive work, so the same post is not classified or announced repeatedly.
- Downloads images or extracts representative video frames with `ffmpeg`.
- Uses Gemini multimodal classification to distinguish live new-grad SWE openings from memes, advice, promotions, senior roles, and other unrelated stories.
- Sends immediate high-priority alerts during the day, applies company/urgency overrides overnight, and batches ordinary overnight results for the morning.
- Runs continuously on Azure and recovers automatically from application crashes and VM reboots.

## What I Learned and Applied

| Area | How it appears in this project |
| :--- | :--- |
| API integration and scraping | Integrated a third-party Instagram downloader, adapted inconsistent external payloads, handled authentication, quotas, retries, rate limits, and expiring CDN URLs. |
| Reliable state and concurrency | Used Upstash Redis, TTLs, atomic Lua scripts, batched claims, idempotency, deduplication, queues, and dead-letter handling. |
| Applied AI | Built a strict multimodal Gemini classifier with structured JSON output, schema validation, deterministic prompting, quota pacing, and retry behavior. |
| Media processing | Downloaded images and used `ffprobe`/`ffmpeg` to extract representative frames from video stories. |
| Backend and system design | Designed a multi-stage ingest → claim → classify → escalate pipeline with quiet-hour behavior and explicit failure recovery. |
| Cloud and Linux operations | Provisioned and operated an Ubuntu VM, networking, SSH controls, swap, process logs, resource monitoring, and reboot recovery in Azure. |
| Containers and infrastructure as code | Packaged the worker with Docker and automated Azure resources with Terraform and cloud-init. |
| Testing and observability | Added 64 automated tests, live smoke tests, structured logs, resource checks, Terraform drift detection, and crash/reboot verification. |

## Live Deployment

The production deployment was provisioned and verified on September 11, 2026:

- Azure for Students `Standard_B2ats_v2` Ubuntu VM in Mexico Central.
- Docker container `swe-alerts` with `unless-stopped` restart policy; Docker itself starts at boot.
- Active polling every **60 seconds** from 08:00–23:00 ET and quiet polling every **3600 seconds** from 23:00–08:00 ET.
- Upstash Redis remains externally hosted and supplies shared deduplication, processing claims, the quiet-hours queue, and the failed-classification queue.
- Live verification covered RapidAPI ingestion, a real Gemini image classification, ntfy emergency and quiet probes, repeated scheduled polls, application-process crash recovery, and a complete Azure VM reboot.
- Current infrastructure matches Terraform with no drift, and all **64 tests** pass.

See the [Azure operations runbook](infra/azure/README.md) for deployment, logs, troubleshooting, costs, and teardown.

---

## Architecture Overview

```
                                    +-----------------------+
                                    | Azure VM + Docker     |
                                    | APScheduler 60s/3600s |
                                    +-----------+-----------+
                                                |
                                                v
+------------------------+          +-----------+-----------+
| Instagram Story Source | -------> |   Ingestion Layer     |
| RapidAPI / Apify       |          | Adapter -> StoryAsset |
+------------------------+          +-----------+-----------+
                                                |
                                                v
                                    +-----------+-----------+
                                    |  Upstash Redis State  |
                                    | Batch Claim + Dedup   |
                                    +-----------+-----------+
                                                |
                                                v
                                    +-----------+-----------+
                                    |   Media Processor     |
                                    |  FFmpeg / Keyframe    |
                                    +-----------+-----------+
                                                |
                                                v
                                    +-----------+-----------+
                                    |  Gemini Multimodal    |
                                    |  Structured JSON      |
                                    +-----------+-----------+
                                                |
                       +------------------------+------------------------+
                       |                                                 |
                       v [Negative]                                      v [Positive]
            +--------------------+                           +-----------+-----------+
            | Redis: mark seen   |                           |    Escalator Routing  |
            +--------------------+                           +-----------+-----------+
                                                                         |
                                                +------------------------+------------------------+
                                                |                                                 |
                                                v [Active OR Tier-1 Override]                     v [Quiet Hours Non-Tier-1]
                                     +--------------------+                           +--------------------+
                                     |  Emergency Siren   |                           |  Queue to Redis    |
                                     |  ntfy P5 / Push P2 |                           |  + Silent Ping     |
                                     +--------------------+                           +---------+----------+
                                                                                                |
                                                                                                v [08:00 ET]
                                                                                      +--------------------+
                                                                                      | Morning Burst Alert|
                                                                                      +--------------------+
```

---

## Core Features

- **Multi-Provider Scraping & Resilient Ingestion**:
  - **Instagram Downloader (RapidAPI, production)**: Calls `/convert?url=https://www.instagram.com/stories/{username}/` to discover public story media. It derives stable deduplication IDs from Instagram CDN filenames because the provider does not return native story IDs.
  - **Generic RapidAPI**: Configurable adapter with URL/body template formatting, capped exponential backoff on HTTP 429/5xx, and dynamic username-to-`user_id` resolution (`RAPIDAPI_USER_ID_URL_TEMPLATE`).
  - **Apify Standby**: Standby adapter maintaining persistent worker connections to eliminate per-tick cold starts.
  - **Universal Normalization**: Decoupled normalizer ([`app/scrapers/normalize.py`](app/scrapers/normalize.py)) parsing Instagram private API schemas, RapidAPI wrappers, tray formats, link stickers, and image candidates into unified [`StoryAsset`](app/models.py) objects.

- **Intelligent Media Processing ([`app/media.py`](app/media.py))**:
  - Static images: Automatically downloads the highest-fidelity resolution candidate.
  - Video stories: Runs `ffprobe` to accurately calculate clip duration, seeking to the midpoint (`duration / 2.0`) and invoking `ffmpeg` to extract a clean, representative keyframe.
  - Deterministic cleanup: Guarantees zero disk leakage by isolating media downloads to disposable temporary directories.

- **Strict Multimodal Classification ([`app/classifier.py`](app/classifier.py))**:
  - Leverages Google Gemini (`gemini-3.6-flash`) with strict JSON schema enforcement ([`CLASSIFICATION_JSON_SCHEMA`](app/models.py)) at `temperature: 0.0`.
  - Rigorous prompt filtering rejects memes, lifestyle posts, quant/math riddles, generic career advice, bootcamps, and senior/manager roles.
  - Identifies company name and role title, reads visible application URLs when possible, and evaluates urgency scores ($1 \dots 5$). The production downloader does not expose link-sticker destinations, so alerts fall back to the target Instagram story URL when no direct URL is visible.
  - Spaces Gemini calls by at least 13 seconds to respect the configured model quota when several unseen stories arrive in one poll.
  - Gracefully recovers from API rate limits and classifies configuration errors ([`ClassifierConfigError`](app/classifier.py)) so claims are freed rather than poisoned.

- **Two-Phase Atomic Deduplication & State ([`app/state.py`](app/state.py))**:
  - Powered by Upstash Redis with atomic Lua scripts (and fallback native pipelines).
  - Claims a poll's story IDs in one batched Redis Lua round trip, so only unseen stories download media or reach Gemini.
  - Phase 1: Claims a story using `SET story:claiming:<id> 1 NX EX 180` to prevent duplicate processing across concurrent workers.
  - Phase 2: Upon successful evaluation or alert dispatch, promotes the record to `SET story:seen:<id> 1 EX 172800` (48-hour TTL) and removes the claim key.
  - Transient failures (e.g. network drops during frame download) delete the claim key immediately to trigger instant re-processing on the next tick.
  - Failures in classification are pushed to a Dead Letter Queue (`queue:failed_classifications`) alongside a low-priority debug alert.

- **Smart Escalation & Quiet Hours Management ([`app/escalation.py`](app/escalation.py))**:
  - **Active Hours** (08:00–23:00 ET): Triggers immediate high-priority alerts with sound sirens.
  - **Quiet Hours** (23:00–08:00 ET):
    - **Tier-1 Override**: High-urgency drops ($\ge 5$) or drops from target companies (`Google`, `Meta`, `Stripe`, `Jane Street`, `Citadel`, `OpenAI`, `Nvidia`, `Apple`, `Amazon`, `Microsoft`, `Netflix`) bypass quiet hours and fire immediate sirens.
    - Non-override drops are pushed to `queue:quiet_hours` and a muted priority-1 notification is sent.
  - **Morning Burst** (08:00 ET): Drains all overnight queued items, packs them into a single consolidated emergency siren notification, and dispatches any overflow as individual low-priority updates.

- **Pluggable Push Notification Backends**:
  - **ntfy** (Default, zero cost): Dispatches Priority 5 emergency sirens (`tags: rotating_light,siren`) with direct click-through URLs (`click`), or Priority 1 silent updates (`tags: mute`). Compatible with iOS and Android.
  - **Pushover**: Dispatches Priority 2 emergency alerts (30s retry, 3600s expiration, siren audio) or Priority -1 quiet background updates.

- **Dynamic Polling Scheduler ([`app/main.py`](app/main.py))**:
  - Powered by APScheduler `BlockingScheduler`.
  - Production polling dynamically throttles to **60 seconds** during active day hours and **3600 seconds** during quiet overnight hours.
  - Cron triggers at 23:00 and 08:00 ET automatically adjust poller cadence and trigger morning batch delivery.
  - The scheduler lives inside the long-running Python process. It is not cron or GitHub Actions; Azure keeps the VM online, Docker keeps the container online, and APScheduler decides when each poll runs.

---

## Project Structure

```
newgrad-swe-alerts/
├── app/
│   ├── __init__.py
│   ├── classifier.py         # Gemini multimodal structured classification & prompt
│   ├── config.py             # Pydantic Settings, validation, and URL normalization
│   ├── escalation.py         # Quiet hours, override logic, ntfy & Pushover backends
│   ├── http.py               # Shared pooled HTTPX client and timeout policies
│   ├── logging_setup.py      # Structured console logging (sanitizes tokens & payloads)
│   ├── main.py               # APScheduler daemon entrypoint & signal handling
│   ├── media.py              # Frame download, ffprobe duration probe, ffmpeg keyframe
│   ├── models.py             # Domain models (StoryAsset, Classification, DLQ) & schemas
│   ├── notify_test.py        # Standalone probe to verify mobile push notifications
│   ├── pipeline.py           # Single-tick orchestration (ingest → claim → classify → escalate)
│   ├── poll_once.py          # Standalone CLI to execute one full end-to-end poll tick
│   ├── state.py              # Upstash Redis state manager, Lua scripts, DLQ, and mutexes
│   └── scrapers/
│       ├── __init__.py
│       ├── apify_standby.py  # Apify actor standby HTTP client
│       ├── instagram_downloader.py # Low-cost RapidAPI downloader adapter
│       ├── base.py           # Scraper protocol and provider factory
│       ├── normalize.py      # Vendor-agnostic Instagram JSON payload normalizer
│       └── rapidapi.py       # RapidAPI stories client with dynamic user_id lookup
├── tests/
│   ├── conftest.py           # Settings fixtures and fakeredis client
│   ├── test_classifier.py    # Unit tests for Gemini prompt, retry, and JSON parsing
│   ├── test_config.py        # Config validation, Upstash URL normalization tests
│   ├── test_escalation.py    # Quiet hours, override matching, morning burst tests
│   ├── test_imports.py       # Package sanity checks
│   ├── test_media.py         # Image resolution selection and video keyframe tests
│   ├── test_normalize.py     # Schema normalization tests across vendor shapes
│   ├── test_pipeline.py      # End-to-end tick execution and error handling tests
│   ├── test_scheduler.py     # Job registration and dynamic rescheduling tests
│   ├── test_scrapers.py      # RapidAPI and Apify adapter tests
│   └── test_state.py         # Redis claim transitions, Lua scripts, DLQ tests
├── infra/azure/              # Terraform, cloud-init, deploy helper, and Azure runbook
├── Dockerfile                # Minimal container image (python:3.12-slim + ffmpeg)
├── pytest.ini                # Pytest execution configuration
├── requirements.txt          # Production runtime dependencies
└── requirements-dev.txt      # Testing and development tooling
```

---

## Environment Configuration

Copy the sample environment template:

```bash
cp .env.example .env
```

### Configuration Reference

| Variable | Required | Default | Description |
| :--- | :---: | :---: | :--- |
| `SCRAPER_PROVIDER` | No | `rapidapi` | Ingestion engine: `instagram_downloader`, `rapidapi`, or `apify_standby`. |
| `TARGET_IG_USERNAME` | No | `zero2sudo` | Target Instagram handle to poll (without `@`). |
| `TARGET_IG_USER_ID` | No | `""` | Optional static Instagram numerical ID (bypasses lookup). |
| `RAPIDAPI_KEY` | Yes* | `""` | RapidAPI application key (*required for either RapidAPI provider). |
| `RAPIDAPI_HOST` | Yes* | `""` | RapidAPI host header. For the downloader: `instagram-downloader-download-instagram-stories-videos4.p.rapidapi.com`. |
| `RAPIDAPI_METHOD` | No | `GET` | HTTP verb for the generic RapidAPI stories endpoint (`GET` or `POST`). Ignored by `instagram_downloader`. |
| `RAPIDAPI_URL_TEMPLATE`| No | `https://{host}/user/stories?username={username}` | Generic adapter URL template supporting `{host}`, `{username}`, and `{user_id}`. Ignored by `instagram_downloader`. |
| `RAPIDAPI_BODY_TEMPLATE`| No | `""` | Optional JSON body template for POST endpoints. |
| `RAPIDAPI_ITEMS_PATH` | No | `data.stories` | Generic adapter dot-notation JSON path to the story array. |
| `RAPIDAPI_USER_ID_URL_TEMPLATE` | No | `https://{host}/user_id_by_username?username={username}` | Endpoint template used to resolve username to numerical ID. |
| `APIFY_API_TOKEN` | Yes* | `""` | Apify API token (*required if `apify_standby`). |
| `APIFY_STANDBY_URL` | Yes* | `""` | Full URL for persistent Apify Standby actor. |
| `APIFY_STANDBY_METHOD` | No | `POST` | HTTP verb used for the Apify Standby actor (`GET` or `POST`). |
| `UPSTASH_REDIS_URL` | **Yes** | — | Upstash Redis connection string (`rediss://default:...@...upstash.io:6379`). Auto-cleans `redis-cli` paste strings. |
| `GEMINI_API_KEY` | **Yes** | — | Google Gemini API key. |
| `GEMINI_MODEL` | No | `gemini-3.6-flash` | Gemini multimodal vision model name. |
| `GEMINI_MIN_REQUEST_INTERVAL_SECONDS` | No | `13` | Minimum spacing between Gemini calls; keeps free-tier traffic below five requests/minute. |
| `NOTIFY_PROVIDER` | No | `ntfy` | Push notification service: `ntfy` or `pushover`. |
| `NTFY_BASE_URL` | No | `https://ntfy.sh` | ntfy instance server base URL. |
| `NTFY_TOPIC` | Yes* | `""` | Secret topic name for ntfy (*required if `ntfy`). |
| `NTFY_TOKEN` | No | `""` | Optional authentication token for protected ntfy topics. |
| `PUSHOVER_APP_TOKEN` | Yes* | `""` | Pushover Application API token (*required if `pushover`). |
| `PUSHOVER_USER_KEY` | Yes* | `""` | Pushover User Key (*required if `pushover`). |
| `OVERRIDE_COMPANIES` | No | *(Tier 1 list)* | Comma-separated companies that immediately break quiet hours. |
| `QUIET_HOURS_START_HOUR`| No | `23` | Hour in 24h format when quiet mode begins (23 = 11 PM). |
| `QUIET_HOURS_END_HOUR` | No | `8` | Hour in 24h format when quiet mode ends & morning burst fires (8 = 8 AM). |
| `TIMEZONE` | No | `America/New_York`| Timezone for quiet hours and log timestamps. |
| `ACTIVE_POLL_INTERVAL_SECONDS` | No | `45` | Code default for active hours; the production `.env` and sample use `60` for the Pro quota. |
| `QUIET_POLL_INTERVAL_SECONDS` | No | `300` | Code default for quiet hours; the production `.env` and sample use `3600`. |
| `CLAIM_TTL_SECONDS` | No | `180` | In-flight story processing lock timeout. |
| `STORY_TTL_SECONDS` | No | `172800` | Processed story deduplication retention (48 hours). |
| `LOG_LEVEL` | No | `INFO` | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

---

## Setup & Prerequisites

### 1. Local Dependencies

- Python 3.12 or newer
- `ffmpeg` and `ffprobe` (required for keyframe extraction from video stories)

```bash
# macOS (Homebrew)
brew install ffmpeg

# Debian / Ubuntu
sudo apt-get update && sudo apt-get install -y ffmpeg
```

### 2. Python Environment Setup

```bash
# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install production and development dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

### 3. Provider Credentials

1. **Upstash Redis**:
   - Create a serverless Redis database at [Upstash Console](https://console.upstash.com/).
   - Under **Connect** $\rightarrow$ **redis-py**, copy the URI starting with `rediss://default:...`.
2. **Google Gemini**:
   - Create an API key at [Google AI Studio](https://aistudio.google.com/).
3. **Mobile Notifications (ntfy)**:
   - Install the **ntfy** app on your phone ([iOS App Store](https://apps.apple.com/us/app/ntfy/id1625396347) / [Google Play](https://play.google.com/store/apps/details?id=io.heckel.ntfy)).
   - Choose a unique, unguessable topic name (e.g. `swe-drops-x92f`) and subscribe to it in the app.
   - Set `NTFY_TOPIC=swe-drops-x92f` in your `.env`.
4. **RapidAPI**:
   - Subscribe to **Instagram Downloader - Download Instagram Stories - Videos** on [RapidAPI](https://rapidapi.com/).
   - For that production provider, set `SCRAPER_PROVIDER=instagram_downloader` and `RAPIDAPI_HOST=instagram-downloader-download-instagram-stories-videos4.p.rapidapi.com`; its generic method, URL/body template, items-path, and user-ID fields are ignored.
   - For the generic adapter, set `SCRAPER_PROVIDER=rapidapi` and populate the matching endpoint templates.

---

## Operational Tooling & Verification

### Test Push Notifications

Verify that emergency sirens (P5) and quiet pings (P1) deliver properly to your mobile device without running Redis or scrapers:

```bash
python -m app.notify_test
```

### One-Shot End-to-End Poll Tick

Execute a single end-to-end poll tick against your live Redis, RapidAPI, and Gemini integrations:

```bash
python -m app.poll_once
```

Expected output:
- Verifies Redis connectivity via `PING`.
- Fetches active stories for `@zero2sudo`.
- Deduplicates using Redis claim locks.
- If stories exist: extracts frames, runs Gemini vision classification, and triggers an alert if positive.
- Exits cleanly without keeping a long-running process alive.

### Run Full Test Suite

Execute the comprehensive 64-test suite with coverage over media handling, normalizers, mock Redis state transitions, and escalation:

```bash
pytest -v
```

---

## Running in Production

### Native Process

```bash
python -m app.main
```

### Docker Container

The included [`Dockerfile`](Dockerfile) builds a hardened, unprivileged container image (`appuser`, UID 10001) with `ffmpeg` installed:

```bash
# Build Docker image
docker build -t newgrad-swe-alerts .

# Run with environment variables
docker run -d \
  --name swe-alerts \
  --restart unless-stopped \
  --env-file .env \
  --init \
  newgrad-swe-alerts
```

### Azure VM

The [`infra/azure`](infra/azure) Terraform configuration provisions the always-on Ubuntu worker in Mexico Central, with Docker, SSH restricted to your current public IP, and 2 GiB swap. It deliberately keeps `.env` credentials outside Terraform state. Future application updates are deployed with `./infra/azure/deploy.sh`; see the [Azure runbook](infra/azure/README.md) for all commands.

### Process Signals

The daemon handles process termination gracefully:
- `SIGINT` (`Ctrl+C`) or `SIGTERM`: Stops APScheduler, shuts down connection pools, and closes Redis connections without corrupting in-flight story state.

---

## RapidAPI Quota Budgeting

Depending on your RapidAPI subscription tier, configure polling intervals to stay within quota. Instagram Downloader Pro provides 3,200 requests/day and supports the deployed one-minute active cadence plus hourly quiet cadence:

```env
ACTIVE_POLL_INTERVAL_SECONDS=60
QUIET_POLL_INTERVAL_SECONDS=3600
```

This schedule uses approximately 909 ingestion requests/day: about 900 during the 15 active hours and 9 during the 9 quiet hours. The downloader response does not include native story timestamps, captions, IDs, or sticker URLs. The adapter derives IDs from stable CDN filenames, while Gemini classifies only unseen media. When the API does not provide a job link, the alert opens `https://www.instagram.com/stories/zero2sudo/` so the user can tap the original sticker in Instagram.

| Provider tier | Request limit | Recommended `ACTIVE_POLL_INTERVAL_SECONDS` | Recommended `QUIET_POLL_INTERVAL_SECONDS` | Approx. Daily ingestion rate |
| :--- | :--- | :--- | :--- | :--- |
| **Instagram Downloader Pro** | 3,200/day | `60` seconds | `3600` seconds | $\approx 909$ calls/day |
| **Instagram Downloader Basic** | 45/month | Do not run the daemon | Use `app.poll_once` manually | At most $\approx 1$ call/day for testing |
| **Generic provider** | Plan-specific | Calculate from the provider allowance | Calculate from the provider allowance | Plan-specific |

> [!NOTE]
> If using `rapidapi_user_id_url_template`, the account `user_id` lookup runs only once on process startup and is cached in memory for all subsequent story polls. If you know the target ID in advance, set `TARGET_IG_USER_ID` in `.env` to eliminate the lookup request entirely.

---

## License

No `LICENSE` file is currently included in this repository.
