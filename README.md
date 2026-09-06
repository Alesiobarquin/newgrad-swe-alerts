# New Grad SWE Alert Engine

A production-grade, low-latency alerting service that monitors Instagram story job drops (targeting [@zero2sudo](https://www.instagram.com/zero2sudo/)), parses and classifies postings via **Google Gemini Multimodal Vision**, and dispatches instant high-priority mobile push notifications (**ntfy** or **Pushover**) before application windows close.

---

## Architecture Overview

```
                                    +-----------------------+
                                    |    APScheduler        |
                                    | Dynamic 45s / 300s    |
                                    +-----------+-----------+
                                                |
                                                v
+------------------------+          +-----------+-----------+
| Instagram Story Source | -------> |   Ingestion Layer     |
| RapidAPI / Apify       |          | normalize_payload()   |
+------------------------+          +-----------+-----------+
                                                |
                                                v
                                    +-----------+-----------+
                                    |  Upstash Redis State  |
                                    |  Atomic 2-Phase Claim |
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
  - **RapidAPI**: Primary adapter with automatic template formatting, exponential backoff with jitter on HTTP 429/5xx, and dynamic username-to-`user_id` resolution (`RAPIDAPI_USER_ID_URL_TEMPLATE`).
  - **Apify Standby**: Standby adapter maintaining persistent worker connections to eliminate per-tick cold starts.
  - **Universal Normalization**: Decoupled normalizer ([`app/scrapers/normalize.py`](app/scrapers/normalize.py)) parsing Instagram private API schemas, RapidAPI wrappers, tray formats, link stickers, and image candidates into unified [`StoryAsset`](app/models.py) objects.

- **Intelligent Media Processing ([`app/media.py`](app/media.py))**:
  - Static images: Automatically downloads the highest-fidelity resolution candidate.
  - Video stories: Runs `ffprobe` to accurately calculate clip duration, seeking to the midpoint (`duration / 2.0`) and invoking `ffmpeg` to extract a clean, representative keyframe.
  - Deterministic cleanup: Guarantees zero disk leakage by isolating media downloads to disposable temporary directories.

- **Strict Multimodal Classification ([`app/classifier.py`](app/classifier.py))**:
  - Leverages Google Gemini (`gemini-3.6-flash`) with strict JSON schema enforcement ([`CLASSIFICATION_JSON_SCHEMA`](app/models.py)) at `temperature: 0.0`.
  - Rigorous prompt filtering rejects memes, lifestyle posts, quant/math riddles, generic career advice, bootcamps, and senior/manager roles.
  - Identifies company name, role title, direct application links (from link stickers or OCR), and evaluates urgency scores ($1 \dots 5$).
  - Gracefully recovers from API rate limits and classifies configuration errors ([`ClassifierConfigError`](app/classifier.py)) so claims are freed rather than poisoned.

- **Two-Phase Atomic Deduplication & State ([`app/state.py`](app/state.py))**:
  - Powered by Upstash Redis with atomic Lua scripts (and fallback native pipelines).
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
  - Polling interval dynamically throttles: **45 seconds** during active day hours, **300 seconds** during quiet overnight hours.
  - Cron triggers at 23:00 and 08:00 ET automatically adjust poller cadence and trigger morning batch delivery.

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
| `SCRAPER_PROVIDER` | No | `rapidapi` | Ingestion engine: `rapidapi` or `apify_standby`. |
| `TARGET_IG_USERNAME` | No | `zero2sudo` | Target Instagram handle to poll (without `@`). |
| `TARGET_IG_USER_ID` | No | `""` | Optional static Instagram numerical ID (bypasses lookup). |
| `RAPIDAPI_KEY` | Yes* | `""` | RapidAPI application key (*required if `rapidapi`). |
| `RAPIDAPI_HOST` | Yes* | `""` | RapidAPI host header (e.g. `instagram-scraper-api2.p.rapidapi.com`). |
| `RAPIDAPI_METHOD` | No | `GET` | HTTP verb for stories endpoint (`GET` or `POST`). |
| `RAPIDAPI_URL_TEMPLATE`| No | `https://{host}/stories?user_id={user_id}` | URL template supporting `{host}`, `{username}`, `{user_id}`. |
| `RAPIDAPI_BODY_TEMPLATE`| No | `""` | Optional JSON body template for POST endpoints. |
| `RAPIDAPI_ITEMS_PATH` | No | `""` | Dot-notation JSON path to story array (e.g. `data.stories`). |
| `RAPIDAPI_USER_ID_URL_TEMPLATE` | No | `https://{host}/user_id_by_username?username={username}` | Endpoint template used to resolve username to numerical ID. |
| `APIFY_API_TOKEN` | Yes* | `""` | Apify API token (*required if `apify_standby`). |
| `APIFY_STANDBY_URL` | Yes* | `""` | Full URL for persistent Apify Standby actor. |
| `UPSTASH_REDIS_URL` | **Yes** | — | Upstash Redis connection string (`rediss://default:...@...upstash.io:6379`). Auto-cleans `redis-cli` paste strings. |
| `GEMINI_API_KEY` | **Yes** | — | Google Gemini API key. |
| `GEMINI_MODEL` | No | `gemini-3.6-flash` | Gemini multimodal vision model name. |
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
| `ACTIVE_POLL_INTERVAL_SECONDS` | No | `45` | Polling frequency during active daytime hours. |
| `QUIET_POLL_INTERVAL_SECONDS` | No | `300` | Polling frequency during quiet overnight hours. |
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
   - Subscribe to an Instagram story scraper on [RapidAPI](https://rapidapi.com/).
   - Populate `RAPIDAPI_KEY`, `RAPIDAPI_HOST`, and matching endpoint templates in `.env`.

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

Execute the comprehensive 59-test suite with coverage over media handling, normalizers, mock Redis state transitions, and escalation:

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

### Process Signals

The daemon handles process termination gracefully:
- `SIGINT` (`Ctrl+C`) or `SIGTERM`: Stops APScheduler, shuts down connection pools, and closes Redis connections without corrupting in-flight story state.

---

## RapidAPI Quota Budgeting

Depending on your RapidAPI subscription tier, configure polling intervals to optimize quota usage:

| Tier | Monthly Request Limit | Recommended `ACTIVE_POLL_INTERVAL_SECONDS` | Recommended `QUIET_POLL_INTERVAL_SECONDS` | Approx. Daily Ingestion Rate |
| :--- | :--- | :--- | :--- | :--- |
| **Pro / Dedicated** | $\ge 12,000$ / mo ($\sim 400$/day) | `45` seconds | `300` seconds | $\approx 1,200$ calls/day |
| **Basic / Free** | $100$ / month | `7200` seconds (2 hrs) | `14400` seconds (4 hrs) | $\approx 10$ calls/day |

> [!NOTE]
> If using `rapidapi_user_id_url_template`, the account `user_id` lookup runs only once on process startup and is cached in memory for all subsequent story polls. If you know the target ID in advance, set `TARGET_IG_USER_ID` in `.env` to eliminate the lookup request entirely.

---

## License

MIT License. See [LICENSE](LICENSE) for details.
