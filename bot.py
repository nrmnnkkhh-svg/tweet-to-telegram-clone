import asyncio, json, os, random, traceback, time
import aiohttp
from twscrape import API

TWITTER_USER   = "IranIntlBrk"
TELEGRAM_CHAT  = "@CloneIntlbrk"
TOKEN          = os.environ["TELEGRAM_BOT_TOKEN"]
COOKIES        = os.environ["X_COOKIES_CLONE"]
AI_API_KEY     = os.environ.get("AI_API_KEY", "")
STATE_FILE     = "state.json"
TEMPLATE_FILE  = "template.txt"
CONTEXT_FILE   = "weekly_context.json"

BURNER_USERNAME = "NRMNDIDI"

SEPARATOR = "\n\n"
MAX_RECENT_IDS = 500
DELETION_CHECK_COUNT = 20
DELETION_MIN_AGE_SEC = 300          # 5 minutes – never delete tweets younger than this

GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.1-8b-instant"

api = API()

# ------------------------------------------------------------
#  State helpers
# ------------------------------------------------------------
def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "last_tweet_id": None,
            "recent_ids": [],
            "thread_messages": {},
            "total_sent": 0,
            "tweet_to_msg": {}
        }
    with open(STATE_FILE, "r") as f:
        state = json.load(f)
    state.setdefault("last_tweet_id", None)
    state.setdefault("recent_ids", [])
    state.setdefault("thread_messages", {})
    state.setdefault("total_sent", 0)
    state.setdefault("tweet_to_msg", {})
    for conv_id, entry in state["thread_messages"].items():
        if "texts" not in entry:
            entry["texts"] = []
        if "combined" not in entry:
            entry["combined"] = entry.get("text", "")
        if "importance" not in entry:
            entry["importance"] = None
    return state

def save_state(state):
    state["recent_ids"] = state["recent_ids"][-MAX_RECENT_IDS:]
    valid_ids = set(state["recent_ids"])
    state["tweet_to_msg"] = {tid: info for tid, info in state["tweet_to_msg"].items() if tid in valid_ids}
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    print(f"💾 State saved (last_tweet_id={state['last_tweet_id']}, recent={len(state['recent_ids'])})")

def load_template():
    with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
        return f.read().strip()

def get_footer():
    template = load_template()
    return template.replace("{text}", "").strip()

# ------------------------------------------------------------
#  AI helpers
# ------------------------------------------------------------
def load_weekly_context() -> dict:
    if os.path.exists(CONTEXT_FILE):
        try:
            with open(CONTEXT_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            print(f"⚠️  Could not parse {CONTEXT_FILE}: {exc}")
    return {"topics": [], "week": "unknown"}

async def classify_tweet(tweet_text: str) -> str:
    if not AI_API_KEY:
        return "IMPORTANT"

    ctx    = load_weekly_context()
    topics = ctx.get("topics", [])
    week   = ctx.get("week", "this week")

    if topics:
        topic_list   = "\n".join(f"- {t}" for t in topics)
        context_part = f"Top Iran news topics for {week}:\n{topic_list}\n\n"
    else:
        context_part = ""

    prompt = f"""{context_part}Tweet from @IranIntlBrk (Iran International breaking news):
"{tweet_text}"

Classify this tweet:
IMPORTANT   = significant development: military strike, sanctions package, nuclear milestone, high-level political decision, mass casualty event, diplomat expulsion, proxy group major attack, leadership change
NON_IMPORTANT = minor update, cultural/social news, sports, historical background, routine protest report, unverified rumor, celebrity statement, economic statistic without major context

Reply with ONLY one word — either IMPORTANT or NON_IMPORTANT. No punctuation, no explanation."""

    headers = {
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type":  "application/json"
    }
    payload = {
        "model":       GROQ_MODEL,
        "messages":    [{"role": "user", "content": prompt}],
        "max_tokens":  6,
        "temperature": 0
    }

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as sess:
            async with sess.post(GROQ_URL, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    print(f"    ⚠️  Groq {resp.status}: {body[:120]} — default IMPORTANT")
                    return "IMPORTANT"
                data   = await resp.json()
                result = data["choices"][0]["message"]["content"].strip().upper()

        if "NON" in result or "NOT" in result or "UNIMPORT" in result:
            return "NON_IMPORTANT"
        return "IMPORTANT"

    except asyncio.TimeoutError:
        print("    ⚠️  Groq timeout — default IMPORTANT")
        return "IMPORTANT"
    except Exception as exc:
        print(f"    ⚠️  Groq error: {exc} — default IMPORTANT")
        return "IMPORTANT"

# ------------------------------------------------------------
#  Telegram helpers
# ------------------------------------------------------------
async def send_message(text: str, tweet_id: str) -> int | None:
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT, "text": text, "disable_web_page_preview": True}
    for attempt in range(5):
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(url, json=payload) as resp:
                    data = await resp.json()
                    if data.get("ok"):
                        msg_id = data["result"]["message_id"]
                        print(f"✅ Sent tweet {tweet_id} → msg {msg_id}")
                        return msg_id
                    if data.get("error_code") == 429:
                        wait = data.get("parameters", {}).get("retry_after", 10)
                        print(f"⏳ Rate limited, waiting {wait}s…")
                        await asyncio.sleep(wait + 2)
                        continue
                    print(f"❌ Telegram error: {data}")
                    return None
        except Exception as exc:
            print(f"❌ Telegram error (attempt {attempt+1}): {exc}")
            await asyncio.sleep(2 ** attempt + random.uniform(0, 2))
    return None

async def edit_message(msg_id: int, new_text: str) -> bool:
    url = f"https://api.telegram.org/bot{TOKEN}/editMessageText"
    payload = {"chat_id": TELEGRAM_CHAT, "message_id": msg_id, "text": new_text, "disable_web_page_preview": True}
    for attempt in range(5):
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(url, json=payload) as resp:
                    data = await resp.json()
                    if data.get("ok"):
                        print(f"✅ Edited msg {msg_id}")
                        return True
                    if data.get("error_code") == 429:
                        wait = data.get("parameters", {}).get("retry_after", 10)
                        print(f"⏳ Rate limited, waiting {wait}s…")
                        await asyncio.sleep(wait + 2)
                        continue
                    print(f"❌ Edit error: {data}")
                    return False
        except Exception as exc:
            print(f"❌ Edit error (attempt {attempt+1}): {exc}")
            await asyncio.sleep(2 ** attempt + random.uniform(0, 2))
    return False

async def delete_message(msg_id: int) -> bool:
    url = f"https://api.telegram.org/bot{TOKEN}/deleteMessage"
    payload = {"chat_id": TELEGRAM_CHAT, "message_id": msg_id}
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(url, json=payload) as resp:
                    data = await resp.json()
                    if data.get("ok"):
                        print(f"🗑️  Deleted msg {msg_id}")
                        return True
                    if data.get("error_code") == 429:
                        wait = data.get("parameters", {}).get("retry_after", 10)
                        print(f"⏳ Rate limited, waiting {wait}s…")
                        await asyncio.sleep(wait + 2)
                        continue
                    print(f"❌ Delete error: {data}")
                    return False
        except Exception as exc:
            print(f"❌ Delete error (attempt {attempt+1}): {exc}")
            await asyncio.sleep(2 ** attempt)
    return False

# ------------------------------------------------------------
#  Formatting
# ------------------------------------------------------------
def format_ai_message(text: str, importance: str) -> str:
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if importance == "IMPORTANT":
        label = "🔴 &lt;Important&gt;"
    else:
        label = "⚪ &lt;Non-Important&gt;"
    template = load_template()
    return template.replace("{text}", f"{label}\n{safe}")

def format_thread_with_label(texts: list[str], importance: str, footer: str) -> str:
    safe_texts = [t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") for t in texts]
    if importance == "IMPORTANT":
        label = "🔴 &lt;Important&gt;"
    else:
        label = "⚪ &lt;Non-Important&gt;"
    combined = label + "\n" + SEPARATOR.join(safe_texts)
    if footer:
        combined += "\n\n" + footer
    return combined

# ------------------------------------------------------------
#  Deletion check (NOW WITH MINIMUM AGE)
# ------------------------------------------------------------
async def check_deleted_tweets(state, thread_map):
    tweet_to_msg = state.get("tweet_to_msg", {})
    recent_ids = state.get("recent_ids", [])
    if not recent_ids:
        return

    now = time.time()
    # Collect candidates that have a Telegram mapping AND are old enough
    candidates = []
    for tid in recent_ids:
        if tid not in tweet_to_msg:
            continue
        info = tweet_to_msg[tid]
        forwarded_at = info.get("forwarded_at")
        if not forwarded_at:
            continue
        age = now - forwarded_at
        if age < DELETION_MIN_AGE_SEC:
            continue   # too recent, skip
        candidates.append(tid)

    if not candidates:
        print(f"🔍 No tweets old enough for deletion check yet")
        return

    newest_first = list(reversed(candidates))
    to_check = newest_first[:DELETION_CHECK_COUNT]

    print(f"🔍 Checking {len(to_check)} tweets for deletion (min age {DELETION_MIN_AGE_SEC}s)...")
    for tid in to_check:
        try:
            tweet = await api.tweet_details(tid)
            if tweet is None:
                print(f"🗑️  Tweet {tid} not found / deleted")
                await handle_deleted_tweet(tid, state, thread_map, tweet_to_msg)
        except Exception as e:
            print(f"⚠️ Error checking tweet {tid}: {e}")
        await asyncio.sleep(1)

async def handle_deleted_tweet(tid: str, state, thread_map, tweet_to_msg):
    info = tweet_to_msg.get(tid)
    if not info:
        return
    msg_id = info["msg_id"]
    conv_id = info.get("conv_id")
    is_thread = info.get("is_thread", False)

    if not is_thread or not conv_id or conv_id not in thread_map:
        await delete_message(msg_id)
        del tweet_to_msg[tid]
        if tid in state["recent_ids"]:
            state["recent_ids"].remove(tid)
    else:
        thread_entry = thread_map.get(conv_id)
        if not thread_entry:
            return
        deleted_text = info.get("text", "")
        texts = thread_entry.get("texts", [])
        if deleted_text in texts:
            texts.remove(deleted_text)
        if not texts:
            await delete_message(msg_id)
            del thread_map[conv_id]
            for t, inf in list(tweet_to_msg.items()):
                if inf.get("conv_id") == conv_id:
                    del tweet_to_msg[t]
                    if t in state["recent_ids"]:
                        state["recent_ids"].remove(t)
        else:
            footer = get_footer()
            importance = thread_entry.get("importance") or "IMPORTANT"
            combined = format_thread_with_label(texts, importance, footer)
            if await edit_message(msg_id, combined):
                thread_entry["texts"] = texts
                thread_entry["combined"] = combined
                del tweet_to_msg[tid]
                if tid in state["recent_ids"]:
                    state["recent_ids"].remove(tid)

# ------------------------------------------------------------
#  Main
# ------------------------------------------------------------
async def main():
    print("🚀 Run started")
    if os.path.exists("paused.txt"):
        print("⏸️  Bot is paused (paused.txt exists). Exiting.")
        return

    if AI_API_KEY:
        ctx = load_weekly_context()
        print(f"🤖 AI classification: ON  |  {len(ctx.get('topics',[]))} topics for {ctx.get('week','?')}")
    else:
        print("⚠️  AI classification: OFF")

    try:
        await api.pool.add_account_cookies(BURNER_USERNAME, COOKIES)
        print("✅ Cookies loaded")
        acc = await api.pool.get_account(BURNER_USERNAME)
        if not acc.active:
            print("Account not active"); return
        user = await api.user_by_login(TWITTER_USER)
        user_id = user.id
        print(f"📌 User ID: {user_id}")

        raw_tweets = []
        seen = set()
        async for t in api.user_tweets(user_id, limit=30):
            if t.id not in seen:
                seen.add(t.id)
                raw_tweets.append(t)
                if len(raw_tweets) >= 30:
                    break
        raw_tweets.sort(key=lambda t: t.id, reverse=True)
        print(f"📥 Got {len(raw_tweets)} tweets")
    except Exception as e:
        print(f"❌ Fetch failed: {e}"); return

    state = load_state()
    last_id = int(state.get("last_tweet_id", 0))
    recent_ids = set(state.get("recent_ids", []))
    thread_map = state.get("thread_messages", {})
    tweet_to_msg = state.get("tweet_to_msg", {})
    footer = get_footer()

    new_tweets = []
    if raw_tweets:
        for t in raw_tweets:
            tid = int(t.id)
            if False and (tid <= last_id or str(tid) in recent_ids):
                print(f"⏭️  Skipping duplicate tweet {tid}")
                continue
            text = t.rawContent or ""
            if not text:
                continue
            conv_id = str(getattr(t, "conversationId", tid))
            new_tweets.append({"id": tid, "text": text, "conv_id": conv_id})

    if not new_tweets:
        print("✓ No new tweets")
    else:
        new_tweets.sort(key=lambda x: x["id"])
        for tw in new_tweets:
            importance = await classify_tweet(tw["text"])
            print(f"  🤖 {importance}")

            conv_id = tw["conv_id"]
            existing = thread_map.get(conv_id)

            if existing and existing.get("msg_id"):
                if not existing.get("importance"):
                    existing["importance"] = importance
                all_texts = existing["texts"] + [tw["text"]]
                combined = format_thread_with_label(all_texts, existing["importance"], footer)
                if await edit_message(existing["msg_id"], combined):
                    existing["texts"] = all_texts
                    existing["combined"] = combined
                    existing["last_tweet_id"] = str(tw["id"])
                    thread_map[conv_id] = existing
                    state["total_sent"] = state.get("total_sent", 0) + 1
                    tweet_to_msg[str(tw["id"])] = {
                        "msg_id": existing["msg_id"],
                        "conv_id": conv_id,
                        "is_thread": True,
                        "text": tw["text"],
                        "forwarded_at": time.time()
                    }
                else:
                    msg_text = format_ai_message(tw["text"], importance)
                    msg_id = await send_message(msg_text, str(tw["id"]))
                    if msg_id:
                        thread_map[conv_id] = {
                            "msg_id": msg_id,
                            "last_tweet_id": str(tw["id"]),
                            "texts": [tw["text"]],
                            "combined": msg_text,
                            "importance": importance
                        }
                        state["total_sent"] = state.get("total_sent", 0) + 1
                        tweet_to_msg[str(tw["id"])] = {
                            "msg_id": msg_id,
                            "conv_id": conv_id,
                            "is_thread": False,
                            "text": tw["text"],
                            "forwarded_at": time.time()
                        }
                    else:
                        print("❌ Failed to send, stopping")
                        return
            else:
                msg_text = format_ai_message(tw["text"], importance)
                msg_id = await send_message(msg_text, str(tw["id"]))
                if msg_id:
                    thread_map[conv_id] = {
                        "msg_id": msg_id,
                        "last_tweet_id": str(tw["id"]),
                        "texts": [tw["text"]],
                        "combined": msg_text,
                        "importance": importance
                    }
                    state["total_sent"] = state.get("total_sent", 0) + 1
                    tweet_to_msg[str(tw["id"])] = {
                        "msg_id": msg_id,
                        "conv_id": conv_id,
                        "is_thread": False,
                        "text": tw["text"],
                        "forwarded_at": time.time()
                    }
                else:
                    print("❌ Failed to send, stopping")
                    return

            # Save state after each successful send
            state["last_tweet_id"] = str(tw["id"])
            recent_ids.add(str(tw["id"]))
            state["recent_ids"] = list(recent_ids)
            state["thread_messages"] = thread_map
            state["tweet_to_msg"] = tweet_to_msg
            save_state(state)
            await asyncio.sleep(1.5)

    # Deletion check (re‑enabled with minimum age)
    #await check_deleted_tweets(state, thread_map)
    save_state(state)

    print("✅ Finished processing")

if __name__ == "__main__":
    asyncio.run(main())
