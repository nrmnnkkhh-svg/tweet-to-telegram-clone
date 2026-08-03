# ================== FEATURE FLAGS ==================
FEATURE_THREAD_MERGE             = True
FEATURE_AI_CLASSIFICATION        = False
FEATURE_DELETION_CHECK           = False
FEATURE_DUPLICATE_PREVENTION     = False
FEATURE_PAUSE_MECHANISM          = False
FEATURE_SIMILARITY_DEDUP = True
# ===================================================

import asyncio, json, os, random, traceback
from difflib import SequenceMatcher
import aiohttp
from twscrape import API

from logger import (
    setup_logging, get_logger, set_log_context, log_exception, flush_and_stop
)

TWITTER_USER   = "IranIntlBrk"
TELEGRAM_CHAT  = "@CloneIntlbrk"
TOKEN          = os.environ["TELEGRAM_BOT_TOKEN"]
COOKIES        = os.environ["X_COOKIES_CLONE"]
STATE_FILE     = "state.json"
TEMPLATE_FILE  = "template.txt"

BURNER_USERNAME = "NRMNDIDI"
SEPARATOR       = "\n\n"

api = API()

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"last_tweet_id": None, "thread_messages": {}, "total_sent": 0}
    with open(STATE_FILE) as f:
        state = json.load(f)
    state.setdefault("last_tweet_id", None)
    state.setdefault("thread_messages", {})
    state.setdefault("total_sent", 0)
    return state

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def load_template():
    with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
        return f.read().strip()

def get_footer():
    template = load_template()
    return template.replace("{text}", "").strip()

def is_similar(text1: str, text2: str, threshold: float = 0.7) -> bool:
    """Return True if text1 and text2 are at least `threshold` similar."""
    return SequenceMatcher(None, text1, text2).ratio() >= threshold

async def send_message(text: str, tweet_id: str) -> int | None:
    log = get_logger("send_message")
    set_log_context(tweet_id=tweet_id)
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    msg = load_template().replace("{text}", safe)
    payload = {"chat_id": TELEGRAM_CHAT, "text": msg, "disable_web_page_preview": True}
    for attempt in range(5):
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(url, json=payload) as resp:
                    data = await resp.json()
                    if data.get("ok"):
                        msg_id = data["result"]["message_id"]
                        log.info(f"Sent tweet → msg {msg_id}")
                        return msg_id
                    if data.get("error_code") == 429:
                        wait = data.get("parameters", {}).get("retry_after", 10)
                        log.warning(f"Rate limited. Waiting {wait}s (attempt {attempt+1}/5)")
                        await asyncio.sleep(wait + 2)
                        continue
                    log.error(f"Telegram API rejected: {data}")
                    return None
        except Exception as exc:
            log_exception(log, exc, f"Telegram network error (attempt {attempt+1})")
            await asyncio.sleep(2 ** attempt + random.uniform(0, 2))
    log.error("Failed to send after 5 attempts")
    return None

async def edit_message(msg_id: int, new_text: str) -> bool:
    log = get_logger("edit_message")
    url = f"https://api.telegram.org/bot{TOKEN}/editMessageText"
    payload = {"chat_id": TELEGRAM_CHAT, "message_id": msg_id, "text": new_text, "disable_web_page_preview": True}
    for attempt in range(5):
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(url, json=payload) as resp:
                    data = await resp.json()
                    if data.get("ok"):
                        log.info(f"Edited msg {msg_id}")
                        return True
                    if data.get("error_code") == 429:
                        wait = data.get("parameters", {}).get("retry_after", 10)
                        log.warning(f"Rate limited. Waiting {wait}s")
                        await asyncio.sleep(wait + 2)
                        continue
                    log.error(f"Edit rejected: {data}")
                    return False
        except Exception as exc:
            log_exception(log, exc, f"Edit error (attempt {attempt+1})")
            await asyncio.sleep(2 ** attempt + random.uniform(0, 2))
    return False

async def delete_message(msg_id: int) -> bool:
    log = get_logger("delete_message")
    url = f"https://api.telegram.org/bot{TOKEN}/deleteMessage"
    payload = {"chat_id": TELEGRAM_CHAT, "message_id": msg_id}
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(url, json=payload) as resp:
                    data = await resp.json()
                    if data.get("ok"):
                        log.info(f"Deleted msg {msg_id}")
                        return True
                    if data.get("error_code") == 429:
                        wait = data.get("parameters", {}).get("retry_after", 10)
                        log.warning(f"Delete rate limited. Waiting {wait}s")
                        await asyncio.sleep(wait + 2)
                        continue
                    log.error(f"Delete error: {data}")
                    return False
        except Exception as exc:
            log_exception(log, exc, f"Delete error (attempt {attempt+1})")
            await asyncio.sleep(2 ** attempt)
    return False

def build_thread_text(texts: list[str], footer: str) -> str:
    safe_texts = [t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") for t in texts]
    combined = SEPARATOR.join(safe_texts)
    if footer:
        combined += "\n\n" + footer
    return combined

async def main():
    log = setup_logging()
    set_log_context("main")
    log.info("Run started")

    try:
        await api.pool.add_account_cookies(BURNER_USERNAME, COOKIES)
        log.info("Cookies loaded")
        acc = await api.pool.get_account(BURNER_USERNAME)
        if not acc.active:
            log.error("Account not active"); return
        user = await api.user_by_login(TWITTER_USER)
        user_id = user.id
        log.info(f"User ID: {user_id}")

        raw_tweets = []
        seen = set()
        async for t in api.user_tweets(user_id, limit=30):
            if t.id not in seen:
                seen.add(t.id)
                raw_tweets.append(t)
                if len(raw_tweets) >= 30:
                    break
        raw_tweets.sort(key=lambda t: t.id, reverse=True)
        log.info(f"Fetched {len(raw_tweets)} tweets")
    except Exception as e:
        log_exception(log, e, "Fetch failed")
        return

    if not raw_tweets:
        log.info("No tweets"); return

    state = load_state()
    last_id = int(state.get("last_tweet_id", 0))
    thread_map = state.get("thread_messages", {})
    footer = get_footer()

    new_tweets = []
    for t in raw_tweets:
        tid = int(t.id)
        if tid <= last_id:
            log.debug(f"Skipping duplicate tweet {tid}")
            continue
        text = t.rawContent or ""
        if not text:
            continue
        conv_id = str(getattr(t, "conversationId", tid))
        new_tweets.append({"id": tid, "text": text, "conv_id": conv_id})

    if not new_tweets:
        log.info("No new tweets")
    else:
        new_tweets.sort(key=lambda x: x["id"])
        for tw in new_tweets:
            conv_id = tw["conv_id"]
            existing = thread_map.get(conv_id)
            set_log_context(section="process_tweet", tweet_id=str(tw["id"]))

            # ── Similarity dedup check ─────────────────────────
            # Only if the feature is ON, and the new tweet is part of an existing thread
            if FEATURE_SIMILARITY_DEDUP and existing and existing.get("msg_id"):
                last_text_in_thread = existing["texts"][-1] if existing["texts"] else ""
                if last_text_in_thread and is_similar(tw["text"], last_text_in_thread):
                    log.info(f"Similarity dedup triggered – deleting old msg {existing['msg_id']} and sending new tweet")
                    # Delete the old Telegram message
                    if await delete_message(existing["msg_id"]):
                        # Remove thread entry so it's no longer tracked
                        del thread_map[conv_id]
                        # Fall through to send as standalone tweet
                        existing = None
                    else:
                        log.error("Failed to delete old message – keeping both")

            # ── Thread merge or send new ───────────────────────
            if FEATURE_THREAD_MERGE and existing and existing.get("msg_id"):
                all_texts = existing["texts"] + [tw["text"]]
                combined = build_thread_text(all_texts, footer)
                if await edit_message(existing["msg_id"], combined):
                    existing["texts"] = all_texts
                    existing["combined"] = combined
                    existing["last_tweet_id"] = str(tw["id"])
                    thread_map[conv_id] = existing
                    state["total_sent"] = state.get("total_sent", 0) + 1
                    await asyncio.sleep(1.5)
                else:
                    msg_id = await send_message(tw["text"], str(tw["id"]))
                    if msg_id:
                        thread_map[conv_id] = {
                            "msg_id": msg_id, "last_tweet_id": str(tw["id"]),
                            "texts": [tw["text"]], "combined": tw["text"],
                        }
                        state["total_sent"] = state.get("total_sent", 0) + 1
                        await asyncio.sleep(1.5)
            else:
                msg_id = await send_message(tw["text"], str(tw["id"]))
                if msg_id:
                    thread_map[conv_id] = {
                        "msg_id": msg_id, "last_tweet_id": str(tw["id"]),
                        "texts": [tw["text"]], "combined": tw["text"],
                    }
                    state["total_sent"] = state.get("total_sent", 0) + 1
                    await asyncio.sleep(1.5)
                else:
                    log.error("Failed to send tweet, stopping")
                    return

            state["last_tweet_id"] = str(tw["id"])
            save_state(state)

    state["thread_messages"] = thread_map
    save_state(state)
    log.info("Run complete")

    try:
        pass
    finally:
        flush_and_stop()

if __name__ == "__main__":
    asyncio.run(main())
