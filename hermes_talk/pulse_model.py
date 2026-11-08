"""The one model route Pulse uses: Hermes' host-owned plugin LLM facade (``ctx.llm``).

Upstream Conduit Pulse makes every selection, memory and follow-up call through
``ctx.llm.complete_structured`` (pulse.py ``_memory_llm``). Talk does the same: the
``hermes-telegram-talk`` plugin's ``register(ctx)`` binds its own ``ctx.llm`` here, and the voice
server, which ``hermes talk serve`` starts under the same Hermes interpreter and home, loads
the profile's plugins through the public ``hermes_cli.plugins.discover_plugins`` (from its Pulse
worker, after it is already serving) so that binding happens in its process too. The host owns provider, model, auth, timeouts and
fallback: the profile's own active model, no new account, credential, billing meter or
model role. Talk passes no override, so the plugin trust gate stays fail-closed.

When nothing is bound (a Hermes without ``ctx.llm``, the plugin not enabled, or a call that
failed) callers fall back to deterministic decisions and record that they did.
"""
import json
import logging
import threading

logger = logging.getLogger("tg-voice")

PLUGIN_ID = "hermes-telegram-talk"
CALL_TIMEOUT_S = 60.0

_lock = threading.Lock()
_bound = None


class ModelUnavailable(Exception):
    """No model route, or the call failed or returned no JSON object; the caller degrades honestly."""


def bind(llm):
    """Called from the plugin's register(ctx) with ctx.llm."""
    global _bound
    with _lock:
        _bound = llm


def bound():
    with _lock:
        return _bound


def load_host():
    """Discover the profile's plugins through the public loader, so hermes-telegram-talk binds its ctx.llm here."""
    if bound() is not None:
        return bound()
    try:
        from hermes_cli.plugins import discover_plugins
    except ImportError:
        return None
    try:
        discover_plugins()
    except Exception as exc:  # a plugin loader problem only costs model decisions
        logger.warning("pulse model: plugin discovery failed (%s)", type(exc).__name__)
    return bound()


class PulseModel:
    """Bounded structured calls: instructions + one text input -> one parsed JSON object."""

    def __init__(self, llm=None, timeout=CALL_TIMEOUT_S):
        self._llm = llm
        self.timeout = timeout
        self.calls = []  # (purpose, ok, model) of this process, newest last; for status and gates

    @property
    def llm(self):
        return self._llm if self._llm is not None else bound()

    def available(self):
        return self.llm is not None

    def label(self):
        llm = self.llm
        return f"plugin_llm:{getattr(llm, '_plugin_id', PLUGIN_ID)}" if llm is not None else "none"

    def ask(self, instructions, text, purpose, max_tokens=2000, timeout=None):
        llm = self.llm
        if llm is None:
            raise ModelUnavailable("no model route is bound")
        try:
            result = llm.complete_structured(instructions=instructions, input=[{"type": "text", "text": text}],
                                             json_mode=True, purpose=purpose, timeout=timeout or self.timeout,
                                             max_tokens=max_tokens)
        except Exception as exc:
            self.calls = (self.calls + [(purpose, False, "")])[-50:]
            raise ModelUnavailable(f"{type(exc).__name__}") from exc
        parsed = getattr(result, "parsed", None)
        if parsed is None:
            try:
                parsed = json.loads(getattr(result, "text", "") or "")
            except ValueError:
                parsed = None
        model = f"{getattr(result, 'provider', '')}/{getattr(result, 'model', '')}"
        self.calls = (self.calls + [(purpose, isinstance(parsed, dict), model)])[-50:]
        if not isinstance(parsed, dict):
            raise ModelUnavailable("the reply was not a JSON object")
        return parsed


__all__ = ["ModelUnavailable", "PulseModel", "bind", "bound", "load_host", "PLUGIN_ID"]
