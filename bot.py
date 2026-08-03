"""
bot.py — Pipeline orchestrator (thin entry point).
"""
import asyncio
import json
import os
import time

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
    ThreadMerger,
)

TWITTER_USER  = "IranIntlBrk"
TELEGRAM_CHAT = os.environ.get("TELEGRAM_CHANNEL", "@CloneIntlbrk")
BOT_TOKEN     = os.environ["TELEGRAM_BOT_TOKEN"]
COOKIES       = os.environ["X_COOKIES_CLONE"]
AI_API_KEY    = os.environ.get("AI_API_KEY", "")
INSTANCE      = os.environ.get("BOT_INSTANCE", "main")
CONTEXT_FILE  = "weekly_context.json"

BURNER_USERNAME = "NRMNDIDI"

def load_weekly_context() -> dict:
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
    log = get_logger("fetch_new_tweets")
    set_log_context(section="fetch_new_tweets")
    api = API()
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
    except asyncio.TimeoutError:
        log.warning("Search timed out after 60s")
    except Exception as exc:
        log_exception(log, exc, "Tweet fetch error")
    return tweets

def build_pipeline(dup_filter: DuplicateFilter) -> Pipeline:
    log = get_logger("build_pipeline")
    pipeline = Pipeline()

    # Core: duplicate filter always runs
    pipeline.register(dup_filter)

    # AI classifier (optional)
    if is_enabled("ai_classifier", INSTANCE):
        weekly_ctx = load_weekly_context()
        pipeline.register(AIClassifier(api_key=AI_API_KEY, weekly_context=weekly_ctx))
        log.debug("ai_classifier: registered")

    # Thread merger (optional) — before formatter/sender
    if is_enabled("thread_merger", INSTANCE):
        pipeline.register(ThreadMerger(TWITTER_USER, TELEGRAM_CHAT, BOT_TOKEN))
        log.debug("thread_merger: registered")

    # Deletion checker (optional, in‑dev)
    if is_enabled("deletion_checker", INSTANCE):
        pipeline.register(DeletionChecker())

    # Core: formatter + sender
    pipeline.register(MessageFormatter(TWITTER_USER, TELEGRAM_CHAT))
    pipeline.register(TelegramSender(BOT_TOKEN, TELEGRAM_CHAT, dup_filter=dup_filter))

    return pipeline

async def main() -> None:
    run_start = time.monotonic()
    log = setup_logging()
    set_log_context(section="main")
    log.info("━" * 56)
    log.info(f"🚀 Run | instance={INSTANCE!r} | channel={TELEGRAM_CHAT!r}")
    flags = feature_summary(INSTANCE)
    on = [k for k, v in flags.items() if v]
    off = [k for k, v in flags.items() if not v]
    log.info(f"🏁 Features ON:  {on}")
    log.info(f"⏸️  Features OFF: {off}")

    try:
        dup_filter = DuplicateFilter()
        dup_filter.setup()
        since_id = dup_filter.get_watermark()
        log.info(f"📌 Watermark (since_id): {since_id or '(none — first run)'}")
        new_tweets = await fetch_new_tweets(since_id=since_id)
        if not new_tweets:
            log.info("✓ No new tweets this run.")
            return
        new_tweets.sort(key=lambda t: t.id)
        log.info(f"📬 {len(new_tweets)} new tweet(s)")

        pipeline = build_pipeline(dup_filter)
        # Setup all processors except dup_filter (already done)
        for p in pipeline._processors[1:]:
            p.setup()
        log.info(pipeline.describe())

        sent = skipped = failed = 0
        for tweet in new_tweets:
            set_log_context(section="pipeline", tweet_id=str(tweet.id))
            preview = tweet.rawContent[:70].replace("\n", " ")
            log.info(f"Processing: {preview}…")

            # Attach conv_id to metadata early so ThreadMerger can use it
            conv_id = str(getattr(tweet, "conversationId", tweet.id))
            ctx = TweetContext(tweet_id=str(tweet.id), raw_text=tweet.rawContent)
            ctx.metadata["conv_id"] = conv_id

            ctx = await pipeline.run(ctx)
            if not ctx.should_forward:
                log.info(f"Skipped: {ctx.stop_reason}")
                skipped += 1
            elif "telegram_sender" in ctx.ran:
                sent += 1
            else:
                log.warning(f"No send attempt — ran: {ctx.ran}")
                failed += 1
            await asyncio.sleep(1.5)

        set_log_context(section="main", tweet_id="-")
        pipeline.teardown()
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
