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

SEPARATOR = "\n\n"

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

async def send_message(text: str, tweet_id: str) -> int | None:
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
                        print(f"✅ Sent tweet {tweet_id} → msg {data['result']['message_id']}")
                        return data["result"]["message_id"]
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

def strip_footer(text: str, footer: str) -> str:
    if footer and text.endswith(footer):
        return text[:-len(footer)].rstrip()
    return text

def build_thread_text(texts: list[str], footer: str) -> str:
    safe_texts = [t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") for t in texts]
    combined = SEPARATOR.join(safe_texts)
    if footer:
        combined += "\n\n" + footer
    return combined

async def main():
    print("🚀 Run started")
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

    if not raw_tweets:
        print("⚠️ No tweets"); return

    state = load_state()
    last_id = int(state.get("last_tweet_id", 0))
    thread_map = state.get("thread_messages", {})
    footer = get_footer()

    new_tweets = []
    for t in raw_tweets:
        tid = int(t.id)
        if tid <= last_id:
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
            conv_id = tw["conv_id"]
            existing = thread_map.get(conv_id)

            if existing and existing.get("msg_id"):
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
                            "msg_id": msg_id,
                            "last_tweet_id": str(tw["id"]),
                            "texts": [tw["text"]],
                            "combined": tw["text"],
                        }
                        state["total_sent"] = state.get("total_sent", 0) + 1
                        await asyncio.sleep(1.5)
            else:
                msg_id = await send_message(tw["text"], str(tw["id"]))
                if msg_id:
                    thread_map[conv_id] = {
                        "msg_id": msg_id,
                        "last_tweet_id": str(tw["id"]),
                        "texts": [tw["text"]],
                        "combined": tw["text"],
                    }
                    state["total_sent"] = state.get("total_sent", 0) + 1
                    await asyncio.sleep(1.5)
                else:
                    print("❌ Failed to send, stopping")
                    return

            state["last_tweet_id"] = str(tw["id"])
            save_state(state)

    state["thread_messages"] = thread_map
    save_state(state)
    print("✅ Finished processing")

if __name__ == "__main__":
    asyncio.run(main())
