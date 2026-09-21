"""Production opt-in with per-generation circuit breakers and free fallbacks."""
from __future__ import annotations

import os

from .provider_http import ProviderFailure


class FallbackProvider:
    def __init__(self, production, development, budget, kind):
        self.production, self.development = production, development
        self.budget, self.kind = budget, kind
        self.failed = False

    def _call(self, method, *args, **kwargs):
        if not self.failed:
            try:
                return getattr(self.production, method)(*args, **kwargs)
            except ProviderFailure as exc:
                # BudgetExceeded is deliberately NOT caught here.
                self.failed = True
                self.budget.event(self.kind, "DEVELOPMENT_FALLBACK", error_code=str(exc))
        return getattr(self.development, method)(*args, **kwargs)

    def generate(self, **kwargs):
        return self._call("generate", **kwargs)

    def create(self, *args):
        return self._call("create", *args)

    def synthesize(self, *args):
        return self._call("synthesize", *args)


def production_provider(kind, default, budget, history=None):
    from .llm_provider import ChatStoryProvider
    from .runway_provider import RunwayVisualProvider
    from .elevenlabs_provider import ElevenLabsVoiceProvider

    requirements = {
        "script": ("STORY_LLM_API_KEY",),
        "visual": ("RUNWAYML_API_SECRET",),
        "voice": ("ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID"),
    }
    missing = [name for name in requirements[kind] if not os.getenv(name, "").strip()]
    if missing:
        budget.event(kind, "DEVELOPMENT_FALLBACK", missing_configuration=missing)
        return default()
    factories = {
        "script": lambda: ChatStoryProvider(budget, history=history),
        "visual": lambda: RunwayVisualProvider(budget),
        "voice": lambda: ElevenLabsVoiceProvider(budget),
    }
    return FallbackProvider(factories[kind](), default(), budget, kind)
