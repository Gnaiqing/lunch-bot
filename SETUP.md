# Lunch Bot — Setup & Go-Live Runbook

This is the team-facing guide to standing up **Lunch Bot** from a fresh checkout
to a running deployment. Follow the sections in order. For architecture and
design details, see [`README.md`](README.md).

## 1. Overview

Lunch Bot is a Slack bot that runs a reading group's weekly lunch-decision
workflow: on **poll-create day** (default Mon 10:00) it discovers nearby
restaurants and posts a diversity-aware Block Kit poll; the group votes; on
**poll-close day** (default Wed 10:00) it closes the poll, records votes, and
announces the winner, prompting the **organizer** to create and post the Uber
Eats group-order link; on **order-reminder day** (default Thu 10:00) it reminds
the group to order before the organizer's deadline (default Thu 11:00, a human
step). **The bot never creates or places the order.** It talks to Slack over
**Socket Mode** (no public URL or open port needed) and runs as a single
long-lived process on the lab compute cluster via **SkyPilot**.

The **production** channel is **#dl-time-series-tabular** (`C03J1AVLGFM`, the
default). **#thoughts-on-lunch** (`C0C4DT9J75Z`) is the **test** channel — set
`SLACK_CHANNEL_ID` to it while testing. Every day/time and the channel are
configurable, so other reading groups can run their own instance (see
[§11](#11-reusing-for-another-reading-group)).

## 2. Prerequisites

- **Python 3.11+**.
- A **Slack workspace** where you can install apps. Installing may require a
  workspace admin's approval — request it early if you are not an admin.
- A **Google Cloud project** with **billing enabled** (required for the Places
  and Geocoding APIs).
- An **Anthropic account** with **credit** on it.

## 3. Slack credentials (Socket Mode — 3 values)

Lunch Bot uses Socket Mode, so you need three Slack secrets
(`SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`) plus the target
channel id (`SLACK_CHANNEL_ID`).

1. Go to <https://api.slack.com/apps> → **Create New App** → **From scratch**.
   Name it **"Lunch Bot"** and pick the **layer6** workspace.
2. **Enable Socket Mode:** Settings → **Socket Mode** → toggle on. When prompted,
   generate an **app-level token** with the `connections:write` scope. This token
   (starts with `xapp-`) is your **`SLACK_APP_TOKEN`**.
3. **OAuth & Permissions → Scopes → Bot Token Scopes** — add:
   - `app_mentions:read` — receive @-mentions (restaurant suggestions)
   - `chat:write` — post polls and announcements
   - `channels:history` — read channel history
   - `commands` — slash commands
4. **Event Subscriptions:** toggle **On**, then under *Subscribe to bot events*
   add the **`app_mention`** event. (With Socket Mode there is **no request URL**
   to configure.)
5. **Interactivity & Shortcuts:** toggle **On** — required for the poll vote
   buttons. (No request URL needed with Socket Mode.)
6. **Install App to Workspace** and approve. Copy the **Bot User OAuth Token**
   (starts with `xoxb-`) — this is your **`SLACK_BOT_TOKEN`**.
7. **Basic Information → Signing Secret** → copy it — this is your
   **`SLACK_SIGNING_SECRET`**.
8. In Slack, invite the bot to the channel: `/invite @Lunch Bot`. Open the
   channel's **About** tab and copy the **channel ID** (starts with `C…`) — set
   this as **`SLACK_CHANNEL_ID`** (the id, not the name). Production is
   **#dl-time-series-tabular** (`C03J1AVLGFM`, the default); use the test channel
   **#thoughts-on-lunch** (`C0C4DT9J75Z`) while testing.

## 4. Google Places key → `GOOGLE_MAPS_API_KEY`

1. Open the [Google Cloud Console](https://console.cloud.google.com/) and select
   (or create) your project.
2. **Enable billing** for the project.
3. Enable **both** APIs (APIs & Services → Library):
   - **"Places API"** — the **classic** one, **NOT** "Places API (New)".
   - **"Geocoding API"**.
4. **Credentials → Create credentials → API key.** This value is
   **`GOOGLE_MAPS_API_KEY`**.
5. *(Recommended)* Restrict the key to just those two APIs (Places API +
   Geocoding API).

## 5. LLM provider key → `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`

The LLM (used for cuisine tagging and parsing free-text suggestions) is
**optional** — the bot still starts and runs without it, degrading those two
features gracefully.

Choose a provider with **`LLM_PROVIDER`** (`anthropic` or `openai`, default
`anthropic`). Only the **selected** provider's key is required; the other is not.

**Option A — Anthropic (default):**

1. Go to <https://console.anthropic.com>.
2. Add **billing credit** to the account.
3. **API Keys → Create Key.** Copy the key (starts with `sk-ant-`) — it is
   **shown only once**. This is your **`ANTHROPIC_API_KEY`**.
   The default model is `claude-haiku-4-5`, overridable via `ANTHROPIC_MODEL`.

**Option B — OpenAI:**

1. Set `LLM_PROVIDER=openai`.
2. Go to <https://platform.openai.com/api-keys> and add billing credit.
3. Create a key (starts with `sk-`) → **`OPENAI_API_KEY`**.
   The default model is `gpt-4o-mini`, overridable via `OPENAI_MODEL`.

To switch providers later, change `LLM_PROVIDER` and supply that provider's key.

## 6. Configure

1. Copy the env template and fill in the five secrets:
   ```bash
   cp .env.example .env
   ```
2. *(Optional)* Copy the non-secret config template and adjust knobs (channel id
   + name, timezone, the weekly `schedule` days/times, `restaurants_csv`, office
   lat/lng, search radius, poll size, max price level):
   ```bash
   cp config.example.yaml config.yaml
   ```
   Environment variables override `config.yaml`; `config.yaml` overrides the
   built-in defaults. Secrets belong in `.env` **only**, never in `config.yaml`.

### Environment variables

Secrets come from `.env` (or the shell). Non-secret knobs can live in `.env`
**or** `config.yaml`; an env value always wins.

| Variable               | Secret? | Description |
|------------------------|:-------:|-------------|
| `SLACK_BOT_TOKEN`      | yes     | Bot User OAuth Token (`xoxb-…`). **Required to start.** |
| `SLACK_APP_TOKEN`      | yes     | App-level token for Socket Mode (`xapp-…`, scope `connections:write`). **Required to start.** |
| `SLACK_SIGNING_SECRET` | yes     | Slack app signing secret (Basic Information). **Required to start.** |
| `GOOGLE_MAPS_API_KEY`  | yes     | Google API key with Places API + Geocoding API enabled. Needed for restaurant discovery. |
| `ANTHROPIC_API_KEY`    | yes     | Anthropic API key (`sk-ant-…`). Required only when `LLM_PROVIDER=anthropic`; enables cuisine tagging + suggestion parsing. |
| `OPENAI_API_KEY`       | yes     | OpenAI API key (`sk-…`). Required only when `LLM_PROVIDER=openai`; enables cuisine tagging + suggestion parsing. |
| `SLACK_CHANNEL_ID`     | no      | Target channel id (`C…`), not the name. Default `C03J1AVLGFM` (#dl-time-series-tabular, production); use `C0C4DT9J75Z` (#thoughts-on-lunch) for testing. |
| `SLACK_CHANNEL_NAME`   | no      | Human-readable channel name for display in messages (default `#dl-time-series-tabular`). |
| `LLM_PROVIDER`         | no      | LLM provider: `anthropic` (default) or `openai`. Selects which key/model is used. |
| `ANTHROPIC_MODEL`      | no      | Anthropic model for tagging/parsing (default `claude-haiku-4-5`). Legacy `LLM_MODEL` still honored. |
| `OPENAI_MODEL`         | no      | OpenAI model for tagging/parsing (default `gpt-4o-mini`). |
| `OFFICE_ADDRESS`       | no      | Office address used for discovery/geocoding (default `661 University Ave, Toronto`). |
| `OFFICE_LAT`           | no      | Office latitude (default `43.6579`). |
| `OFFICE_LNG`           | no      | Office longitude (default `-79.3883`). |
| `SEARCH_RADIUS_M`      | no      | Nearby-search radius in metres (default `5000`). |
| `POLL_SIZE`            | no      | Restaurants offered per poll; keep 4–6 (default `5`). |
| `MAX_PRICE_LEVEL`      | no      | Coarse budget ceiling, Google price_level 0–4 (default `2`). |
| `EXPLORATION_C`        | no      | UCB exploration weight for selection (default `1.0`). |
| `DB_PATH`              | no      | SQLite database file path (default `lunch_bot.db`). |
| `RESTAURANTS_CSV`      | no      | Candidate source for the seed script (default `data/restaurants_seed.csv`). |
| `TIMEZONE`             | no      | Scheduler timezone (default `America/Toronto`). |

The weekly **schedule** (days + times for `poll_create`, `poll_close`,
`order_reminder`, and the informational `order_deadline`) lives in `config.yaml`
under a `schedule:` block — see `config.example.yaml`. Each phase takes a weekday
(`mon`..`sun`, or a full name) and an `"HH:MM"` 24-hour time, interpreted in
`timezone`. `order_deadline` is used only in the reminder text (the organizer
places the order then); it is **not** a scheduled bot job. Defaults:

| Phase            | Default    | Bot job? |
|------------------|------------|:--------:|
| `poll_create`    | Mon 10:00  | yes — post poll, open voting |
| `poll_close`     | Wed 10:00  | yes — close poll, record votes, announce winner |
| `order_reminder` | Thu 10:00  | yes — remind group to order |
| `order_deadline` | Thu 11:00  | no — organizer closes link + places order (human) |

> **Security note:** never commit `.env` — it is gitignored (as are `config.yaml`
> and `*.db`). Do not paste API keys or tokens into plaintext Slack channels,
> tickets, or PRs. Rotate any secret that leaks.

## 7. Seed the pool

Load the 31-restaurant starter list (15 cuisine buckets) into the SQLite pool:

```bash
python scripts/seed_from_csv.py                          # uses config restaurants_csv
python scripts/seed_from_csv.py data/restaurants_seed.csv  # or an explicit path
```

The CSV path is an **optional** positional argument; when omitted it falls back to
the configured `restaurants_csv` (env/yaml/default `data/restaurants_seed.csv`).
The script writes to the DB at `DB_PATH` (from config/env). To target a specific
DB file instead, pass `--db-path`:

```bash
python scripts/seed_from_csv.py data/restaurants_seed.csv --db-path lunch_bot.db
```

Rows are inserted with `source = 'seed'` and de-duplicated by `place_id` (or by
name when no `place_id` is present). Recognised CSV columns (case-insensitive):
`name` (required), `cuisine`, `address`, `place_id`, `lat`, `lng`, `price_level`.

## 8. Local dry run

Install dependencies and start the bot:

```bash
pip install -r requirements.txt
python -m lunch_bot.main
```

`python -m lunch_bot.main` initialises the DB, starts the APScheduler jobs, and
connects to Slack over Socket Mode (it blocks and runs forever). It needs the
**real Slack tokens** to connect — with the required secrets unset it exits
immediately with a message naming the missing environment variables rather than
crashing with a stack trace.

> If you run from a source checkout without installing the package, put the
> package on the path first, e.g. `PYTHONPATH=src python -m lunch_bot.main`.

## 9. Deploy on SkyPilot

The bot is a long-lived Socket Mode process with no inbound ports, so it runs as
a plain task on a minimal CPU node (`cpus: 1+`, `memory: 2+`, no GPU). The
provided [`sky.yaml`](sky.yaml) syncs the repo (`workdir: .`), installs
`requirements.txt` in `setup`, and runs `python -m lunch_bot.main`.

```bash
sky launch -c lunch-bot sky.yaml
sky logs lunch-bot     # tail logs
sky down lunch-bot     # tear down
```

**How secrets/env reach the cluster.** `sky.yaml` supports two mechanisms:

- **Synced `.env` (default):** its `file_mounts` block mounts your local `.env`
  to `/lunch-bot/.env` on the node, and the `run` script sources it before
  starting the bot. Just make sure your `.env` is filled in locally before
  launch. Comment out the `file_mounts` entry if you do not want to sync it.
- **`--env` flags / `envs:` block:** pass secrets at launch with
  `sky launch ... --env SLACK_BOT_TOKEN=… --env SLACK_APP_TOKEN=…` (repeat per
  variable), or set non-secret knobs in the `envs:` block of `sky.yaml`
  (`DB_PATH`, `TIMEZONE` are set there already). Do **not** commit real secrets
  into `sky.yaml`.

## 10. Weekly flow

Defaults shown; all days/times are configurable via `schedule` (see §6) and local
to `timezone`.

- **Poll create — Mon 10:00** — the scheduler auto-selects 4–6 diverse
  restaurants and posts the Block Kit poll to the channel, opening voting.
- **Mon–Wed** — the team votes via the poll buttons (one vote per person,
  changeable); anyone can @-mention the bot to suggest a new restaurant.
- **Poll close + announce — Wed 10:00** — the bot closes the poll, tallies and
  records the votes (updating preference memory), and announces the winner. It
  prompts the **organizer** to create and post the Uber Eats **group-order** link
  (a suggested search is included to save a lookup).
- **Order reminder — Thu 10:00** — the bot pings the group to place their orders
  on the group-order link before the deadline.
- **Order deadline — Thu 11:00 (human step, no bot job)** — the organizer closes
  the link and places the actual order. **The bot never creates or places the
  order.**

## 11. Reusing for another reading group

Each reading group runs its **own instance** from its own config — nothing is
hardcoded. To stand up a new group:

1. Copy `config.example.yaml` to `config.yaml` and set the group's
   `slack_channel_id` (and optional `slack_channel_name`), `timezone`, the
   `schedule` days/times, and `restaurants_csv` (the group's candidate list).
   Adjust `poll_size` / `max_price_level` / office knobs as desired.
2. Copy `.env.example` to `.env` and fill in that group's Slack, Google, and
   Anthropic secrets.
3. Seed and run a **separate** instance:
   ```bash
   python scripts/seed_from_csv.py    # uses config restaurants_csv
   python -m lunch_bot.main
   ```
