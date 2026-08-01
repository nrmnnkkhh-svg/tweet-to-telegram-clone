#!/usr/bin/env python3
"""
Weekly Iran news topics generator – now with real headlines.
1. Fetches recent Iran headlines from Google News RSS (free, no key).
2. Asks Groq to extract the 10 most important topics from those headlines.
3. Saves the result to weekly_context.json.
"""
import asyncio, json, os, sys
import aiohttp
from datetime import datetime, timezone

GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

# Google News RSS for “Iran” in English (world news)
RSS_URL = "https://news.google.com/rss/search?q=iran&hl=en-US&gl=US&ceid=US:en"

async def fetch_headlines():
    """Returns a list of recent headlines from Google News RSS."""
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(RSS_URL) as resp:
                if resp.status != 200:
                    print(f"⚠️ RSS fetch failed: HTTP {resp.status}")
                    return []
                raw = await resp.text()

        # Simple XML parsing for <title> tags (headlines are in <title> after removing feed title)
        import xml.etree.ElementTree as ET
        root = ET.fromstring(raw)
        headlines = []
        for item in root.findall(".//item"):
            title = item.find("title").text
            if title:
                headlines.append(title)
        # Remove the feed's own title (first one is usually the feed name)
        if headlines:
            headlines = headlines[1:]   # drop the channel title
        return headlines[:30]  # take up to 30 recent headlines
    except Exception as e:
        print(f"⚠️ RSS parsing error: {e}")
        return []

async def generate_topics(api_key: str, headlines: list) -> list:
    """Ask Groq to extract the top 10 important Iran news topics from the headlines."""
    if not headlines:
        return []

    joined = "\n".join(f"- {h}" for h in headlines)
    prompt = f"""Below are recent news headlines about Iran. Identify the 10 most important, recurring, or significant topics from these headlines.

Headlines:
{joined}

Return ONLY a raw JSON array of exactly 10 short topic strings (each under 15 words). Only include topics that clearly appear in the headlines. Zero preamble, zero explanation, zero markdown fences. ONLY the JSON array.

Example: ["US sanctions more Iranian entities", "Iran enriches uranium to 60%", ...]"""

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json"
    }
    payload = {
        "model":       GROQ_MODEL,
        "messages":    [{"role": "user", "content": prompt}],
        "max_tokens":  700,
        "temperature": 0.2
    }

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as sess:
        async with sess.post(GROQ_URL, headers=headers, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Groq API HTTP {resp.status}: {body[:400]}")
            data = await resp.json()

    raw = data["choices"][0]["message"]["content"].strip()
    for fence in ["```json", "```JSON", "```"]:
        if raw.startswith(fence):
            raw = raw[len(fence):]
            break
    raw = raw.rstrip("`").strip()

    topics = json.loads(raw)
    if not isinstance(topics, list):
        raise ValueError(f"Expected JSON array, got {type(topics).__name__}")
    return [str(t).strip() for t in topics[:10] if str(t).strip()]


async def main():
    api_key = os.environ.get("AI_API_KEY", "")
    if not api_key:
        print("❌ AI_API_KEY environment variable is not set")
        sys.exit(1)

    now      = datetime.now(timezone.utc)
    week_str = now.strftime("%Y-W%W")
    date_str = now.strftime("%Y-%m-%d")

    print(f"📅 Generating Iran news topics for week {week_str} ({date_str})")
    print("📰 Fetching real headlines from Google News…")
    headlines = await fetch_headlines()
    if not headlines:
        print("❌ No headlines fetched – cannot generate accurate topics.")
        sys.exit(1)
    print(f"   → {len(headlines)} headlines retrieved")

    print(f"🤖 Asking Groq to extract top 10 topics…")
    try:
        topics = await generate_topics(api_key, headlines)
    except json.JSONDecodeError as exc:
        print(f"❌ AI returned invalid JSON: {exc}")
        sys.exit(1)
    except Exception as exc:
        print(f"❌ Topic generation failed: {exc}")
        sys.exit(1)

    print(f"\n✅ Generated {len(topics)} topics:")
    for i, topic in enumerate(topics, 1):
        print(f"   {i:2}. {topic}")

    context = {
        "updated_at": now.isoformat(),
        "week":       week_str,
        "topics":     topics
    }

    with open("weekly_context.json", "w", encoding="utf-8") as f:
        json.dump(context, f, indent=2, ensure_ascii=False)

    print(f"\n💾 Saved weekly_context.json successfully")


if __name__ == "__main__":
    asyncio.run(main())
