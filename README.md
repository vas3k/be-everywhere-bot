# be-everywhere-bot

A small Python app that **mesh-syncs** your posts across **X (Twitter)**, **Threads**, **Bluesky**, **Telegram**, **Mastodon**, **Instagram**, **LinkedIn**, and **RSS feeds**. Posts from source-enabled connections are reposted only to destination-enabled connections in the same project. The bot tracks synced content and posts it created itself to prevent repeated delivery and circular reposts.

## Features

- **Terminal control panel** — project dashboard, connection management, sync/backfill actions, and live logs over SSH
- **Project-scoped mesh sync** — connections repost only within their project
- **Multiple accounts per network** — connect several Twitter/Telegram/Mastodon accounts with `--label`
- **Thread support** — consecutive posts in the same conversation are merged when the destination allows
- **Duplicate protection** — tracks completed deliveries and bot-created posts to prevent repeated or circular reposts
- **Smart filtering** (X) — skips retweets, quote tweets, `@`-replies, and replies to other people
- **Source-only marker** — append `/x` to a post to keep it on that network only (not mesh-synced)
- **Link unwrapping** — `t.co` and other shorteners are resolved before posting
- **Token renewal** — Meta (Threads, Instagram) 60-day tokens are refreshed automatically before they expire

## Thread processing rules

- Sync original content from source-enabled connections to destination-enabled connections in the same project; skip bot-created posts and content already delivered to that destination.
- Skip posts ending in `/x`; on reply-filtered networks, descendants with a missing parent are also excluded.
- Group by source conversation ID, oldest first. Merge unsynced text with blank lines and collect attachments separately; original post boundaries and text-to-attachment associations are not preserved.
- Split to destination limits: paragraphs → lines → sentences → spaces → hard cuts. Fill media captions first, then publish remaining text; chain resulting posts as replies.
- Wait until the root reaches the project’s minimum age (default: 20 minutes) during normal polling; replies need no separate delay. Startup's first cycle and `--since` bypass the age gate.
- Append later replies to the last mapped destination post; do not rewrite previously delivered content. Resume partial deliveries from saved progress.
- On X, Threads, Bluesky, and Mastodon, retain own threads with complete locally available parent chains; missing ancestors can exclude legitimate old-thread continuations.
- Where mixed media is disabled, publish photos before videos. Limits below are bot configuration, not a complete platform capability list.

## Requirements

- **Python 3.13+**
- **[uv](https://docs.astral.sh/uv/)** (recommended)
- API credentials for each network you want to connect

## Quick start

```bash
git clone <repo-url> be-everywhere-bot
cd be-everywhere-bot
uv sync

# Open the control panel, create a project, and add your accounts
uv run python main.py

# Run continuous mesh sync without the UI
uv run python main.py --watch
```

## Setup

### Projects

- Each connection belongs to one project; routing and pending deliveries stay within that project.
- **Default** is available immediately; create additional projects for separate networks of accounts.
- Posts never cross project boundaries. To use a remote account in several projects, authorize it separately in each and set **every record to `destination_only`**. Shared sources are rejected.
- Account labels are unique per project and network; multiple accounts on the same network are supported. `--list-accounts` shows each connection’s sync role.
- `--auth` without `--project` uses **Default**. Watch/backfill without `--project` runs all enabled projects sequentially. Unknown names fail instead of falling back; disabled projects are skipped even when explicitly selected.

```bash
uv run python main.py --create-project Work
uv run python main.py --project Work --auth=twitter --label=company
uv run python main.py --project Work --auth=twitter --label=founder
uv run python main.py --project Work --auth=telegram --label=channel
uv run python main.py --list-projects
uv run python main.py --project Work --list-accounts
uv run python main.py --project Work       # open this project in the TUI
uv run python main.py --project Work --watch
uv run python main.py --project Work --since=2026-01-01

# Change persisted settings; omit a flag to retain its current value
uv run python main.py --project Work --configure-project --min-age-minutes 30 --overlap-hours 6 --initial-lookback-hours 48 --backfill-delay-seconds 3
uv run python main.py --project Work --configure-project --no-enabled
uv run python main.py --project Work --configure-project --enabled
```

Each project has its own enabled state, minimum post age, fetch overlap, initial lookback, and backfill delay. The watch schedule is global. The dashboard shows recent sync times, delivery counts, and errors; posts can still be waiting for the age gate after a successful cycle.

### Connection roles

- `source_and_destination`: read original posts and receive reposts within the project; default for writable networks.
- `source_only`: read original posts; never receive reposts. Mandatory for Instagram and RSS; those connections cannot be shared across projects.
- `destination_only`: receive reposts; never fetch or forward original/cached posts. Mandatory for every record of a remote account shared across projects.
- Set `--sync-role` during authentication; omission preserves an existing connection's role. Change roles without reauthorizing using `--configure-account NETWORK --label NAME --sync-role ROLE`.
- Role changes stop incompatible pending deliveries; saved progress remains available if the route is enabled again.
- Mirror IDs prevent bot-created posts from being fetched back into the mesh, including when several records refer to the same remote account.

```bash
# Existing personal Twitter must become destination-only before sharing it
uv run python main.py --project Default --configure-account twitter --label=personal --sync-role=destination_only
uv run python main.py --project Work --auth=twitter --label=personal --sync-role=destination_only

# A source-only channel in Work
uv run python main.py --project Work --auth=telegram --label=announcements --sync-role=source_only
```

### Connect accounts

```bash
uv run python main.py --auth=twitter --label=personal
uv run python main.py --auth=twitter --label=work
uv run python main.py --auth=telegram --label=main
uv run python main.py --auth=mastodon --label=fedi
uv run python main.py --auth=threads --label=main
uv run python main.py --auth=bluesky --label=main
uv run python main.py --auth=rss --label=blog
uv run python main.py --auth=instagram --label=main
uv run python main.py --auth=linkedin --label=main
```

Re-running `--auth` with the same project + network + label updates credentials and remote identity while keeping the local account ID, sync history, and role unless `--sync-role` is supplied. Use **Reauthorize** for the equivalent TUI action; **Add account** rejects an existing label.

Use `--label` to connect multiple accounts on the same network (e.g. personal and work Twitter).

---

### X (Twitter)

**Sync rules:**

- Read + publish; 280 characters, up to 4 attachments. Mixed media allowed by the bot; unsupported combinations may fail remotely.
- Skip retweets, quote tweets, `RT @…`, and any text beginning with `@` after whitespace.

**Portal:** [developer.x.com](https://developer.x.com/en/portal/dashboard)

**What you need:** Bearer Token + your `@handle`

**Steps:**

1. Sign in at the [X Developer Portal](https://developer.x.com/en/portal/dashboard).
2. Create a **Project** and an **App** (or open an existing app).
3. Open the app → **Keys and tokens** tab.
4. Under **Bearer Token**, click **Generate** or **Regenerate** and copy the token.
5. Note your X username without `@` (e.g. `vas3k`).
6. Run `uv run python main.py --auth=twitter --label=main` and paste the token and username when prompted.

**Notes:** X API v2 is pay-per-use (~$0.001 per owned timeline read). Watch mode only polls recent posts to limit cost. Top up credits at the [developer portal](https://developer.x.com/en/portal/dashboard) if you get HTTP 402.

---

### Telegram

**Sync rules:**

- Read + publish; 4,096 characters for text, 1,024 for captions; up to 4 mixed photo/video attachments per outbound.
- Join album members by album ID; ordinary messages and replies remain separate conversations.
- Read incoming channel updates only; no full-history backfill. Extract photos, videos, and animations; skip messages with neither text nor supported media.

**Portals:** [@BotFather](https://t.me/BotFather) · [Telegram Bot API docs](https://core.telegram.org/bots)

**What you need:** Bot token + channel ID

**Steps:**

1. Open [@BotFather](https://t.me/BotFather) in Telegram and send `/newbot`.
2. Follow the prompts to name your bot and copy the **bot token** (looks like `123456:ABC-DEF…`).
3. Create a **channel** (or use an existing one) where posts should be synced.
4. Add your bot to the channel as an **administrator** (needs permission to post).
5. Find the channel ID:
   - Public channel: `@yourchannel`
   - Private channel: forward a message to [@userinfobot](https://t.me/userinfobot) or use the `-100…` numeric ID from your client
6. Run `uv run python main.py --auth=telegram --label=main` and paste the bot token and channel ID.

**Notes:** The Bot API cannot backfill full channel history — only posts received via `getUpdates` after the bot is set up are synced. For older content, backfill from X or Mastodon instead.

---

### Mastodon

**Sync rules:**

- Read + publish; fixed 500-character limit; up to 4 photos or 1 video/animation per post. Separate photos and videos.
- Exclude boosts; do not discover instance-specific limits.

**Portal:** your instance (e.g. [mastodon.social](https://mastodon.social))

**What you need:** Instance URL + access token with `read` and `write` scopes

**Steps:**

1. Log in to your Mastodon instance.
2. Go to **Preferences** → **Development** → **New application** (or open an existing app).
3. Enable scopes **read** and **write**, then create the application.
4. Copy the **Access token** shown for your account.
5. Note your instance base URL (e.g. `https://mastodon.social` — no trailing slash).
6. Run `uv run python main.py --auth=mastodon --label=main` and paste the URL and token.

---

### Threads

**Sync rules:**

- Read + publish; 500 characters, up to 20 attachments; separate photos and videos.
- Skip reposts, quotes, and text beginning with `@` after whitespace.
- Publish media via externally fetchable URLs; omit `tgfile:` attachments. Current validation accepts HTTP/HTTPS without checking public reachability.

**Portal:** [developers.facebook.com](https://developers.facebook.com/apps/)

**What you need:** User access token with Threads scopes (optionally the Threads App Secret)

**Steps:**

1. Create or open an app at [Meta for Developers](https://developers.facebook.com/apps/).
2. Add the **Threads API** product to your app.
3. Under **Threads API** → tools / token generator, create a **User access token**.
4. Grant scopes **`threads_basic`** and **`threads_content_publish`**.
5. Copy the access token. Optionally copy the **Threads App secret** from **App settings** → **Basic**.
6. Run `uv run python main.py --auth=threads --label=main` and paste the token (App Secret and username are optional).

**Notes:** Publishing is limited to **250 posts per 24 hours** per profile. Media must be on a **public HTTPS URL** — posts from X/Mastodon usually work; Telegram-sourced media may publish as text-only. See [Meta access tokens](#meta-access-tokens) for how expiry is handled.

---

### Bluesky

**Sync rules:**

- Read + publish; 300 characters; up to 4 photos or 1 video per post. Separate photos and videos; oversized images fail without resizing.
- Skip reposts, recognized quote embeds, and text beginning with `@` after whitespace; quote detection is not exhaustive.

**Portal:** Bluesky app → **Settings** → **Privacy and security** → **App passwords**

**What you need:** Handle + app password (not your login password)

**Steps:**

1. Log in to [Bluesky](https://bsky.app/) (web or app).
2. Open **Settings** → **Privacy and security** → **App passwords**.
3. Create a new app password and copy it (shown once).
4. Note your full handle (e.g. `you.bsky.social`).
5. Run `uv run python main.py --auth=bluesky --label=main` and enter handle and app password.
6. Press Enter for the default PDS (`https://bsky.social`) unless you use a custom server.

---

### RSS (source only)

**Sync rules:**

- Source only; publish entry title, summary, and link as text. Do not import article media.

**What you need:** Feed URL (RSS 2.0 or Atom)

**Steps:**

1. Find the feed URL for your blog or site (often `/feed`, `/feed.xml`, or `/atom.xml`).
2. Verify it loads in a browser or feed reader.
3. Run `uv run python main.py --auth=rss --label=blog` and paste the URL (e.g. `https://example.com/feed.xml`).

**Notes:** RSS is **read-only** — items sync out to your social accounts but nothing is posted back. Each item is published as **title**, **description/summary**, and a **link**. Use `--since=YYYY-MM-DD` on first run to import older items.

---

### Instagram (source only)

**Sync rules:**

- Source only: feed posts, carousels, videos/reels, and available stories; no publishing to Instagram.
- Group consecutive stories with gaps ≤20 minutes; retain only the first nonempty caption per merged story batch.
- Story availability and expiring media URLs limit delayed delivery/backfill.

**Portal:** [developers.facebook.com](https://developers.facebook.com/apps/)

**What you need:** Instagram Business or Creator account + user access token

**Steps:**

1. Convert your Instagram account to **Business** or **Creator** (Instagram app → Account type).
2. Create or open an app at [Meta for Developers](https://developers.facebook.com/apps/).
3. Add the **Instagram** product → **API setup with Instagram login**.
4. Connect your Instagram account and generate a **User access token** with scope **`instagram_business_basic`**.
5. Copy the access token. Optionally copy the **App secret** from **App settings** → **Basic**.
6. Run `uv run python main.py --auth=instagram --label=main` and paste the token (App Secret and username are optional).

**Notes:** Instagram is **read-only** in mesh sync — feed posts and active **stories** (24 h window) flow out to other networks, never in. Media URLs expire; the bot downloads media at publish time. Accounts connected through **Facebook login** are also asked for the **Facebook App ID**, which Meta requires to renew that flavour of token. See [Meta access tokens](#meta-access-tokens).

---

### Meta access tokens

Threads and Instagram user tokens are valid for **60 days** and can be renewed for another 60 — but only while they are still valid. A token that lapses cannot be recovered and the account has to be connected again.

The bot handles this on its own:

- At `--auth` time a short-lived (1 hour) token is exchanged for a 60-day one when you supply the App Secret. Without the secret, an already-long-lived token is accepted as-is.
- Before every fetch or publish, the stored token is renewed if it has less than `META_TOKEN_MIN_REMAINING_DAYS` (14) left. The new token and its expiry are written back to `account_credentials`.
- Failed renewals are retried every `META_TOKEN_RETRY_HOURS` (6) rather than on every sync cycle — Meta rejects tokens younger than 24 hours, so a freshly connected account settles within a day.
- Accounts connected before this feature existed have no recorded expiry; the bot probes them until Meta reports one.

If the bot is stopped for more than 60 days the token expires anyway. You will see `Threads access token expired on … Re-authenticate: …` in the logs — re-run `--auth` for that network.

---

### LinkedIn

**Sync rules:**

- Publish; reading requires suitable API permissions. Initial posts: 3,000 characters, up to 20 images; omit videos.
- Publish continuations as comments; omit their images.

**Portal:** [linkedin.com/developers](https://www.linkedin.com/developers/apps)

**What you need:** Member access token with posting (and ideally reading) scopes

**Steps:**

1. Create an app at [LinkedIn Developers](https://www.linkedin.com/developers/apps) (linked to a Company Page).
2. Under **Products**, request **Share on LinkedIn** and **Sign In with LinkedIn using OpenID Connect**.
3. Open the **Auth** tab and note your OAuth settings (redirect URL if you use the OAuth flow).
4. Generate a **member access token** with scopes:
   - `openid`, `profile`, `email` — identity
   - `w_member_social` — publish posts
   - `r_member_social` — read your posts (may require [LinkedIn approval](https://learn.microsoft.com/en-us/linkedin/marketing/lms-faq); without it the account can publish but not act as a source)
5. Run `uv run python main.py --auth=linkedin --label=main` and paste the token.

**Notes:** Long posts are split at 3,000 characters. Thread continuations are posted as comments. Images upload via the LinkedIn Images API (up to 20 per post). Video is not supported yet.

---

## Running

```bash
uv run python main.py                      # interactive TUI (default)
uv run python main.py --watch              # unattended scheduled sync
uv run python main.py --since=2026-01-01   # backfill from date
uv run python main.py --list-accounts      # show configured accounts
uv run python main.py -v                   # debug logging
```

### Terminal UI

- Dashboard: select or create a project, force sync enabled projects, or backfill from a UTC date.
- Project: edit settings, pause/resume sync, manage accounts, or run project/account sync actions.
- Accounts: add, edit labels and roles, reauthorize, or remove with confirmation. Removing a connection erases its credentials and pending work; published posts and loop-protection history remain.
- The account table shows network, account name, role, last fetch time, label, and pending deliveries. Selecting a row shows further sync and token details.
- Commands run sequentially. A near-full-screen log window opens for each task and stays open until you close it after completion. Force sync and backfill bypass the age gate while respecting paused projects and account roles.
- Use Tab/Shift+Tab, arrows, Enter, or the mouse. Escape returns or cancels; `r` refreshes; Ctrl+Q exits when idle. Content scrolls on smaller terminals.

### Watch mode

`--watch` syncs immediately, then follows `WATCH_CRON` in `config.py` (UTC). Normal cycles respect each project's minimum post age; the initial cycle bypasses it. Failed deliveries resume from saved progress.

Run only one sync operation against a database at a time. Stop the watcher before running manual sync or changing connections. A crash between remote publication and saving progress can still duplicate a post.

### Backfill (`--since`)

One-shot sync since the given date. Skips min-age filter, uses the project’s backfill delay (default **3 seconds**) between publishes. Works for X and Mastodon history; Telegram only picks up posts received via `getUpdates` since the bot was configured.

### Docker

```bash
docker compose up -d --build
docker compose logs -f bot
```

For account management or manual sync over SSH, stop the watcher and open the TUI in a temporary container using the same saved data:

```bash
docker compose stop bot
docker compose run --rm -it bot uv run python main.py
# After exiting the TUI:
docker compose start bot
```

To view the dashboard while the watcher runs, use `docker compose exec -it bot uv run python main.py`. Stop the watcher before making changes or starting manual sync operations.

## Configuration

Project timing constants initialize new projects; change existing projects with `--configure-project`. Other constants remain global. Instagram story grouping still uses `POST_MIN_AGE_MINUTES` globally.

| Constant | Default | Description |
|----------|---------|-------------|
| `POST_MIN_AGE_MINUTES` | `20` | Min age for main posts before publishing; skipped on first watch cycle after start/restart and on `--since` |
| `WATCH_CRON` | `0,30 7-22 * * *` | Watch-mode cron schedule (UTC) |
| `BACKFILL_POST_DELAY_SECONDS` | `3` | Delay between posts in `--since` mode |
| `TWITTER_FETCH_MAX_PAGES` | `10` | Max X API pages per poll (safety cap) |
| `TWITTER_FETCH_PAGE_SIZE` | `10` | X tweets per page; next page only if all are new |
| `WATCH_OVERLAP_HOURS` | `6` | Re-fetch overlap for threads / retries |
| `META_TOKEN_MIN_REMAINING_DAYS` | `14` | Renew Meta tokens once this little of the 60 days is left |
| `META_TOKEN_RETRY_HOURS` | `6` | Backoff between failed Meta token renewals |
| `META_TOKEN_UNKNOWN_EXPIRY_DAYS` | `30` | Renewal cadence when Meta reports no expiry |

## Data and upgrades

Credentials and sync history are stored in `data/be_everywhere.db`; Docker mounts `./data` into the container. Keep this directory private and include it in backups.

1. Stop the watcher and close any other bot processes.
2. Back up the entire `data/` directory while the bot is stopped.
3. Update the code and dependencies (`uv sync --locked`), or rebuild your Docker image.
4. Apply database updates with `uv run python main.py --migrate` or `docker compose run --rm bot uv run python main.py --migrate`.
5. Start `--watch` or `docker compose up -d` and review the logs.

Database updates also run automatically on startup. To roll back, restore the matching code version and pre-upgrade data backup together.

## Development checks

```bash
uv sync --locked --dev
uv run --no-sync ruff check .
uv run --no-sync pytest -q
```

Tests use temporary databases and mocked network APIs. They cover routing, thread processing, authentication, retries, account management, and database upgrades. Ruff checks syntax and likely runtime errors. GitHub Actions runs these checks on pushes and pull requests; it does not build Docker images or deploy the app.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| No accounts configured | Run `--auth=…` for each network |
| Post not synced yet | May be younger than 20 min in watch mode |
| Telegram history missing | Bot API can't backfill channels; use `--since` on X/Mastodon |
| Instagram stories missing | Stories expire after 24 h; run watch mode regularly |
| Repeated reposts | Ensure only one sync runs at a time and the persisted data directory is intact |
| HTTP 402 on X | Top up API credits at developer.x.com |
| `Session has expired` on Threads/Instagram | Token lapsed past its 60-day window; re-run `--auth` for that network |

```bash
uv run python main.py -v   # debug logging
```

## License

Private / personal use. Adjust as needed.
