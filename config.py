from dataclasses import dataclass
from pathlib import Path

# --- Timing ---

WATCH_INTERVAL_MINUTES = 20
TWEET_MIN_AGE_MINUTES = 30
BACKFILL_POST_DELAY_SECONDS = 3  # pause between posts during --since backfill

# Watch mode: only fetch recent own tweets (owned reads are $0.001/tweet on X API)
WATCH_MAX_PAGES = 2  # cap API pages per poll (~200 raw tweets max)
WATCH_OVERLAP_HOURS = 6  # re-fetch window for threads / failed publishes
WATCH_INITIAL_LOOKBACK_HOURS = 48  # first run before any sync state exists

# --- Paths ---

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "data" / "be_everywhere.db"

# --- Network identifiers ---

NETWORK_TWITTER = "twitter"
NETWORK_TELEGRAM = "telegram"

SOURCE_NETWORKS: list[str] = [NETWORK_TWITTER]
DESTINATION_NETWORKS: list[str] = [NETWORK_TELEGRAM]

SYNC_PAIRS: list[tuple[str, str]] = [
    (source, dest) for source in SOURCE_NETWORKS for dest in DESTINATION_NETWORKS
]

# --- App-level settings (no secrets) ---


@dataclass(frozen=True)
class TwitterAppConfig:
    api_base_url: str = "https://api.x.com/2"


@dataclass(frozen=True)
class TelegramAppConfig:
    api_base: str = "https://api.telegram.org"
    max_text: int = 4096
    max_caption: int = 1024
    max_media_group: int = 4


TWITTER_APP = TwitterAppConfig()
TELEGRAM_APP = TelegramAppConfig()

DESTINATION_LIMITS: dict[str, TelegramAppConfig] = {
    NETWORK_TELEGRAM: TELEGRAM_APP,
}

TWITTER_CREDENTIAL_KEYS = ("bearer_token", "user_id", "username")
TELEGRAM_CREDENTIAL_KEYS = ("bot_token", "channel_id")
