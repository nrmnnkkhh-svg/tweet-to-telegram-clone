#!/usr/bin/env python3
"""Post the current weekly Iran topics to the channel and pin the message."""
import asyncio, json, os, sys
import aiohttp

TOKEN       = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID     = "@CloneIntlbrk"
CONTEXT_FILE = "weekly_context.json"

async def send_message(text: str) -> int | None:
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    async with aiohttp.ClientSession() as sess:
        async with sess.post(url, json=payload) as resp:
            data = await resp.json()
            if data.get("ok"):
                return data["result"]["message_id"]
            print(f"❌ Send error: {data}")
            return None

async def unpin_all():
    url = f"https://api.telegram.org/bot{TOKEN}/unpinAllChatMessages"
    payload = {"chat_id": CHAT_ID}
    async with aiohttp.ClientSession() as sess:
        async with sess.post(url, json=payload) as resp:
            data = await resp.json()
            if data.get("ok"):
                print("📌 Old pins cleared")
                return True
            print(f"⚠️ Unpin error (maybe no pins): {data}")
            return False

async def pin_message(msg_id: int):
    url = f"https://api.telegram.org/bot{TOKEN}/pinChatMessage"
    payload = {"chat_id": CHAT_ID, "message_id": msg_id, "disable_notification": True}
    async with aiohttp.ClientSession() as sess:
        async with sess.post(url, json=payload) as resp:
            data = await resp.json()
            if data.get("ok"):
                print(f"📌 Pinned message {msg_id}")
                return True
            print(f"❌ Pin error: {data}")
            return False

async def main():
    # Load the topics
    with open(CONTEXT_FILE, "r", encoding="utf-8") as f:
        ctx = json.load(f)
    topics = ctx.get("topics", [])
    week   = ctx.get("week", "?")
    if not topics:
        print("❌ No topics found in weekly_context.json")
        sys.exit(1)

    # Build message
    lines = [f"📌 <b>Iran Top News Topics</b> – Week {week}\n"]
    for i, t in enumerate(topics, 1):
        lines.append(f"{i}. {t}")
    lines.append(f"\n🆔 {CHAT_ID}")
    text = "\n".join(lines)

    # Send, unpin old, pin new
    msg_id = await send_message(text)
    if not msg_id:
        sys.exit(1)

    await unpin_all()
    await pin_message(msg_id)

if __name__ == "__main__":
    asyncio.run(main())
