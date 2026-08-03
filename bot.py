"""
bot.py — Pipeline orchestrator (thin entry point).

Responsibilities:
  1. Set up logging
  2. Authenticate the X burner account
  3. Fetch new tweets (using the watermark from DuplicateFilter's state)
  4. Build the pipeline based on feature flags
  5. Run each tweet through the pipeline
  6. Commit logs are handled by forward.yml

What does NOT belong here:
  • Feature logic (in processors.py)
  • Feature flags (in features.py)
  • Pipeline infrastructure (in pipeline.py)
  • State management (in ProcessorState per processor)
"""
import asyncio
import json
import os
import time
import traceback

import aiohttp
from twscrape import API

from features import is_enabled, feature_summary
from logger import flush_and_stop, get_logger, log_exception, set_log_context, setup_logging
from pipeline import Pipeline, TweetContext
from processors import (
    AIClassifier,
    DeletionChecker,
    DuplicateFilter,
    MessageFormatter,
    TelegramSender,
)

# ── Environment ───────────────────────────────────────────────
TWITTER_USER  = "IranIntlBrk"
TELEGRAM_CHAT = os.environ.get("TELEGRAM_CHANNEL", "@CloneIntlbrk")
BOT_TOKEN     = os.environ["TELEGRAM_BOT_TOKEN"]
COOKIES       = os.environ["X_COOKIES_CLONE"]
AI_API_KEY    = os.environ.get("AI_API_KEY", "")
INSTANCE      = os.environ.get("BOT_INSTANCE", "main")
CONTEXT_FILE  = "weekly_context.json"

BURNER_USERNAME = "NRMNDIDI"

# ── Helpers ───────────────────────────────────────────────────
def load_weekly_context() -> dict:
    """Load AI weekly topics. Graceful fallback if file missing."""
    log = get_logger("load_weekly_context")
    if os.path.exists(CONTEXT_FILE):
        try:
            with open(CONTEXT_FILE, encoding="utf-8") as f:
                data = json.load(f)
            log.debug(f"Loaded {len(data.get('topics', []))} topics for {data.get('week')!r}")
            return data
        except Exception as exc:
            log_exception(log, exc, f"Could not parse {CONTEXT_FILE}")
    log.warning(f"{CONTEXT_FILE} not found — AI will classify without weekly context")
    return {"topics": [], "week": "unknown"}


async def fetch_new_tweets(since_id: int) -> list:
    """
    Authenticate the burner account and fetch tweets from @IranIntlBrk
    newer than since_id.
    Returns a list of twscrape Tweet objects.
    """
    log = get_logger("fetch_new_tweets")
    set_log_context(section="fetch_new_tweets")

    api        = API()
    try:
        await api.pool.add_account_cookies(BURNER_USERNAME, COOKIES)
        log.info(f"Account {BURNER_USERNAME!r} loaded")
    except Exception as exc:
        log_exception(log, exc, f"Cannot add account {BURNER_USERNAME!r} — aborting")
        raise

    query = f"from:{TWITTER_USER} -filter:replies -filter:retweets"
    if since_id:
        query += f" since_id:{since_id}"
    log.debug(f"Query: {query}")

    tweets = []
    try:
        async with asyncio.timeout(60):
            async for tweet in api.search(query, limit=20):
                tweets.append(tweet)
                log.debug(f"Found: {tweet.id} — {tweet.rawContent[:50]}…")
    except asyncio.TimeoutError:
        log.warning("Search timed out after 60s — using tweets found so far")
    except Exception as exc:
        log_exception(log, exc, "Tweet fetch error")

    return tweets


def build_pipeline(dup_filter: DuplicateFilter) -> Pipeline:
    """
    Construct the pipeline for this run.

    Rules:
      • Core processors are always registered (DuplicateFilter, Formatter, Sender)
      • Optional processors are gated by is_enabled() checks
      • is_enabled() reads features.py + instance-level overrides
      • Adding a new feature here is ONE if-block — nothing else changes
    """
    log = get_logger("build_pipeline")
    pipeline = Pipeline()

    # ── Core: always runs ─────────────────────────────────────
    pipeline.register(dup_filter)

    # ── Optional: AI classification ───────────────────────────
    if is_enabled("ai_classifier", INSTANCE):
        weekly_ctx = load_weekly_context()
        pipeline.register(AIClassifier(api_key=AI_API_KEY, weekly_context=weekly_ctx))
        log.debug("ai_classifier: registered")
    else:
        log.debug("ai_classifier: disabled")

    # ── [NEW FEATURE TEMPLATE] ────────────────────────────────
    # To add a future feature:
    #   1. Create class in processors.py
    #   2. Add entry in features.py
    #   3. Add ONE block here:
    #
    # if is_enabled("deletion_checker", INSTANCE):
    #     pipeline.register(DeletionChecker())

    if is_enabled("deletion_checker", INSTANCE):
        pipeline.register(DeletionChecker())
        log.debug("deletion_checker: registered")

    # ── Core: always runs ─────────────────────────────────────
    pipeline.register(MessageFormatter(TWITTER_USER, TELEGRAM_CHAT))
    pipeline.register(
        TelegramSender(BOT_TOKEN, TELEGRAM_CHAT, dup_filter=dup_filter)
    )

    return pipeline


# ── Main ──────────────────────────────────────────────────────
async def main() -> None:
    run_start = time.monotonic()
    log       = setup_logging()
    set_log_context(section="main")

    log.info("━" * 56)
    log.info(f"🚀 Run | instance={INSTANCE!r} | channel={TELEGRAM_CHAT!r}")

    # Log feature flag summary for this run
    flags = feature_summary(INSTANCE)
    on    = [k for k, v in flags.items() if v]
    off   = [k for k, v in flags.items() if not v]
    log.info(f"🏁 Features ON:  {on}")
    log.info(f"⏸️  Features OFF: {off}")

    try:
        # ── Read watermark BEFORE building pipeline ───────────
        # We need the since_id for the X search query.
        # Source of truth: DuplicateFilter's state file.
        # Creating the instance here loads the state automatically.
        dup_filter = DuplicateFilter()
        dup_filter.setup()   # Loads state_duplicate_filter.json (with migration)
        since_id   = dup_filter.get_watermark()
        log.info(f"📌 Watermark (since_id): {since_id or '(none — first run)'}")

        # ── Fetch tweets ──────────────────────────────────────
        new_tweets = await fetch_new_tweets(since_id=since_id)

        if not new_tweets:
            log.info("✓ No new tweets this run.")
            return

        new_tweets.sort(key=lambda t: t.id)   # Oldest first → correct channel order
        log.info(f"📬 {len(new_tweets)} new tweet(s)")

        # ── Build pipeline (dup_filter already set up) ────────
        pipeline = build_pipeline(dup_filter)
        # Don't call pipeline.setup() for dup_filter again — pass it pre-set up.
        # Call setup() only for the remaining processors:
        for p in pipeline._processors[1:]:   # Skip dup_filter (index 0)
            p.setup()

        log.info(pipeline.describe())

        # ── Process each tweet ────────────────────────────────
        sent = skipped = failed = 0

        for tweet in new_tweets:
            set_log_context(section="pipeline", tweet_id=str(tweet.id))
            preview = tweet.rawContent[:70].replace("\n", " ")
            log.info(f"Processing: {preview}…")

            ctx = TweetContext(tweet_id=str(tweet.id), raw_text=tweet.rawContent)
            ctx = await pipeline.run(ctx)

            if not ctx.should_forward:
                log.info(f"Skipped: {ctx.stop_reason}")
                skipped += 1
            elif "telegram_sender" in ctx.ran:
                sent += 1
            else:
                log.warning(f"No send attempt — ran: {ctx.ran}")
                failed += 1

            await asyncio.sleep(1.5)   # Telegram rate-limit buffer

        # ── Teardown ──────────────────────────────────────────
        set_log_context(section="main", tweet_id="-")
        pipeline.teardown()   # Saves all processor states

        log.info(f"📊 sent={sent} skipped={skipped} failed={failed}")

    except Exception as exc:
        set_log_context(section="main", tweet_id="-")
        log_exception(log, exc, "Fatal unhandled exception")
        raise

    finally:
        elapsed = time.monotonic() - run_start
        log.info(f"🏁 Complete in {elapsed:.1f}s")
        flush_and_stop()


if __name__ == "__main__":
    asyncio.run(main())
