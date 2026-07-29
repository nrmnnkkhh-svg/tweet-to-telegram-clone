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

api = API()

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"last_tweet_id": None, "recent_ids": [], "thread_messages": {}, "total_sent": 0}
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
    except:
        return {"last_tweet_id": None, "recent_ids": [], "thread_messages": {}, "total_sent": 0}
    state.setdefault("last_tweet_id", None)
    state.setdefault("recent_ids", [])
    state.setdefault("thread_messages", {})
    state.setdefault("total_sent", 0)
    return state

def save_state(state):
    state["recent_ids"] = state["recent_ids"][-100:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def load_template():
    with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
        return f.read().strip()

async def send_telegram(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    template = load_template()
    msg = template.replace("{text}", safe)
    payload = {"chat_id": TELEGRAM_CHAT, "text": msg, "disable_web_page_preview": True}
    for attempt in range(5):
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(url, json=payload) as resp:
                    data = await resp.json()
                    if data.get("ok"):
                        print(f"✅ Sent")
                        return True
                    if data.get("error_code") == 429:
                        wait = data.get("parameters", {}).get("retry_after", 10)
                        print(f"⏳ Rate limited, waiting {wait}s…")
                        await asyncio.sleep(wait + 2)
                        continue
                    print(f"❌ Telegram error: {data}")
                    return False
        except Exception as exc:
            print(f"❌ Telegram error (attempt {attempt+1}): {exc}")
            await asyncio.sleep(2 ** attempt + random.uniform(0, 2))
    return False

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
        async for t in api.user_tweets(user_id, limit=20):
            if t.id not in seen:
                seen.add(t.id)
                raw_tweets.append(t)
                # Debug: print tweet attributes
                conv_id = getattr(t, "conversationId", "N/A")
                reply_to = getattr(t, "inReplyToStatusId", "N/A")
                print(f"DEBUG: id={t.id} conv_id={conv_id} reply_to={reply_to}")
                if len(raw_tweets) >= 20:
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
    print(f"📌 Last forwarded tweet ID: {last_id or 'none'}")

    new_tweets = []
    for t in raw_tweets:
        if last_id and int(t.id) <= last_id:
            continue
        if str(t.id) in recent_ids:
            continue
        text = t.rawContent or ""
        if not text:
            continue
        new_tweets.append(t)

    new_tweets.reverse()
    if not new_tweets:
        print("✅ Nothing new")
        return

    print(f"📬 {len(new_tweets)} new tweet(s) to forward")
    success = 0
    for t in new_tweets:
        if not await send_telegram(t.rawContent):
            print("❌ Send failed, stopping batch")
            break
        state["last_tweet_id"] = str(t.id)
        recent_ids.add(str(t.id))
        state["recent_ids"] = list(recent_ids)
        state["total_sent"] = state.get("total_sent", 0) + 1
        save_state(state)
        success += 1
        await asyncio.sleep(1.5)

    print(f"✅ Forwarded {success}/{len(new_tweets)} tweets")

if __name__ == "__main__":
    asyncio.run(main())
