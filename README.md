# lunch-bot

[![CI](https://github.com/Gnaiqing/lunch-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/Gnaiqing/lunch-bot/actions/workflows/ci.yml)

CI runs the `pytest` suite on every push and pull request.

A Slack bot that runs a weekly lunch-decision workflow for a reading group: it
discovers nearby restaurants, posts a diversity-aware poll, records votes,
announces the winner, and reminds the group to order — while the **organizer**
(a human) creates and places the actual Uber Eats group order.

The **production** channel is **#dl-time-series-tabular** (`C03J1AVLGFM`), the
default. **#thoughts-on-lunch** (`C0C4DT9J75Z`) is the **test** channel — point
`SLACK_CHANNEL_ID` at it while testing. The channel is fully configurable, so
other reading groups can run their own instance (see *Reusing for another reading
group* below).

> **v1 scaffold.** This repository is the initial structure with working
> pure-logic (selection + config) and stubs wired for live services. Places,
> Slack, and Anthropic calls need your API keys and (optionally) a seed
> restaurant list before the bot does anything real. Search the code for
> `TODO(user)` for the spots that need your input.

## Weekly flow

All days/times are configurable per group (defaults shown; times local to
`timezone`, default `America/Toronto`):

| When | What happens |
|------|--------------|
| **Mon 10:00 — poll create** | Bot selects 4–6 diverse restaurants (see below) and posts a Block Kit poll, opening voting. |
| **Mon–Wed** | Team votes via poll buttons; votes are recorded in SQLite (one per person, changeable). |
| **Wed 10:00 — poll close + announce** | Bot closes the poll, tallies + records votes (updating preference memory), and announces the winner. It prompts the **organizer** to create and post the Uber Eats **group-order** link (a suggested search is included to save a lookup). |
| **Thu 10:00 — order reminder** | Bot pings the group to place their orders on the group-order link before the deadline. |
| **Thu 11:00 — order deadline** | **Human step (no bot job):** the organizer closes the link and places the order. |

**The bot never creates or places the order.** At any time, a member can
**@-mention the bot** to suggest a restaurant; the bot parses the free text (LLM),
validates it via Google Places, and adds it to the pool.

## Architecture

```
Slack (Socket Mode)  ─┐
                      ├─ slack_app.py   app_mention (suggestions) + poll button votes
APScheduler  ─────────┤
                      ├─ scheduler.py   poll_create · poll_close+announce · order_reminder
                      │
                      ├─ discovery.py   Google Places Nearby Search + geocoding + cuisine tagging
                      ├─ selection.py   diversity + vote-weighted UCB sampling  (PURE, unit-tested)
                      ├─ polls.py       Block Kit poll build + vote handling + tally
                      ├─ ubereats.py    order summary + Uber Eats link (NO automation)
                      ├─ llm.py         Anthropic (Claude) or OpenAI (GPT) cuisine tag + suggestion parse
                      ├─ db.py          SQLite schema + query helpers (stdlib sqlite3)
                      ├─ models.py      Restaurant / Poll / Vote dataclasses
                      └─ config.py      env + config.yaml -> Config dataclass
```

- **Language:** Python 3.11+
- **Slack:** `slack-bolt` in **Socket Mode** (no public URL needed).
- **Scheduling:** `APScheduler` (three configurable cron jobs: poll create /
  poll close+announce / order reminder).
- **Storage:** SQLite via the stdlib `sqlite3` (single file, path configurable, gitignored).
- **Discovery:** Google Places Nearby Search + Geocoding (`googlemaps`).
- **LLM:** pluggable provider (`llm_provider`) — Anthropic `anthropic` SDK
  (`claude-haiku-4-5`, default) or OpenAI `openai` SDK (`gpt-4o-mini`), both
  fast/cheap, for cuisine tagging and parsing free-text restaurant suggestions.
  Only the selected provider's API key is needed.
- **Deploy:** a lab compute cluster via **SkyPilot** (`sky.yaml`).

## Selection algorithm

`selection.select_candidates(...)` is a **pure function** (no I/O) so it is fully
unit-tested and deterministic with a seeded RNG. It balances *diversity* against
*preference memory*:

- **Score (UCB-style)** per active restaurant:
  - `avg_votes = total_votes / times_selected` (when offered before).
  - Never-offered restaurants (`times_selected == 0`) get an **optimistic prior**
    so they get explored — they are *not* penalised like a restaurant that was
    offered but earned zero votes.
  - `exploration_bonus = C / sqrt(times_selected + 1)` (C configurable).
  - `score = avg_or_prior + exploration_bonus`, used as a positive sampling weight.
- **Diversity:** group by cuisine, iteratively pick distinct cuisines (weighted by
  each cuisine's aggregate score), then sample one restaurant within that cuisine
  (weighted by score), without replacement, until N are chosen. If distinct
  cuisines run out, keep sampling remaining restaurants by score.

Net effect: restaurants that keep getting no votes fade over time, popular ones
recur, and new/unknown options still get their turn.

## Budget → price filter

Target budget is **≤ $30/person**. Google Places only exposes a coarse
`price_level` (0–4), so the bot filters to `price_level <= 2` by default (the
`max_price_level` knob). Restaurants with an *unknown* price level are kept
(unknown is not assumed expensive).

## Uber Eats: organizer-run (by design)

When it announces the winner, the bot posts a summary plus a **suggested** Uber
Eats search link (to save a lookup) and asks the **organizer** to create the Uber
Eats **group order** and post the shareable link in the channel. **The bot never
creates or places the order.** There is no official Uber Eats consumer
group-order API, and v1 uses **no browser automation**. The organizer sets up the
group order, the team adds their items, and at the deadline the organizer closes
the link and checks out.

## Reusing for another reading group

Each reading group runs its **own instance** from its own config — nothing is
hardcoded. To stand up a new group:

1. Copy `config.example.yaml` to `config.yaml` and set:
   - `slack_channel_id` (and optional `slack_channel_name`) — the group's channel.
   - `schedule` — the group's `poll_create` / `poll_close` / `order_reminder` days
     and times, plus the informational `order_deadline`, and `timezone`.
   - `restaurants_csv` — the group's own candidate restaurant list.
   - Optionally `poll_size`, `max_price_level`, and office/search knobs.
2. Copy `.env.example` to `.env` and fill in that group's Slack/Google/Anthropic
   secrets.
3. Seed and run a **separate** instance:
   ```bash
   python scripts/seed_from_csv.py           # uses config restaurants_csv
   python -m lunch_bot.main
   ```

## Setup

### 1. Slack app

1. Create an app at <https://api.slack.com/apps> (From scratch).
2. **Socket Mode:** enable it. Under *Basic Information → App-Level Tokens*,
   generate a token with the `connections:write` scope → this is your
   `SLACK_APP_TOKEN` (starts with `xapp-`).
3. **Bot token scopes** (*OAuth & Permissions → Scopes → Bot Token Scopes*):
   - `app_mentions:read` — receive @-mentions (restaurant suggestions)
   - `chat:write` — post polls and announcements
   - `commands` — (for any slash commands you add later)
   - `channels:history` — read channel history as appropriate
4. **Event Subscriptions:** subscribe to the `app_mention` bot event.
5. Install the app to your workspace; copy the **Bot User OAuth Token**
   (`xoxb-…`) → `SLACK_BOT_TOKEN`. (The **Signing Secret** → `SLACK_SIGNING_SECRET`
   is *optional* — only needed for an HTTP request receiver, not Socket Mode.)
6. Invite the bot to your channel and copy the channel **id** (not name) →
   `SLACK_CHANNEL_ID`. Production is **#dl-time-series-tabular** (`C03J1AVLGFM`,
   the default); use the test channel **#thoughts-on-lunch** (`C0C4DT9J75Z`) while
   testing.

App-level token scope summary: `connections:write` (Socket Mode) + bot scopes
`app_mentions:read`, `chat:write`, `commands`, `channels:history`.

### 2. Google Places key

1. In the Google Cloud Console, create/select a project.
2. Enable **Places API** and **Geocoding API**.
3. Create an API key → `GOOGLE_MAPS_API_KEY`. Restrict it to those APIs.

### 3. LLM provider key (Anthropic or OpenAI)

Pick a provider with `LLM_PROVIDER` (`anthropic` or `openai`, default `anthropic`).
Only the selected provider's key is required; the LLM is optional overall.

- **Anthropic:** get a key at <https://console.anthropic.com/> → `ANTHROPIC_API_KEY`.
  Model defaults to `claude-haiku-4-5` (fast/cheap), overridable via `ANTHROPIC_MODEL`.
- **OpenAI:** get a key at <https://platform.openai.com/api-keys> → `OPENAI_API_KEY`.
  Model defaults to `gpt-4o-mini` (small/cheap), overridable via `OPENAI_MODEL`.

To switch providers, set `LLM_PROVIDER` to the other value and provide that
provider's key.

### 4. Configure

```bash
cp .env.example .env               # fill in secrets
cp config.example.yaml config.yaml # adjust non-secret knobs (both .env and config.yaml are gitignored)
```

Environment variables override `config.yaml`. Secrets belong in `.env` only.

## Install & run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Seed the pool from your restaurant CSV (see below), then:
python -m lunch_bot.main
```

`python -m lunch_bot.main` initialises the DB, starts the APScheduler jobs, and
connects to Slack via Socket Mode (it blocks and runs forever).

## Seed the pool from CSV

Provide a CSV with a header row; recognised columns (case-insensitive):
`name` (required), `cuisine`, `address`, `place_id`, `lat`, `lng`, `price_level`.

```bash
python scripts/seed_from_csv.py                     # uses config restaurants_csv
python scripts/seed_from_csv.py restaurants.csv     # or an explicit path
# or target a specific DB file:
python scripts/seed_from_csv.py restaurants.csv --db-path lunch_bot.db
```

The positional CSV path is optional; when omitted it falls back to the configured
`restaurants_csv` (default `data/restaurants_seed.csv`).

Rows are de-duplicated by `place_id` (or by name when absent). Untagged cuisines
can be filled in later by a discovery pass (`discovery.discover_and_store`).

## Run the tests

```bash
pip install pytest
pytest
```

The test suite (`tests/test_selection.py`, `tests/test_config.py`,
`tests/test_schedule.py`, `tests/test_db.py`, `tests/test_main.py`,
`tests/test_scheduler.py`, and `tests/test_llm.py`) covers the pure logic,
config/schedule parsing, the SQLite layer, the startup gate, the scheduler jobs,
and LLM provider selection. It needs **no** network access, Slack, Google, or
Anthropic **credentials** (no live API calls are made). The pure-logic modules
keep heavy imports lazy, so most tests run even without the Slack/Google packages
installed; the exception is `tests/test_llm.py`, which constructs an Anthropic
client and so needs the `anthropic` package (a project dependency in
`requirements.txt`) installed — the OpenAI-backed test is skipped when `openai`
is absent.

## Deploy on SkyPilot

The bot is a long-lived Socket Mode process (no inbound ports). `sky.yaml` runs
`python -m lunch_bot.main`.

```bash
# Provide secrets via a synced .env (default in sky.yaml) or --env flags:
sky launch -c lunch-bot sky.yaml
# tail logs:
sky logs lunch-bot
# tear down:
sky down lunch-bot
```

See `sky.yaml` for how secrets are provided (synced `.env` file mount, or
`--env KEY=VALUE` at launch). Never commit real secrets.

## Data model (SQLite)

- `restaurants` — the pool: `name, cuisine, address, place_id (unique), lat, lng,
  price_level, source ('seed'|'places'|'suggestion'), active, times_selected,
  total_votes, last_selected_at, created_at`.
- `polls` — `slack_channel, slack_ts, created_at, closes_at, status
  ('open'|'closed'), winner_restaurant_id`.
- `poll_options` — options offered in a poll (`poll_id`, `restaurant_id`).
- `votes` — `poll_id, restaurant_id, slack_user_id, created_at`, unique on
  `(poll_id, slack_user_id)` so a user's vote can change but is counted once.
