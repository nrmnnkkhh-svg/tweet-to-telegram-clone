"""
features.py — Central feature flag registry.

This is the ONLY place to enable or disable features.
Never put feature flags inside processor code.

Rules:
  • True  = processor is registered and runs
  • False = processor is never instantiated (zero performance cost)
  • To test a new feature: enable it in clone workflow via BOT_INSTANCE env,
    keep it False here for main until proven stable.

Adding a new feature:
  1. Add its entry here (default: False)
  2. Create the processor class in processors.py
  3. Add the is_enabled() guard in bot.py
  Done. Zero other files change.
"""

FEATURES: dict[str, bool] = {

    # ── Core stages (almost never disable) ────────────────────
    "duplicate_filter":  True,
    "message_formatter": True,
    "telegram_sender":   True,

    # ── Optional enhancements ─────────────────────────────────
    "ai_classifier":     True,     # Groq importance classification

    # ── In development (enable on clone only) ─────────────────
    "deletion_checker":  False,    # Detect deleted tweets on X
    "thread_merger":     False,    # Merge tweet threads into one message
    "media_handler":     False,    # Handle image/video attachments
}

# Per-instance overrides — read by bot.py based on BOT_INSTANCE env var.
# Use this to enable a feature on clone before enabling it for main.
_INSTANCE_OVERRIDES: dict[str, dict[str, bool]] = {
    "clone": {
        "deletion_checker": True,   # Testing deletion_checker on clone only
    },
    "main": {},
}


def is_enabled(feature: str, instance: str = "main") -> bool:
    """
    Check if a feature is enabled for the given bot instance.
    Instance-level overrides take precedence over the global FEATURES dict.

    Usage:
        from features import is_enabled
        import os
        INSTANCE = os.environ.get("BOT_INSTANCE", "main")

        if is_enabled("ai_classifier", INSTANCE):
            pipeline.register(AIClassifier(...))
    """
    override = _INSTANCE_OVERRIDES.get(instance, {})
    if feature in override:
        return override[feature]
    return FEATURES.get(feature, False)


def feature_summary(instance: str = "main") -> dict[str, bool]:
    """Return the effective feature flags for a given instance."""
    result = dict(FEATURES)
    result.update(_INSTANCE_OVERRIDES.get(instance, {}))
    return result
