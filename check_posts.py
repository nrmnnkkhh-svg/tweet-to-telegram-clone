#!/usr/bin/env python3
"""
Last 5 tweets from @IranIntlBrk + last 5 forwarded IDs from main & clone channels.
"""
import asyncio, json, os, sys
from twscrape import API

X_USER        = "IranIntlBrk"
MAIN_STATE    = os.path.expanduser("~/tweet-to-telegram/state.json")
CLONE_STATE   = os.path.expanduser("~/tweet-to-telegram-clone/state.json")
BOT_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
X_AUTH_TOKEN  = os.environ.get("X_AUTH_TOKEN", "")
X_CT0         = os.environ.get("X_CT0", "")
X_BURNER      = os.environ.get("X_USERNAME", "burner")

def read_state(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            content = f.read().strip()
            if not content:
                return {}
            return json.loads(content)
    except Exception:
        return {}

async def fetch_tweets(limit=5):
    api = API()
    cookie_str = f"auth_token={X_AUTH_TOKEN}; ct0={X_CT0}"
    await api.pool.add_account_cookies(X_BURNER, cookie_str)
    acc = await api.pool.get_account(X_BURNER)
    if not acc.active:
        print("❌ Burner account not active – check cookies.")
        return []
    user = await api.user_by_login(X_USER)
    user_id = user.id
    tweets = []
    async for t in api.user_tweets(user_id, limit=limit):
        tweets.append(t)
        if len(tweets) >= limit:
            break
    return tweets

def print_tweets(tweets):
    print("\n" + "="*60)
    print("📡 @IranIntlBrk – Last 5 tweets on X")
    print("="*60)
    if not tweets:
        print("No tweets found.")
        return
    for t in tweets:
        text = (t.rawContent or "").replace("\n", " ")[:120]
        print(f"  [{t.id}] {text}…")
        print(f"  https://x.com/{X_USER}/status/{t.id}\n")

def print_telegram_ids(label, state_path):
    print("\n" + "="*60)
    print(f"💬 {label} – Last 5 forwarded tweet IDs")
    print("="*60)
    state = read_state(state_path)
    recent = state.get("recent_ids", [])[-5:]
    last_id = state.get("last_tweet_id", "?")
    if not recent:
        print("  No recent IDs found.")
        return
    for tid in reversed(recent):
        print(f"  🆔 {tid}  → https://x.com/{X_USER}/status/{tid}")
    print(f"\n  last_tweet_id stored: {last_id}")

async def main():
    if not X_AUTH_TOKEN or not X_CT0:
        print("❌ Set X_AUTH_TOKEN and X_CT0 environment variables first.")
        sys.exit(1)

    tweets = await fetch_tweets(5)
    print_tweets(tweets)

    print_telegram_ids("Main channel (@Intlbrk)", MAIN_STATE)
    print_telegram_ids("Clone channel (@CloneIntlbrk)", CLONE_STATE)

if __name__ == "__main__":
    asyncio.run(main())
