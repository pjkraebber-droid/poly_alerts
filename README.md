# Poly alerts

Phone alerts when Polymarket traders you follow make big bets, so you can decide
whether to place a similar trade on Kalshi.

- **Read-only.** It reads Polymarket's public data feed. No Polymarket account,
  wallet, login or keys, and it never places trades.
- **Soccer is always skipped.** Built into the code; there's no setting to turn it off.
- **Two groups.** Current-events traders ping you only about politics, economics and world
  news; sports traders ping you only about non-soccer sports (NBA, NFL, college, MLB, NHL…).
- **Free.** Runs every 5 minutes on GitHub Actions and pings your phone through ntfy.

What lands on your phone:

| When | What |
|---|---|
| Within ~5–15 minutes of a big bet | "flawfence: BUY $6.9K" with the market, their price and the price now |
| Every morning, 12:30 UTC (8:30am New York in summer, 7:30am in winter) | A recap of every move of $500+ in the last 24 hours |
| Sundays, 14:00 UTC | "Weekly scout report is ready" with a link to the ranked list of traders |

---

## Setup (about 10 minutes)

Alerts can go to **Telegram** (recommended), **ntfy**, or both. Set up whichever you want.

### 1a. Telegram (from your phone)

1. In Telegram, open **@BotFather**, send `/newbot`, pick a name and a username ending in `bot`.
   BotFather replies with a **bot token**. Keep it private.
2. Open your new bot, tap **Start** and send it "hi".
3. Optional, for separate feeds: create two private channels (e.g. "Whales · Events" and
   "Whales · Sports"), add the bot to each as an admin that can post, and post one message in each.
4. In the repo (phone browser works): **Settings → Secrets and variables → Actions → New repository
   secret**, name `TELEGRAM_BOT_TOKEN`, paste the token.
5. **Actions → Telegram setup → Run workflow.** The run's summary lists your chat IDs.
6. Save your private chat's ID as the secret `TELEGRAM_CHAT_ID`. For separate feeds, also save
   `TELEGRAM_CHAT_EVENTS` and `TELEGRAM_CHAT_SPORTS` (channel IDs start with `-100`).
7. Run **Telegram setup** again: a test message arrives in each chat.

### 1b. ntfy (alternative)

1. Install **ntfy** from the App Store or Google Play (free, no account needed).
2. Tap **+** → **Subscribe to topic** and enter your private topic name
   (it was given to you along with these files).
3. Treat the topic name like a password: anyone who knows it can read your alerts.

### 2. GitHub: create the repo

1. On github.com, click **New repository**. Name it `poly_alerts`.
   Make it **Public** (Actions are free for public repos; see "Public or private?" below).
2. Upload every file from this folder, including the `.github/workflows` folder.
   - Easiest: use **GitHub Desktop**, or `git push` from a terminal.
   - The web uploader can skip folders that start with a dot, so check that
     `.github/workflows/whale-alerts.yml` shows up in the repo afterwards.

### 3. Add your topic as a secret

Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

- Name: `NTFY_TOPIC`
- Secret: your topic name

### 4. Turn it on

1. Repo → **Actions** tab → if asked, click **I understand my workflows, go ahead and enable them**.
2. Click **Whale alerts** → **Run workflow**.
3. Within a minute your phone should get **"Whale tracker is on — Now watching: …"**.

From then on it checks every 5 minutes by itself. The first run only records a
starting point, so you won't get a flood of old trades.

---

## Choosing who to follow

### Scout report (recommended)

Repo → **Actions** → **Scout traders** → **Run workflow** → pick `month`, `week` or `all`.

It pulls the top traders from several Polymarket leaderboards (overall, politics,
economics, crypto and more), checks each one's history, and shows a ranked table on
the run's summary page. You can also download `scout_report.csv`.

| Verdict | Meaning |
|---|---|
| **GOOD** | Profitable all-time, active, places real directional bets, mostly non-sports |
| **MAYBE** | Worth a look, with a caveat (new wallet, one big win, few markets, some soccer) |
| **SKIP** | Loses money all-time, inactive, a bot, a market maker, or mostly sports/soccer |

The bottom of the report has ready-to-paste `[[whales]]` entries.

Why this matters: a trader can top this month's politics leaderboard and still be
down millions all-time. The monthly board only shows recent luck.

### Adding someone by hand

1. On Polymarket, open the trader's profile from the leaderboard.
2. Copy their wallet address (the `0x…` string, shown on the profile and often in the URL).
3. Check them first, if you run Python locally: `python3 whalewatch.py check 0x…`
4. Add them to `watchlist.toml`:

```toml
[[whales]]
name = "SomeTrader"
wallet = "0x…"
note = "Why you're following them"   # shows in the alert
min_bet_usd = 2000                   # optional: only ping for their bigger moves
```

Commit the change. The next run picks it up.

---

## What an alert looks like

Example (made-up numbers):

> **ExampleTrader: BUY $62K**
> Yes — Will the Democrats win the Texas Senate race in 2026?
> Price avg 41¢ · now 44¢
> $80K in this market since 5:32pm
> Politics, ~$3M lifetime profit

- **avg** is what the whale paid. **now** is the current price. If "now" is well past
  "avg", the move has already happened and copying means paying more than they did.
- Tapping the alert opens the market on Polymarket so you can read the rules, then find
  the matching market on Kalshi. Check that Kalshi's resolution rules are the same.
- **SELL** alerts mean they're taking profit or getting out.

Large orders often arrive as hundreds of tiny fills. The tracker adds them up per trader
and market, and pings you once the total crosses your `min_bet_usd`. If they keep adding,
you get another ping for each additional `min_bet_usd`.

---

## Settings (`watchlist.toml`)

| Setting | Default | What it does |
|---|---|---|
| `min_bet_usd` | `1000` | Dollars one trader must put into one market before you're pinged. Can be set per trader. |
| `include_sells` | `true` | Also ping when they sell. |
| `digest_min_usd` | `500` | Smallest move (per trader, per market) listed in the daily recap. |
| `group` (per trader) | none | `"events"` = only politics, economics and world-news bets reach you; `"sports"` = only non-soccer sports bets. Soccer is blocked for every group. |
| `block_all_sports` | `true` | Skip every sport, not just soccer. Soccer is skipped either way. |
| `timezone` | `America/New_York` | For times shown in alerts. |

---

## How the soccer filter works

A trade is dropped if **any** of these is true:

- The market carries Polymarket's soccer tag, or a tag from a soccer league in
  Polymarket's own sports list.
- The market's slug starts with a soccer league code (for example `epl-`, `lal-`, `kor-`).
- The title or slug mentions soccer terms (Premier League, MLS, Champions League,
  World Cup, Ballon d'Or, "FC", and others).
- The rules use soccer-only language ("stoppage time", "penalty shootout", "90 minutes").

If a market can't be looked up, the trade is held back and retried rather than sent
unchecked, and dropped if it still can't be checked after about 30 minutes.

This is a strong backstop, not a guarantee. Read each alert before you act on it, and
never place a soccer trade even if one somehow gets through.

---

## Public or private?

- **Public repo:** GitHub Actions minutes are free, so the 5-minute schedule costs nothing.
  Your watchlist is visible, but those wallets are public data anyway. Your ntfy topic
  stays private because it's a secret.
- **Private repo:** the free plan includes about 2,000 Actions minutes a month. Every
  5 minutes uses roughly 8,600, so change the schedule in
  `.github/workflows/whale-alerts.yml` to `*/30 * * * *` (every 30 minutes) to stay free.

GitHub pauses scheduled workflows in public repos after 60 days without commits. The
included **Keep schedule alive** workflow makes one empty commit a month to prevent that.

---

## Running it on your laptop instead

Needs Python 3.11+ (`python3 --version`). No packages to install.

```bash
export NTFY_TOPIC="your-topic-name"
python3 whalewatch.py test-alert          # sends a test ping
python3 whalewatch.py run --dry-run       # prints alerts instead of sending
python3 whalewatch.py run --loop 300      # checks every 5 minutes until you stop it
python3 whalewatch.py scout               # ranks leaderboard traders
python3 whalewatch.py digest --dry-run    # prints the last 24 hours' recap
python3 -m unittest discover tests        # runs the offline tests
```

Your laptop has to be awake for `--loop` to keep working.

---

## Troubleshooting

- **No "tracker is on" ping:** check the `NTFY_TOPIC` secret matches the topic in the
  app exactly. Open the latest run under **Actions** and read the log.
- **Runs show up late:** GitHub sometimes delays 5-minute schedules at busy times.
  That's normal.
- **"Tracker can't reach Polymarket" ping:** every check has failed for about an hour.
  It keeps trying and recovers by itself.
- **A run fails with a red X:** GitHub emails you. Open the run's log; the last lines
  say what went wrong.
