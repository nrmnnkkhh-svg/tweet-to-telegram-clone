"""
processors.py — All concrete pipeline processors.

Each class handles exactly one pipeline stage.
Adding a new feature = add a new class here.
Never modify an existing class to add a feature.

Processor inventory:
  DuplicateFilter   — CRITICAL  — blocks already-forwarded tweets
  AIClassifier      — OPTIONAL  — Groq importance labelling
  MessageFormatter  — CRITICAL  — builds the Telegram HTML message
  TelegramSender    — CRITICAL  — delivers the message
  DeletionChecker   — OPTIONAL  — [in dev] detects deleted tweets (example of new feature)
"""
from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path
from typing import Optional

import aiohttp

from pipeline import BaseProcessor, TweetContext


# ─────────────────────────────────────────────────────────────
#  Processor 1 — DuplicateFilter
# ─────────────────────────────────────────────────────────────
class DuplicateFilter(BaseProcessor):
    """
    Compares incoming tweet_id against the stored watermark.
    Stops the pipeline if the tweet has already been forwarded.

    State file: state_duplicate_filter.json
      {"last_seen_tweet_id": "18500012345"}

    Also handles one-time migration from the old state.json format.
    """
    name:           str       = "duplicate_filter"
    fault_tolerant: bool      = False   # Can't determine duplicates without state → abort
    depends_on:     list[str] = []

    def setup(self) -> None:
        """Load state, then migrate from old state.json if needed."""
        super().setup()   # loads state_duplicate_filter.json

        # One-time migration from the old monolithic state.json
        if not self.state.get("last_seen_tweet_id"):
            old_path = Path("state.json")
            if old_path.exists():
                try:
                    with open(old_path) as f:
                        old = json.load(f)
                    old_id = old.get("last_tweet_id")
                    if old_id:
                        self.state.set("last_seen_tweet_id", str(old_id))
                        self.state.save()
                        self._log.info(
                            f"Migrated state.json → state_duplicate_filter.json "
                            f"(last_seen_tweet_id={old_id!r})"
                        )
                except Exception as exc:
                    self._log.warning(f"Could not migrate state.json: {exc}")

    async def process(self, ctx: TweetContext) -> TweetContext:
        last_id = int(self.state.get("last_seen_tweet_id", 0))

        if int(ctx.tweet_id) <= last_id:
            ctx.stop(f"duplicate (id={ctx.tweet_id} ≤ watermark={last_id})")
            self._log.debug(f"Blocked: {ctx.tweet_id} (already forwarded)")
            return ctx

        self._log.debug(f"New tweet confirmed: {ctx.tweet_id} > watermark={last_id}")
        return ctx

    def advance_watermark(self, tweet_id: str) -> None:
        """
        Advance the duplicate-check watermark after a successful send.
        Called by TelegramSender via the reference injected at construction.
        Saves immediately so crash recovery is correct.
        """
        current = int(self.state.get("last_seen_tweet_id", 0))
        if int(tweet_id) > current:
            self.state.set("last_seen_tweet_id", str(tweet_id))
            self.state.save()
            self._log.info(f"Watermark: {current} → {tweet_id}")

    def get_watermark(self) -> int:
        """Return the current watermark (used by bot.py to build the search since_id)."""
        return int(self.state.get("last_seen_tweet_id", 0))


# ─────────────────────────────────────────────────────────────
#  Processor 2 — AIClassifier
# ─────────────────────────────────────────────────────────────
class AIClassifier(BaseProcessor):
    """
    Labels each tweet as IMPORTANT or NON_IMPORTANT using Groq.
    Writes result to ctx.metadata["importance"].

    Fault-tolerant: if Groq is unavailable, defaults to IMPORTANT
    so no breaking news is ever silently dropped.

    State file: state_ai_classifier.json (currently unused; reserved
    for future rate-limit tracking or call caching).
    """
    name:           str       = "ai_classifier"
    fault_tolerant: bool      = True   # Groq failure → IMPORTANT default, pipeline continues
    depends_on:     list[str] = []

    GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
    GROQ_MODEL = "llama-3.1-8b-instant"

    def __init__(self, api_key: str, weekly_context: Optional[dict] = None) -> None:
        super().__init__()
        self._api_key        = api_key
        self._weekly_context = weekly_context or {}

    async def process(self, ctx: TweetContext) -> TweetContext:
        if not self._api_key:
            self._log.debug("No API key — defaulting to IMPORTANT")
            ctx.metadata["importance"] = "IMPORTANT"
            return ctx

        topics = self._weekly_context.get("topics", [])
        week   = self._weekly_context.get("week", "this week")

        ctx_block = ""
        if topics:
            ctx_block = (
                f"Top Iran news topics for {week}:\n"
                + "\n".join(f"- {t}" for t in topics)
                + "\n\n"
            )

        prompt = (
            f"{ctx_block}"
            f'Tweet from @IranIntlBrk:\n"{ctx.raw_text}"\n\n'
            "IMPORTANT = military strike, nuclear milestone, sanctions, diplomatic crisis, "
            "mass casualty, high-level political decision, proxy major attack\n"
            "NON_IMPORTANT = minor update, culture, sports, background info, repeated coverage\n\n"
            "Reply ONLY one word: IMPORTANT or NON_IMPORTANT"
        )

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type":  "application/json",
        }
        payload = {
            "model":       self.GROQ_MODEL,
            "messages":    [{"role": "user", "content": prompt}],
            "max_tokens":  6,
            "temperature": 0,
        }

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        ) as sess:
            async with sess.post(self.GROQ_URL, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    raise RuntimeError(
                        f"Groq HTTP {resp.status}: {(await resp.text())[:100]}"
                    )
                data   = await resp.json()
                result = data["choices"][0]["message"]["content"].strip().upper()

        importance = (
            "NON_IMPORTANT"
            if any(tok in result for tok in ("NON", "NOT", "UNIMPORT"))
            else "IMPORTANT"
        )
        ctx.metadata["importance"] = importance
        self._log.info(f"→ {importance}")
        return ctx


# ─────────────────────────────────────────────────────────────
#  Processor 3 — MessageFormatter
# ─────────────────────────────────────────────────────────────
class MessageFormatter(BaseProcessor):
    """
    Builds the Telegram HTML message string and stores it in
    ctx.metadata["formatted_message"].

    Reads ctx.metadata["importance"] if AIClassifier ran.
    Falls back to IMPORTANT if AIClassifier was skipped or failed.
    Stateless: no state file is used.
    """
    name:           str       = "message_formatter"
    fault_tolerant: bool      = False
    depends_on:     list[str] = []   # Works with or without ai_classifier

    def __init__(self, twitter_user: str, channel: str) -> None:
        super().__init__()
        self._twitter_user = twitter_user
        self._channel      = channel

    async def process(self, ctx: TweetContext) -> TweetContext:
        importance = ctx.metadata.get("importance", "IMPORTANT")
        link       = f"https://x.com/{self._twitter_user}/status/{ctx.tweet_id}"

        label = (
            "🔴 &lt;Important&gt;"
            if importance == "IMPORTANT"
            else "⚪ &lt;Non-Important&gt;"
        )
        safe_text = (
            ctx.raw_text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

        ctx.metadata["formatted_message"] = (
            f"{label}\n"
            f"─────────────────────\n"
            f"📡 <b>Iran Intl Breaking</b>\n\n"
            f"{safe_text}\n\n"
            f"<a href='{link}'>🔗 View on X</a>"
        )
        ctx.metadata["tweet_link"] = link
        self._log.debug(f"Formatted (importance={importance})")
        return ctx


# ─────────────────────────────────────────────────────────────
#  Processor 4 — TelegramSender
# ─────────────────────────────────────────────────────────────
class TelegramSender(BaseProcessor):
    """
    Delivers ctx.metadata["formatted_message"] to the Telegram channel.
    On success, calls dup_filter.advance_watermark() to persist progress.

    Depends on message_formatter: if that didn't run, we have nothing to send.
    """
    name:           str       = "telegram_sender"
    fault_tolerant: bool      = False
    depends_on:     list[str] = ["message_formatter"]

    def __init__(
        self,
        bot_token:   str,
        channel:     str,
        dup_filter:  Optional[DuplicateFilter] = None,
    ) -> None:
        super().__init__()
        self._token      = bot_token
        self._channel    = channel
        self._dup_filter = dup_filter   # Injected reference — advances watermark on success

    async def process(self, ctx: TweetContext) -> TweetContext:
        msg = ctx.metadata.get("formatted_message")
        if not msg:
            raise RuntimeError(
                "formatted_message missing from ctx.metadata — "
                "MessageFormatter must run before TelegramSender"
            )

        url     = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {
            "chat_id":                  self._channel,
            "text":                     msg,
            "parse_mode":               "HTML",
            "disable_web_page_preview": True,
        }

        for attempt in range(1, 6):
            async with aiohttp.ClientSession() as sess:
                async with sess.post(url, json=payload) as resp:
                    data = await resp.json()

            if data.get("ok"):
                self._log.info(f"✅ Sent to {self._channel}")
                # Advance watermark IMMEDIATELY after confirmed delivery
                # This way, if we crash mid-batch, already-sent tweets won't be retried
                if self._dup_filter:
                    self._dup_filter.advance_watermark(ctx.tweet_id)
                return ctx

            if data.get("error_code") == 429:
                wait = data.get("parameters", {}).get("retry_after", 10)
                self._log.warning(f"Rate limited — waiting {wait}s (attempt {attempt}/5)")
                await asyncio.sleep(wait + 2)
                continue

            raise RuntimeError(f"Telegram API error: {data}")

        raise RuntimeError(f"Failed after 5 attempts for tweet {ctx.tweet_id}")


# ─────────────────────────────────────────────────────────────
#  Processor 5 — DeletionChecker (example of a new feature)
#
#  This demonstrates how to add a new feature without touching
#  any existing processor. It is currently disabled in features.py.
#  When ready: set "deletion_checker": True in features.py (clone first).
# ─────────────────────────────────────────────────────────────
class DeletionChecker(BaseProcessor):
    """
    [IN DEVELOPMENT — disabled in main, enabled on clone for testing]

    Checks if the current tweet was recently posted and then deleted on X.
    If deleted, posts a notice to Telegram instead of forwarding.

    This class exists in isolation:
      • It only reads ctx.tweet_id and ctx.raw_text
      • It writes ctx.metadata["deletion_status"]
      • Zero changes needed to DuplicateFilter, AIClassifier, or TelegramSender
      • If it crashes, fault_tolerant=True means core forwarding continues
    """
    name:           str       = "deletion_checker"
    fault_tolerant: bool      = True   # New feature → tolerant until proven stable
    depends_on:     list[str] = []

    async def process(self, ctx: TweetContext) -> TweetContext:
        # Placeholder — implement tweet existence check here
        # Example: HEAD request to the tweet URL, check for 404
        ctx.metadata["deletion_status"] = "unchecked"
        self._log.debug(f"Deletion check: {ctx.tweet_id} → unchecked (stub)")
        return ctx


# ─────────────────────────────────────────────────────────────
#  Processor 6 — ThreadMerger
# ─────────────────────────────────────────────────────────────
class ThreadMerger(BaseProcessor):
    """
    Merges tweets belonging to the same thread into one Telegram message.

    - First tweet of a thread: sends a normal message and records it.
    - Subsequent tweets: edits the existing message to append the new text.
    - When enabled, this processor REPLACES MessageFormatter + TelegramSender
      for thread tweets (they are never called for those tweets).
    - Non‑thread tweets pass through unchanged and are handled by the normal
      formatter + sender.

    Fault‑tolerant: if editing fails (e.g. message too old), it falls back
    to sending a new message.
    """
    name:           str       = "thread_merger"
    fault_tolerant: bool      = True
    depends_on:     list[str] = []

    SEPARATOR = "\n\n"

    def __init__(self, twitter_user: str, channel: str, bot_token: str) -> None:
        super().__init__()
        self._twitter_user = twitter_user
        self._channel      = channel
        self._token        = bot_token

    # ── Helpers ──────────────────────────────────────────────
    def _load_template(self) -> str:
        """Read the channel template (template.txt)."""
        try:
            with open("template.txt", "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return "{text}\n\n🆔 @Intlbrk"   # fallback

    def _get_footer(self) -> str:
        return self._load_template().replace("{text}", "").strip()

    async def _send_message(self, text: str) -> int | None:
        """Send a new message to the channel. Returns message_id or None."""
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {"chat_id": self._channel, "text": text, "disable_web_page_preview": True}
        for attempt in range(1, 6):
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.post(url, json=payload) as resp:
                        data = await resp.json()
                        if data.get("ok"):
                            return data["result"]["message_id"]
                        if data.get("error_code") == 429:
                            wait = data.get("parameters", {}).get("retry_after", 10)
                            self._log.warning(f"Rate limited — waiting {wait}s")
                            await asyncio.sleep(wait + 2)
                            continue
                        self._log.error(f"Send error: {data}")
                        return None
            except Exception as exc:
                self._log.error(f"Send attempt {attempt} failed: {exc}")
                await asyncio.sleep(2 ** attempt)
        return None

    async def _edit_message(self, msg_id: int, new_text: str) -> bool:
        """Edit an existing message. Returns True on success."""
        url = f"https://api.telegram.org/bot{self._token}/editMessageText"
        payload = {"chat_id": self._channel, "message_id": msg_id, "text": new_text, "disable_web_page_preview": True}
        for attempt in range(1, 6):
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.post(url, json=payload) as resp:
                        data = await resp.json()
                        if data.get("ok"):
                            return True
                        if data.get("error_code") == 429:
                            wait = data.get("parameters", {}).get("retry_after", 10)
                            self._log.warning(f"Edit rate limited — waiting {wait}s")
                            await asyncio.sleep(wait + 2)
                            continue
                        self._log.error(f"Edit error: {data}")
                        return False
            except Exception as exc:
                self._log.error(f"Edit attempt {attempt} failed: {exc}")
                await asyncio.sleep(2 ** attempt)
        return False

    def _format_single(self, text: str) -> str:
        """Format a single tweet using the channel template."""
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return self._load_template().replace("{text}", safe)

    def _build_combined(self, texts: list[str], footer: str) -> str:
        """Combine multiple tweet texts into one message, footer only at the end."""
        safe_texts = [t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") for t in texts]
        combined = self.SEPARATOR.join(safe_texts)
        if footer:
            combined += "\n\n" + footer
        return combined

    # ── Core logic ───────────────────────────────────────────
    async def process(self, ctx: TweetContext) -> TweetContext:
        conv_id = ctx.metadata.get("conv_id") or ctx.tweet_id   # fallback: own ID
        existing = self.state.all().get(conv_id)                 # dict or None

        # --- First tweet of a new thread ---
        if not existing:
            footer = self._get_footer()
            msg_text = self._format_single(ctx.raw_text)
            msg_id = await self._send_message(msg_text)
            if msg_id:
                self.state.set(conv_id, {
                    "msg_id": msg_id,
                    "last_tweet_id": ctx.tweet_id,
                    "texts": [ctx.raw_text],
                })
                self.state.save()
                self._log.info(f"New thread {conv_id}: sent msg {msg_id}")
                ctx.stop("thread_merger: first tweet sent")   # prevent normal send
            else:
                self._log.error(f"Failed to send first tweet of thread {conv_id} – letting pipeline continue")
                # Fallback: let normal MessageFormatter + TelegramSender handle it
            return ctx

        # --- Subsequent tweet in an existing thread ---
        # Append the new text
        new_texts = existing["texts"] + [ctx.raw_text]
        footer = self._get_footer()
        combined = self._build_combined(new_texts, footer)

        if await self._edit_message(existing["msg_id"], combined):
            existing["texts"] = new_texts
            existing["last_tweet_id"] = ctx.tweet_id
            self.state.set(conv_id, existing)
            self.state.save()
            self._log.info(f"Thread {conv_id}: edited msg {existing['msg_id']}")
            ctx.stop("thread_merger: appended to existing thread")
        else:
            # Edit failed – fallback: send a new message for this thread update
            self._log.warning(f"Edit failed for thread {conv_id}, sending new message")
            msg_id = await self._send_message(combined)
            if msg_id:
                self.state.set(conv_id, {
                    "msg_id": msg_id,
                    "last_tweet_id": ctx.tweet_id,
                    "texts": new_texts,
                })
                self.state.save()
                ctx.stop("thread_merger: sent new message after edit failure")
            # If even the fallback fails, we leave ctx.should_forward=True
            # and let the pipeline continue normally (rare).

        return ctx
