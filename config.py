from dataclasses import dataclass
from pathlib import Path

# --- Timing ---

WATCH_CRON = "0,30 9-23 * * *"  # :00 and :30 each hour, 09:00–23:30 UTC
POST_MIN_AGE_MINUTES = 60
BACKFILL_POST_DELAY_SECONDS = 3  # pause between posts during --since backfill

# Watch mode: only fetch recent own posts (owned reads are $0.001/post on X API)
WATCH_MAX_PAGES = 2  # cap API pages per poll (~200 raw posts max)
WATCH_OVERLAP_HOURS = 6  # re-fetch window for threads / failed publishes
WATCH_INITIAL_LOOKBACK_HOURS = 48  # first run before any sync state exists

# --- Paths ---

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "data" / "be_everywhere.db"

# --- Network identifiers ---

NETWORK_TWITTER = "twitter"
NETWORK_TELEGRAM = "telegram"
NETWORK_MASTODON = "mastodon"
NETWORK_THREADS = "threads"
NETWORK_BLUESKY = "bluesky"
NETWORK_RSS = "rss"
NETWORK_INSTAGRAM = "instagram"

NETWORKS: list[str] = [
    NETWORK_TWITTER,
    NETWORK_TELEGRAM,
    NETWORK_MASTODON,
    NETWORK_THREADS,
    NETWORK_BLUESKY,
    NETWORK_RSS,
    NETWORK_INSTAGRAM,
]

# Read-only sources — mesh sync publishes from these but never to them.
SOURCE_ONLY_NETWORKS: frozenset[str] = frozenset({NETWORK_RSS, NETWORK_INSTAGRAM})


@dataclass(frozen=True)
class NetworkLimits:
    max_text: int
    max_caption: int
    max_media_group: int


@dataclass(frozen=True)
class TwitterAppConfig:
    api_base_url: str = "https://api.x.com/2"


@dataclass(frozen=True)
class TelegramAppConfig:
    api_base: str = "https://api.telegram.org"


@dataclass(frozen=True)
class ThreadsAppConfig:
    api_base_url: str = "https://graph.threads.net/v1.0"


@dataclass(frozen=True)
class BlueskyAppConfig:
    default_pds: str = "https://bsky.social"


@dataclass(frozen=True)
class InstagramAppConfig:
    api_base_url: str = "https://graph.instagram.com/v21.0"
    facebook_graph_url: str = "https://graph.facebook.com/v21.0"


TWITTER_APP = TwitterAppConfig()
TELEGRAM_APP = TelegramAppConfig()
THREADS_APP = ThreadsAppConfig()
BLUESKY_APP = BlueskyAppConfig()
INSTAGRAM_APP = InstagramAppConfig()

TELEGRAM_LIMITS = NetworkLimits(max_text=4096, max_caption=1024, max_media_group=4)
MASTODON_LIMITS = NetworkLimits(max_text=500, max_caption=500, max_media_group=4)

TWITTER_LIMITS = NetworkLimits(max_text=280, max_caption=280, max_media_group=4)

THREADS_LIMITS = NetworkLimits(max_text=500, max_caption=500, max_media_group=20)
BLUESKY_LIMITS = NetworkLimits(max_text=300, max_caption=300, max_media_group=4)

NETWORK_LIMITS: dict[str, NetworkLimits] = {
    NETWORK_TELEGRAM: TELEGRAM_LIMITS,
    NETWORK_MASTODON: MASTODON_LIMITS,
    NETWORK_TWITTER: TWITTER_LIMITS,
    NETWORK_THREADS: THREADS_LIMITS,
    NETWORK_BLUESKY: BLUESKY_LIMITS,
}

TWITTER_CREDENTIAL_KEYS = ("bearer_token", "user_id", "username")
TELEGRAM_CREDENTIAL_KEYS = ("bot_token", "channel_id")
MASTODON_CREDENTIAL_KEYS = ("instance_url", "access_token", "username", "account_id")
THREADS_CREDENTIAL_KEYS = ("access_token", "user_id", "username")
BLUESKY_CREDENTIAL_KEYS = ("handle", "did", "access_jwt", "refresh_jwt", "pds_url")
RSS_CREDENTIAL_KEYS = ("feed_url",)
INSTAGRAM_CREDENTIAL_KEYS = ("access_token", "user_id", "username")
