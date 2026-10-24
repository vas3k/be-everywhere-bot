from dataclasses import dataclass
from pathlib import Path

# --- Timing ---

WATCH_CRON = "0,30 7-22 * * *"  # :00 and :30 each hour, 07:00–22:30 UTC
POST_MIN_AGE_MINUTES = 20  # periodic watch only; skipped on first run / restart / --since
BACKFILL_POST_DELAY_SECONDS = 3  # pause between posts during --since backfill

# Watch mode: only fetch recent own posts (owned reads are $0.001/post on X API)
TWITTER_FETCH_PAGE_SIZE = 10  # tweets per page; paginate only if a full page is all new
TWITTER_FETCH_MAX_PAGES = 10  # safety cap on X timeline pages per poll
WATCH_OVERLAP_HOURS = 6  # re-fetch window for threads / failed publishes
WATCH_INITIAL_LOOKBACK_HOURS = 48  # first run before any sync state exists

# Meta long-lived tokens (Threads, Instagram, Facebook) last 60 days and can only be
# renewed while still valid, so the bot renews them well ahead of expiry.
META_TOKEN_MIN_REMAINING_DAYS = 14  # renew once the token has less than this left
META_TOKEN_UNKNOWN_EXPIRY_DAYS = 30  # renewal cadence when the API reports no expiry
META_TOKEN_RETRY_HOURS = 6  # backoff between renewal attempts (Meta needs tokens ≥24h old)

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
NETWORK_LINKEDIN = "linkedin"

NETWORKS: list[str] = [
    NETWORK_TWITTER,
    NETWORK_TELEGRAM,
    NETWORK_MASTODON,
    NETWORK_THREADS,
    NETWORK_BLUESKY,
    NETWORK_RSS,
    NETWORK_INSTAGRAM,
    NETWORK_LINKEDIN,
]

ACCOUNT_SYNC_ROLES = ("source_and_destination", "source_only", "destination_only")

# Read-only sources — mesh sync publishes from these but never to them.
SOURCE_ONLY_NETWORKS: frozenset[str] = frozenset({NETWORK_RSS, NETWORK_INSTAGRAM})

# Suffix on post text — keep on source network only, do not mesh-sync elsewhere.
SOURCE_ONLY_MARKER = "/x"

# Sources where fetch can return replies — sync filters to own threads after fetch.
REPLY_FILTER_NETWORKS: frozenset[str] = frozenset(
    {NETWORK_TWITTER, NETWORK_THREADS, NETWORK_BLUESKY, NETWORK_MASTODON}
)


@dataclass(frozen=True)
class NetworkLimits:
    max_text: int
    max_caption: int
    max_media_group: int
    allows_mixed_media: bool = True
    max_videos_per_post: int | None = None


@dataclass(frozen=True)
class TwitterAppConfig:
    api_base_url: str = "https://api.x.com/2"


@dataclass(frozen=True)
class TelegramAppConfig:
    api_base: str = "https://api.telegram.org"


@dataclass(frozen=True)
class ThreadsAppConfig:
    api_base_url: str = "https://graph.threads.net/v1.0"
    oauth_base_url: str = "https://graph.threads.net"  # token endpoints are unversioned


@dataclass(frozen=True)
class BlueskyAppConfig:
    default_pds: str = "https://bsky.social"


@dataclass(frozen=True)
class InstagramAppConfig:
    api_base_url: str = "https://graph.instagram.com/v21.0"
    oauth_base_url: str = "https://graph.instagram.com"  # token endpoints are unversioned
    facebook_graph_url: str = "https://graph.facebook.com/v21.0"


@dataclass(frozen=True)
class LinkedInAppConfig:
    api_base_url: str = "https://api.linkedin.com"
    api_version: str = "202504"


TWITTER_APP = TwitterAppConfig()
TELEGRAM_APP = TelegramAppConfig()
THREADS_APP = ThreadsAppConfig()
BLUESKY_APP = BlueskyAppConfig()
INSTAGRAM_APP = InstagramAppConfig()
LINKEDIN_APP = LinkedInAppConfig()

TELEGRAM_LIMITS = NetworkLimits(max_text=4096, max_caption=1024, max_media_group=4)
MASTODON_LIMITS = NetworkLimits(
    max_text=500, max_caption=500, max_media_group=4, allows_mixed_media=False,
    max_videos_per_post=1,
)

TWITTER_LIMITS = NetworkLimits(max_text=280, max_caption=280, max_media_group=4)

THREADS_LIMITS = NetworkLimits(
    max_text=500, max_caption=500, max_media_group=20, allows_mixed_media=False
)
BLUESKY_LIMITS = NetworkLimits(
    max_text=300, max_caption=300, max_media_group=4, allows_mixed_media=False,
    max_videos_per_post=1,
)
LINKEDIN_LIMITS = NetworkLimits(
    max_text=3000,
    max_caption=3000,
    max_media_group=20,
    allows_mixed_media=False,
)

NETWORK_LIMITS: dict[str, NetworkLimits] = {
    NETWORK_TELEGRAM: TELEGRAM_LIMITS,
    NETWORK_MASTODON: MASTODON_LIMITS,
    NETWORK_TWITTER: TWITTER_LIMITS,
    NETWORK_THREADS: THREADS_LIMITS,
    NETWORK_BLUESKY: BLUESKY_LIMITS,
    NETWORK_LINKEDIN: LINKEDIN_LIMITS,
}

TWITTER_CREDENTIAL_KEYS = ("bearer_token", "user_id", "username")
TELEGRAM_CREDENTIAL_KEYS = ("bot_token", "channel_id")
MASTODON_CREDENTIAL_KEYS = ("instance_url", "access_token", "username", "account_id")
THREADS_CREDENTIAL_KEYS = ("access_token", "user_id", "username", "app_secret")
BLUESKY_CREDENTIAL_KEYS = ("handle", "did", "access_jwt", "refresh_jwt", "pds_url")
RSS_CREDENTIAL_KEYS = ("feed_url",)
INSTAGRAM_CREDENTIAL_KEYS = (
    "access_token",
    "user_id",
    "username",
    "token_kind",
    "app_id",
    "app_secret",
)
LINKEDIN_CREDENTIAL_KEYS = ("access_token", "person_urn", "display_name")
