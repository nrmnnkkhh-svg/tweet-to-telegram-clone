#!/usr/bin/env python3
"""
Weekly Iran news topics generator.
Called by .github/workflows/weekly_topics.yml every Sunday.
Uses Groq API (free tier) to identify current Iran topics.
Saves result to weekly_context.json which bot.py reads for classification context.
"""
import asyncio, json, os, sys
import aiohttp
from datetime import datetime, timezone

GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"


async def generate_topics(api_key: str) -> list:
    prompt = """You are a senior geopolitical analyst specializing in Iran and the Middle East.

Generate a list of the 10 most important CURRENT and ONGOING news topics about Iran in international media right now.

Choose topics that are actively developing and significant, such as:
- Nuclear program status (IAEA access, enrichment percentages, deal talks)
- Active US/EU/UN sanction campaigns
- Iran proxy group operations (Hezbollah, Houthis, Hamas, Iraqi militias)
- Direct Iran-Israel military tensions or strikes
- IRGC commanders, structure, or operations
- Domestic political developments (Supreme Leader, parliament, government)
- Economic collapse indicators (rial value, oil export workarounds, inflation)
- Human rights: mass executions, protests, political prisoners
- Iran's diplomatic positioning (Russia alliance, China deals, Western negotiations)
- Weapons: ballistic missiles, drones, transfers to Russia or proxies

Return ONLY a raw JSON array of exactly 10 short topic strings.
Each topic: under 15 words, specific, factual.
Zero preamble. Zero explanation. Zero markdown fences. ONLY the JSON array.

Correct output format:
["Iran enriches uranium to 84% as IAEA blocked for third time", "US Treasury sanctions 15 entities linked to Iranian oil", ...]"""

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

    # Strip markdown fences in case model adds them despite instructions
    for fence in ["```json", "```JSON", "```"]:
        if raw.startswith(fence):
            raw = raw[len(fence):]
            break
    raw = raw.rstrip("`").strip()

    topics = json.loads(raw)
    if not isinstance(topics, list):
        raise ValueError(f"Expected JSON array, got {type(topics).__name__}")

    # Clean and cap to 10
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
    print(f"🤖 Model: {GROQ_MODEL}")

    try:
        topics = await generate_topics(api_key)
    except json.JSONDecodeError as exc:
        print(f"❌ AI returned invalid JSON: {exc}")
        print("   Keeping previous weekly_context.json unchanged.")
        sys.exit(1)
    except Exception as exc:
        print(f"❌ Topic generation failed: {exc}")
        print("   Keeping previous weekly_context.json unchanged.")
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
