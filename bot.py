import asyncio, json, os, random, traceback
import aiohttp
from twscrape import API

TWITTER_USER   = "IranIntlBrk"
TELEGRAM_CHAT  = "@CloneIntlbrk"
TOKEN          = os.environ["TELEGRAM_BOT_TOKEN"]
COOKIES        = os.environ["X_COOKIES_CLONE"]
STATE_FILE     = "state.json"
TEMPLATE_FILE  = "template.txt"

BURNER_USERNAME = "NRMNDIDI"

MAX_RECENT_IDS = 100   # general duplicate prevention (non‑thread tweets)

api = API()

# ------------------------------------------------------------
#  State helpers
# ------------------------------------------------------------
def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "last_tweet_id": None,
            "recent_ids": [],
            "thread_messages": {},   # { conv_id: {"msg_id": 123, "last_tweet_id": "...", "text": "..."} }
            "total_sent": 0,
        }
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
    except:
        return {
            "last_tweet_id": None,
            "recent_ids": [],
            "thread_messages": {},
            "total_sent": 0,
        }
    # ensure all keys exist
    state.setdefault("last_tweet_id", None)
    state.setdefault("recent_ids", [])
    state.setdefault("thread_messages", {})
    state.setdefault("total_sent", 0)
    return state

def save_state(state):
    # keep recent_ids trimmed
    state["recent_ids"] = state["recent_ids"][-MAX_RECENT_IDS:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def load_template():
    with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
        return f.read().strip()

# ------------------------------------------------------------
#  Telegram helpers
# ------------------------------------------------------------
async def send_message(text: str) -> int | None:
    """Send a new message. Returns message_id on success, else None."""
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT,
        "text": text,
        "disable_web_page_preview": True,
    }
    for attempt in range(5):
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(url, json=payload) as resp:
                    data = await resp.json()
                    if data.get("ok"):
                        msg_id = data["result"]["message_id"]
                        print(f"✅ Sent message {msg_id}")
                        return msg_id
                    if data.get("error_code") == 429:
                        wait = data.get("parameters", {}).get("retry_after", 10)
                        print(f"⏳ Rate limited, waiting {wait}s…")
                        await asyncio.sleep(wait + 2)
                        continue
                    print(f"❌ Telegram error (send): {data}")
                    return None
        except Exception as exc:
            print(f"❌ Telegram network error (attempt {attempt+1}): {exc}")
            await asyncio.sleep(2 ** attempt + random.uniform(0, 2))
    return None

async def edit_message(msg_id: int, new_text: str) -> bool:
    """Edit an existing message. Returns True on success."""
    url = f"https://api.telegram.org/bot{TOKEN}/editMessageText"
    payload = {
        "chat_id": TELEGRAM_CHAT,
        "message_id": msg_id,
        "text": new_text,
        "disable_web_page_preview": True,
    }
    for attempt in range(5):
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(url, json=payload) as resp:
                    data = await resp.json()
                    if data.get("ok"):
                        print(f"✅ Edited message {msg_id}")
                        return True
                    if data.get("error_code") == 429:
                        wait = data.get("parameters", {}).get("retry_after", 10)
                        print(f"⏳ Rate limited, waiting {wait}s…")
                        await asyncio.sleep(wait + 2)
                        continue
                    # If message is too old to edit (400), we can't edit; we'll return False
                    print(f"❌ Edit error: {data}")
                    return False
        except Exception as exc:
            print(f"❌ Edit network error (attempt {attempt+1}): {exc}")
            await asyncio.sleep(2 ** attempt + random.uniform(0, 2))
    return False

# ------------------------------------------------------------
#  Formatting helpers
# ------------------------------------------------------------
SEPARATOR = "\n\n➖➖➖➖➖➖➖➖➖➖\n\n"   # between tweets in a thread

def format_single_tweet(text: str) -> str:
    """Format a standalone (non‑thread) tweet using the template."""
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    template = load_template()
    return template.replace("{text}", safe)

def format_thread_text(tweet_texts: list[str]) -> str:
    """Combine multiple tweet texts into one message, with the footer at the end."""
    combined = SEPARATOR.join(
        t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        for t in tweet_texts
    )
    # Append the footer from template. We'll use the @CloneIntlbrk from template.
    # To keep it simple, we'll just put the footer once at the very end.
    # Extract footer from template (everything after {text})
    template = load_template()
    footer = template.replace("{text}", "").strip()
    if footer:
        combined += "\n\n" + footer
    return combined

# ------------------------------------------------------------
#  Main logic
# ------------------------------------------------------------
async def main():
    print("🚀 Run started")
    try:
        await api.pool.add_account_cookies(BURNER_USERNAME, COOKIES)
        print("✅ Cookies loaded")

        acc = await api.pool.get_account(BURNER_USERNAME)
        print(f"Account active: {acc.active}")
        if not acc.active:
            print("Account not active")
            return

        user = await api.user_by_login(TWITTER_USER)
        user_id = user.id
        print(f"📌 User ID: {user_id}")

        raw_tweets = []
        seen = set()
        async for t in api.user_tweets(user_id, limit=30):   # a bit more to catch threads
            if t.id not in seen:
                seen.add(t.id)
                raw_tweets.append(t)
                if len(raw_tweets) >= 30:
                    break
        raw_tweets.sort(key=lambda t: t.id, reverse=True)
        print(f"📥 Got {len(raw_tweets)} unique tweets")
    except Exception as e:
        print(f"❌ Fetch failed: {e}")
        traceback.print_exc()
        return

    if not raw_tweets:
        print("⚠️ No tweets")
        return

    state = load_state()
    last_id = state.get("last_tweet_id")
    if last_id:
        last_id = int(last_id)
    recent_ids = set(state.get("recent_ids", []))
    thread_map = state.get("thread_messages", {})   # conversation_id -> message info

    print(f"📌 Last forwarded tweet ID: {last_id or 'none'}")
    print(f"📋 Recent ID cache: {len(recent_ids)} entries")

    # Filter out already‑seen tweets
    new_tweets = []
    for t in raw_tweets:
        tid = t.id
        if last_id and tid <= last_id:
            continue
        if str(tid) in recent_ids:
            continue
        text = t.rawContent or ""
            print(f"DEBUG: id={t.id} conv_id={getattr(t, "conversationId", "N/A")}")
        if not text:
            continue
        # t.conversationId is usually the ID of the original tweet in the thread
        conv_id = getattr(t, "conversationId", None)
        new_tweets.append({"obj": t, "id": tid, "text": text, "conv_id": conv_id})

    if not new_tweets:
        print("✅ Nothing new")
        return

    # Group tweets by conversation_id (None = standalone)
    threads = {}
    standalone = []
    for tw in new_tweets:
        cid = tw["conv_id"] or tw["id"]   # standalone tweets: use own ID as key to keep separate
        if cid is None:
            standalone.append(tw)
        else:
            threads.setdefault(cid, []).append(tw)

    # Process standalone tweets first (simple send)
    standalone.sort(key=lambda x: x["id"])
    for tw in standalone:
        url = f"https://x.com/{TWITTER_USER}/status/{tw['id']}"
        msg_text = format_single_tweet(tw["text"])
        print(f"📤 Sending standalone {tw['id']}...")
        msg_id = await send_message(msg_text)
        if msg_id:
            state["last_tweet_id"] = str(tw["id"])
            recent_ids.add(str(tw["id"]))
            state["recent_ids"] = list(recent_ids)
            state["total_sent"] = state.get("total_sent", 0) + 1
            save_state(state)
            await asyncio.sleep(1.5)
        else:
            print("❌ Failed to send standalone tweet, stopping batch")
            return

    # Process each thread
    for conv_id, tweets_in_thread in threads.items():
        tweets_in_thread.sort(key=lambda x: x["id"])
        # Determine which tweets are already included in the existing message
        existing = thread_map.get(conv_id)
        new_texts = []
        all_ids = []
        for tw in tweets_in_thread:
            if existing and str(tw["id"]) <= existing["last_tweet_id"]:
                continue   # already in message
            new_texts.append(tw["text"])
            all_ids.append(str(tw["id"]))

        if not new_texts:
            continue

        if existing and existing["msg_id"]:
            # Edit existing message
            existing_text = existing.get("text", "")
            # Combine old text + new tweets
            new_combined = SEPARATOR.join(
                [existing_text] + [
                    t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    for t in new_texts
                ]
            )
            # Append footer if not already present
            template = load_template()
            footer = template.replace("{text}", "").strip()
            if footer and not new_combined.endswith(footer):
                new_combined = new_combined.rstrip() + "\n\n" + footer

            print(f"📝 Editing message {existing['msg_id']} for thread {conv_id}")
            success = await edit_message(existing["msg_id"], new_combined)
            if success:
                # Update thread state
                existing["text"] = new_combined
                existing["last_tweet_id"] = max(existing["last_tweet_id"], max(all_ids))
                thread_map[conv_id] = existing
                state["last_tweet_id"] = max(state["last_tweet_id"], existing["last_tweet_id"])
                for tid in all_ids:
                    recent_ids.add(tid)
                state["recent_ids"] = list(recent_ids)
                state["total_sent"] = state.get("total_sent", 0) + len(new_texts)
                save_state(state)
                await asyncio.sleep(1.5)
            else:
                # Editing failed (maybe too old). Fallback: send a new message and update mapping.
                print(f"⚠️ Edit failed, sending new message for thread {conv_id}")
                existing = None   # force new message

        if not existing:
            # Send new thread message
            combined = format_thread_text(
                [existing["text"]] + new_texts if existing else new_texts
            )
            msg_id = await send_message(combined)
            if msg_id:
                first_id = all_ids[0]
                last_id = all_ids[-1]
                thread_map[conv_id] = {
                    "msg_id": msg_id,
                    "last_tweet_id": last_id,
                    "text": combined,
                }
                state["thread_messages"] = thread_map
                state["last_tweet_id"] = max(state["last_tweet_id"], last_id)
                for tid in all_ids:
                    recent_ids.add(tid)
                state["recent_ids"] = list(recent_ids)
                state["total_sent"] = state.get("total_sent", 0) + len(new_texts)
                save_state(state)
                await asyncio.sleep(1.5)
            else:
                print("❌ Failed to send thread message, stopping batch")
                return

    # Final state cleanup
    state["thread_messages"] = thread_map
    save_state(state)
    print(f"✅ Finished processing. Total sent (including edits): {state.get('total_sent', 0)}")

if __name__ == "__main__":
    asyncio.run(main())
