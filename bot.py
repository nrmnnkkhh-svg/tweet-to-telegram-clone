import asyncio, json, os, traceback
from difflib import SequenceMatcher
import aiohttp
from twikit import Client

TWITTER_USER   = "IranIntlBrk"
TELEGRAM_CHAT  = "@CloneIntlbrk"
TOKEN          = os.environ["TELEGRAM_BOT_TOKEN"]
COOKIES_STR    = os.environ["X_COOKIES_CLONE"]
STATE_FILE     = "state.json"
TEMPLATE_FILE  = "template.txt"

SEPARATOR = "\n\n"

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
    return load_template().replace("{text}", "").strip()

def parse_cookies(cookie_string: str) -> dict:
    cookies = {}
    for part in cookie_string.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            cookies[k] = v
    return cookies

def is_similar(a: str, b: str, threshold=0.7) -> bool:
    return SequenceMatcher(None, a, b).ratio() >= threshold

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
                        print(f"⏳ Rate limited. Waiting {wait}s")
                        await asyncio.sleep(wait + 2)
                        continue
                    print(f"❌ Telegram API error: {data}")
                    return None
        except Exception as exc:
            print(f"❌ Telegram send error: {exc}")
            await asyncio.sleep(2 ** attempt)
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
                        print(f"⏳ Edit rate limited. Waiting {wait}s")
                        await asyncio.sleep(wait + 2)
                        continue
                    print(f"❌ Edit error: {data}")
                    return False
        except Exception as exc:
            print(f"❌ Edit error: {exc}")
            await asyncio.sleep(2 ** attempt)
    return False

def build_thread_text(texts: list[str], footer: str) -> str:
    safe_texts = [t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") for t in texts]
    combined = SEPARATOR.join(safe_texts)
    if footer:
        combined += "\n\n" + footer
    return combined

async def main():
    print("🚀 Run started")

    try:
        cookies = parse_cookies(COOKIES_STR)
        client = Client(language="en-US")
        client.set_cookies(cookies)

        # Bypass broken x-client-transaction-id init
        async def noop_transaction_init(http, ct_headers):
            print("Bypassing x-client-transaction-id init")
            return

        def fake_generate_transaction_id(method="GET", path="/"):
            return "00000000000000000000000000000000"

        client.client_transaction.init = noop_transaction_init
        client.client_transaction.generate_transaction_id = fake_generate_transaction_id
        if not hasattr(client.client_transaction, "key"):
            try:
                client.client_transaction.key = ""
            except Exception:
                pass

        print("✅ Cookies set")

        # Raw user lookup
        raw_user_response, _ = await client.gql.user_by_screen_name(TWITTER_USER)
        user_data = raw_user_response.get("data", {}).get("user", {}).get("result", {})
        user_id = user_data.get("rest_id") or user_data.get("id_str") or str(user_data.get("id", ""))
        if not user_id:
            raise Exception("Could not find user ID")
        print(f"✅ User ID: {user_id}")

        # Raw tweet fetch
        raw_tweets_response, _ = await client.gql.user_tweets(user_id, cursor=None, count=30)

        tweets = []
        instructions = raw_tweets_response.get("data", {}).get("user", {}).get("result", {}).get("timeline_v2", {}).get("timeline", {}).get("instructions", [])
        for instruction in instructions:
            if instruction.get("type") != "TimelineAddEntries":
                continue
            for entry in instruction.get("entries", []):
                tweet_result = entry.get("content", {}).get("itemContent", {}).get("tweet_results", {}).get("result", {})
                if not tweet_result:
                    continue
                tid = tweet_result.get("rest_id")
                legacy = tweet_result.get("legacy", {})
                text = legacy.get("full_text", "")
                conv_id = str(legacy.get("conversation_id_str") or tid)
                if tid and text:
                    tweets.append({"id": int(tid), "text": text, "conv_id": conv_id})

        print(f"📥 Fetched {len(tweets)} tweets")
    except Exception as e:
        print(f"❌ Fetch failed: {e}")
        traceback.print_exc()
        return

    if not tweets:
        print("No tweets")
        return

    state = load_state()
    last_id = int(state.get("last_tweet_id", 0))
    thread_map = state.get("thread_messages", {})
    footer = get_footer()

    new_tweets = []
    for t in tweets:
        tid = int(t["id"])
        if tid <= last_id:
            continue
        new_tweets.append(t)

    if not new_tweets:
        print("No new tweets")
    else:
        new_tweets.sort(key=lambda x: x["id"])
        for tw in new_tweets:
            conv_id = tw["conv_id"]
            existing = thread_map.get(conv_id)

            if existing and existing.get("msg_id"):
                last_text = existing["texts"][-1] if existing["texts"] else ""
                if last_text and is_similar(tw["text"], last_text):
                    print("Similarity dedup triggered – deleting old msg")

            if existing and existing.get("msg_id"):
                all_texts = existing["texts"] + [tw["text"]]
                combined = build_thread_text(all_texts, footer)
                if await edit_message(existing["msg_id"], combined):
                    existing["texts"] = all_texts
                    existing["combined"] = combined
                    existing["last_tweet_id"] = str(tw["id"])
                    thread_map[conv_id] = existing
                    state["total_sent"] = state.get("total_sent", 0) + 1
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

            state["last_tweet_id"] = str(tw["id"])
            save_state(state)
            await asyncio.sleep(1.5)

    state["thread_messages"] = thread_map
    save_state(state)
    print("✅ Run complete")

if __name__ == "__main__":
    asyncio.run(main())
