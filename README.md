# lunch-bot

A Slack bot for the team channel **#thoughts-on-lunch** that runs a weekly
lunch-decision workflow: it discovers nearby restaurants, posts a diversity-aware
poll, records votes, and — before a Thursday reading group — announces the winner
and preps an Uber Eats group order for a human to place.

> **v1 scaffold.** This repository is the initial structure with working
> pure-logic (selection + config) and stubs wired for live services. Places,
> Slack, and Anthropic calls need your API keys and (optionally) a seed
> restaurant list before the bot does anything real. Search the code for
> `TODO(user)` for the spots that need your input.

## Weekly flow

| Day        | What the bot does                                                                 |
|------------|-----------------------------------------------------------------------------------|
| **Monday** | Selects 4–6 diverse restaurants (see below) and posts a Block Kit poll.           |
| **Tue–Thu**| Team votes via poll buttons; votes are recorded in SQLite (one per person).        |
| **Thursday** (before the reading group) | Closes the poll, announces the winner, and posts an Uber Eats order-prep summary + link. **A human places the order.** |

At any time, a member can **@-mention the bot** to suggest a restaurant; the bot
parses the free text (LLM), validates it via Google Places, and adds it to the
pool.

## Architecture

```
Slack (Socket Mode)  ─┐
                      ├─ slack_app.py   app_mention (suggestions) + poll button votes
APScheduler  ─────────┤
                      ├─ scheduler.py   Monday create_poll · Thursday announce+order
                      │
                      ├─ discovery.py   Google Places Nearby Search + geocoding + cuisine tagging
                      ├─ selection.py   diversity + vote-weighted UCB sampling  (PURE, unit-tested)
                      ├─ polls.py       Block Kit poll build + vote handling + tally
                      ├─ ubereats.py    order summary + Uber Eats link (NO automation)
                      ├─ llm.py         Anthropic (Claude Haiku) cuisine tag + suggestion parse
                      ├─ db.py          SQLite schema + query helpers (stdlib sqlite3)
                      ├─ models.py      Restaurant / Poll / Vote dataclasses
                      └─ config.py      env + config.yaml -> Config dataclass
```

- **Language:** Python 3.11+
- **Slack:** `slack-bolt` in **Socket Mode** (no public URL needed).
- **Scheduling:** `APScheduler` (Monday/Thursday cron jobs).
- **Storage:** SQLite via the stdlib `sqlite3` (single file, path configurable, gitignored).
- **Discovery:** Google Places Nearby Search + Geocoding (`googlemaps`).
- **LLM:** Anthropic `anthropic` SDK, `claude-haiku-4-5` (fast/cheap) for cuisine
  tagging and parsing free-text restaurant suggestions.
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

## Uber Eats: semi-automated (by design)

The bot **preps** the order (a summary of the winning restaurant + an Uber Eats
search/restaurant link) and posts it to Slack. **It never creates or places the
order.** There is no official Uber Eats consumer group-order API, and v1 uses
**no browser automation**. A human clicks through and checks out.

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
   (`xoxb-…`) → `SLACK_BOT_TOKEN`, and the **Signing Secret** → `SLACK_SIGNING_SECRET`.
6. Invite the bot to **#thoughts-on-lunch** and copy the channel **id** (not
   name) → `SLACK_CHANNEL_ID`.

App-level token scope summary: `connections:write` (Socket Mode) + bot scopes
`app_mentions:read`, `chat:write`, `commands`, `channels:history`.

### 2. Google Places key

1. In the Google Cloud Console, create/select a project.
2. Enable **Places API** and **Geocoding API**.
3. Create an API key → `GOOGLE_MAPS_API_KEY`. Restrict it to those APIs.

### 3. Anthropic key

1. Get a key at <https://console.anthropic.com/> → `ANTHROPIC_API_KEY`.
2. The default model is `claude-haiku-4-5` (fast/cheap), overridable via `LLM_MODEL`.

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
python scripts/seed_from_csv.py restaurants.csv
# or target a specific DB file:
python scripts/seed_from_csv.py restaurants.csv --db-path lunch_bot.db
```

Rows are de-duplicated by `place_id` (or by name when absent). Untagged cuisines
can be filled in later by a discovery pass (`discovery.discover_and_store`).

## Run the tests

```bash
pip install pytest
pytest
```

The test suite (`tests/test_selection.py`, `tests/test_config.py`) covers the
pure logic only and needs **no** network, Slack, Google, or Anthropic
credentials. The pure-logic modules keep heavy imports lazy, so these tests run
even if the Slack/Google/Anthropic packages aren't installed.

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
