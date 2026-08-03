"""
pipeline.py — Core pipeline architecture.

Classes defined here:
  TweetContext    — data envelope flowing through the pipeline
  ProcessorState  — isolated file-backed state for one processor
  BaseProcessor   — abstract base for all pipeline stages
  Pipeline        — orchestrator: runs processors in order, enforces isolation

Nothing in this file knows about tweets, Telegram, or Groq.
It is pure infrastructure.
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_log = logging.getLogger("bot.pipeline")


# ─────────────────────────────────────────────────────────────
#  TweetContext
#  The single object that passes through every processor.
#  Processors READ from it and WRITE metadata into it.
#  They never call each other directly — they communicate
#  only via ctx.metadata.
# ─────────────────────────────────────────────────────────────
@dataclass
class TweetContext:
    """
    Data envelope for one tweet moving through the pipeline.

    Fields set at creation:
      tweet_id    — string ID of the tweet
      raw_text    — original tweet text

    Fields managed by the pipeline:
      should_forward — set to False by ctx.stop() to halt this tweet
      stop_reason    — human-readable reason for stopping
      ran            — names of processors that have completed
      metadata       — arbitrary data deposited by processors
                       e.g. metadata["importance"] = "IMPORTANT"
                            metadata["formatted_message"] = "..."
    """
    tweet_id:       str
    raw_text:       str
    should_forward: bool = True
    stop_reason:    str  = ""
    ran:            list = field(default_factory=list)
    metadata:       dict = field(default_factory=dict)

    def stop(self, reason: str) -> None:
        """
        Signal the pipeline to stop processing this tweet.
        Use inside a processor when the tweet should not be forwarded:
            ctx.stop("duplicate")
            ctx.stop("content_filter:off_topic")
        """
        self.should_forward = False
        self.stop_reason    = reason

    def mark_ran(self, name: str) -> None:
        """Called by Pipeline after a processor finishes (success or fault-tolerant failure)."""
        if name not in self.ran:
            self.ran.append(name)

    def dependency_met(self, name: str) -> bool:
        """True if the named processor has already run for this tweet."""
        return name in self.ran

    def get_meta(self, key: str, default: Any = None) -> Any:
        """Convenience: read from metadata with a default."""
        return self.metadata.get(key, default)


# ─────────────────────────────────────────────────────────────
#  ProcessorState
#  Isolated, file-backed key-value store for a single processor.
#
#  Each processor gets its OWN file:
#    DuplicateFilter  → state_duplicate_filter.json
#    AIClassifier     → state_ai_classifier.json
#    TelegramSender   → state_telegram_sender.json
#
#  A bug that corrupts one file CANNOT affect another.
# ─────────────────────────────────────────────────────────────
class ProcessorState:
    """
    Usage inside a processor:
        def setup(self) -> None:
            super().setup()             # calls self.state.load()
            last = self.state.get("last_id", 0)

        def teardown(self) -> None:
            self.state.save()           # persist all changes
            super().teardown()

        async def process(self, ctx):
            self.state.set("last_id", ctx.tweet_id)   # in-memory update
            self.state.save()                          # flush to disk now
    """

    def __init__(self, processor_name: str) -> None:
        self._path   = Path(f"state_{processor_name}.json")
        self._data:  dict = {}
        self._pname: str  = processor_name

    # ── Disk I/O ──────────────────────────────────────────────
    def load(self) -> "ProcessorState":
        """Load from disk. Returns self for chaining in setup()."""
        if not self._path.exists():
            _log.debug(f"[{self._pname}] No state file yet — starting empty")
            return self
        try:
            with open(self._path, encoding="utf-8") as f:
                self._data = json.load(f)
            _log.debug(f"[{self._pname}] State loaded: {self._path} ({len(self._data)} keys)")
        except json.JSONDecodeError as exc:
            bak = Path(str(self._path) + ".corrupted")
            _log.error(
                f"[{self._pname}] State file corrupted! "
                f"Renaming to {bak} and starting fresh. Error: {exc}"
            )
            self._path.rename(bak)
            self._data = {}
        except Exception as exc:
            _log.error(f"[{self._pname}] Cannot read state: {exc} — using empty state")
            self._data = {}
        return self

    def save(self) -> None:
        """Persist all in-memory state to disk immediately."""
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
        except Exception as exc:
            _log.error(f"[{self._pname}] CRITICAL: Cannot write state to {self._path}: {exc}")

    # ── Key-value access ──────────────────────────────────────
    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Update in-memory. Call .save() to persist."""
        self._data[key] = value

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def all(self) -> dict:
        """Return a copy of all state data."""
        return dict(self._data)

    def __repr__(self) -> str:
        return f"ProcessorState({self._path}, {len(self._data)} keys)"


# ─────────────────────────────────────────────────────────────
#  BaseProcessor
#  Inherit from this to create a pipeline stage.
#
#  MINIMAL IMPLEMENTATION:
#    class MyFeature(BaseProcessor):
#        name           = "my_feature"
#        fault_tolerant = True      # doesn't break core if it fails
#        depends_on     = []
#
#        async def process(self, ctx: TweetContext) -> TweetContext:
#            # do work, write to ctx.metadata
#            return ctx
#
#  That's it. State, logging, and error handling are provided.
# ─────────────────────────────────────────────────────────────
class BaseProcessor(ABC):
    """
    Abstract base for all pipeline stages.

    Class attributes to override:
      name            (str)       — unique snake_case identifier
      fault_tolerant  (bool)      — True: exception → log + continue
                                    False: exception → stop this tweet
      depends_on      (list[str]) — processor names that must have run first

    Methods to override:
      process(ctx)    — REQUIRED: the processor's logic
      setup()         — OPTIONAL: called once at pipeline start (load state here)
      teardown()      — OPTIONAL: called once at pipeline end (save state here)
    """

    # ── Override these in subclasses ──────────────────────────
    name:           str       = "base_processor"
    fault_tolerant: bool      = False
    depends_on:     list[str] = []

    def __init__(self) -> None:
        self.state = ProcessorState(self.name)
        self._log  = logging.getLogger(f"bot.pipeline.{self.name}")

    # ── Lifecycle ─────────────────────────────────────────────
    def setup(self) -> None:
        """
        Called once before the first tweet is processed.
        Default: loads state from disk.
        Override to open connections, load weekly context, etc.
        Always call super().setup() when overriding.
        """
        self.state.load()
        self._log.debug(f"Setup complete | state={self.state}")

    def teardown(self) -> None:
        """
        Called once after the last tweet is processed.
        Default: saves state to disk.
        Always call super().teardown() when overriding.
        """
        self.state.save()
        self._log.debug("Teardown complete — state saved")

    # ── Core method ───────────────────────────────────────────
    @abstractmethod
    async def process(self, ctx: TweetContext) -> TweetContext:
        """
        Process one tweet.
        Rules:
          • Read from ctx.raw_text, ctx.tweet_id, ctx.metadata
          • Write results into ctx.metadata
          • Call ctx.stop("reason") to prevent forwarding
          • ALWAYS return ctx (even if you called ctx.stop())
          • Never raise intentionally — let exceptions bubble to Pipeline
          • Never import or call another processor directly
        """
        ...

    def __repr__(self) -> str:
        ft  = "⚡fault-tolerant" if self.fault_tolerant else "!critical"
        dep = f" depends={self.depends_on}" if self.depends_on else ""
        return f"{type(self).__name__}[{self.name}|{ft}{dep}]"


# ─────────────────────────────────────────────────────────────
#  Pipeline
#  Runs processors in registration order.
#  Enforces fault isolation, dependency checking, and logging.
# ─────────────────────────────────────────────────────────────
class Pipeline:
    """
    Orchestrates a sequence of processors for each tweet.

    Usage:
        pipeline = Pipeline()
        pipeline.register(
            DuplicateFilter(),
            AIClassifier(api_key=...),   # only if is_enabled("ai_classifier")
            MessageFormatter(...),
            TelegramSender(...),
        )
        pipeline.setup()                 # once, before tweets
        for tweet in tweets:
            ctx = TweetContext(tweet_id=str(tweet.id), raw_text=tweet.rawContent)
            ctx = await pipeline.run(ctx)
            if ctx.should_forward:
                log.info("forwarded")
            else:
                log.info(f"skipped: {ctx.stop_reason}")
        pipeline.teardown()              # once, after tweets
    """

    def __init__(self) -> None:
        self._processors: list[BaseProcessor] = []

    def register(self, *processors: BaseProcessor) -> "Pipeline":
        """
        Add one or more processors. Returns self for chaining.
        Disabled processors should be gated by is_enabled() BEFORE calling register().
        """
        for p in processors:
            self._processors.append(p)
            _log.debug(f"Registered: {p}")
        return self

    def setup(self) -> None:
        """Call once before processing any tweets. Calls setup() on each processor."""
        _log.info(f"Pipeline setup | {len(self._processors)} processor(s)")
        for p in self._processors:
            try:
                p.setup()
            except Exception as exc:
                _log.error(f"❌ {p.name}: setup FAILED: {exc}", exc_info=exc)
                # Setup failure is always fatal — don't start a broken pipeline
                raise RuntimeError(f"Pipeline setup failed at {p.name!r}") from exc

    def teardown(self) -> None:
        """Call once after all tweets. Calls teardown() on each processor."""
        for p in self._processors:
            try:
                p.teardown()
            except Exception as exc:
                _log.error(f"⚠️  {p.name}: teardown failed: {exc}", exc_info=exc)
                # Teardown failures are logged but don't prevent others from running

    async def run(self, ctx: TweetContext) -> TweetContext:
        """
        Run one TweetContext through all registered processors.

        Stops early if:
          • ctx.should_forward becomes False (ctx.stop() was called)
          • A critical (fault_tolerant=False) processor raises an exception
          • A dependency is not met for a critical processor

        Returns the final ctx for inspection by the caller.
        """
        for processor in self._processors:

            # ── Already stopped? ──────────────────────────────
            if not ctx.should_forward:
                _log.debug(
                    f"Pipeline already stopped before {processor.name!r} "
                    f"— reason: {ctx.stop_reason!r}"
                )
                break

            # ── Check dependencies ─────────────────────────────
            unmet = [d for d in processor.depends_on if not ctx.dependency_met(d)]
            if unmet:
                if processor.fault_tolerant:
                    _log.warning(
                        f"⚡ {processor.name}: unmet dependencies {unmet} "
                        f"— skipping (fault-tolerant)"
                    )
                    continue   # Skip this processor, keep going
                else:
                    _log.error(
                        f"❌ {processor.name}: unmet dependencies {unmet} "
                        f"— stopping tweet"
                    )
                    ctx.stop(f"unmet_dependency:{unmet}")
                    break

            # ── Run the processor ──────────────────────────────
            try:
                ctx = await processor.process(ctx)
                ctx.mark_ran(processor.name)
                _log.debug(
                    f"✅ {processor.name}: complete "
                    f"(forward={ctx.should_forward}, "
                    f"meta_keys={list(ctx.metadata.keys())})"
                )
            except Exception as exc:
                if processor.fault_tolerant:
                    _log.error(
                        f"⚡ {processor.name}: FAILED (fault-tolerant) — continuing pipeline",
                        exc_info=exc,
                    )
                    ctx.mark_ran(processor.name)   # Mark ran even on failure
                else:
                    _log.error(
                        f"❌ {processor.name}: FAILED (critical) — stopping tweet",
                        exc_info=exc,
                    )
                    ctx.stop(f"critical_failure:{processor.name}")
                    break

        return ctx

    def describe(self) -> str:
        """Human-readable pipeline description for logging."""
        if not self._processors:
            return "Pipeline(empty)"
        stages = " → ".join(
            f"[{p.name}{'⚡' if p.fault_tolerant else '!'}]"
            for p in self._processors
        )
        return f"Pipeline: {stages}"

    def __len__(self) -> int:
        return len(self._processors)
