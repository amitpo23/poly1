"""Regression tests for drift fixes landed 2026-05-02.

These guard against three latent bugs found in dead-but-importable
code paths during the pre-launch audit:

1. ``GammaMarketClient.get_events(parse_pydantic=True)`` called
   ``self.parse_event(...)`` — a method that never existed. The class
   defines ``parse_pydantic_event`` and ``parse_nested_event``; the
   typo would have surfaced as ``AttributeError`` the moment any
   caller passed ``parse_pydantic=True``.

2. ``Executor.filter_events(events)`` called
   ``prompter.filter_events(events)`` even though
   ``Prompter.filter_events()`` takes no arguments. Production paths
   use ``filter_events_with_rag`` (which calls ``filter_events()``
   correctly), so the dead method was silently broken. Removed.

3. ``gamma.py`` was using ``print(...)`` for parse errors and HTTP
   failures. Per ``CLAUDE.md``: ``logger`` is the standard;
   ``print`` is reserved for one-shot CLI output.

The tests use ``sys.modules`` stubs to load ``gamma`` without the
heavy ``httpx`` / ``polymarket`` deps that block stdlib-only runs.
"""

from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import MagicMock


def _ensure_module(name: str) -> types.ModuleType:
    """Create-or-fetch a stub module so ``import name`` succeeds in
    stdlib-only environments. Returns the module so tests can attach
    attributes to it."""
    if name in sys.modules:
        return sys.modules[name]
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


def _install_gamma_stubs() -> None:
    """Install just enough stubs that ``import agents.polymarket.gamma``
    works without httpx / requests / pydantic-backed objects on the
    Python path. Idempotent — safe to call from each test.

    In Docker (real deps available) the real modules are imported and no
    stubs are installed. In stdlib-only mode imports fail and we fall
    back to lightweight fakes."""
    try:
        import httpx  # noqa: F401
    except ImportError:
        httpx_mod = _ensure_module("httpx")
        if not hasattr(httpx_mod, "get"):
            httpx_mod.get = MagicMock()

    # gamma.py imports Polymarket at module top but the tests below never
    # construct it. Use the real one if available; otherwise stub.
    try:
        from agents.polymarket import polymarket as _real_poly  # noqa: F401
    except Exception:
        poly_mod = _ensure_module("agents.polymarket.polymarket")
        if not hasattr(poly_mod, "Polymarket"):
            poly_mod.Polymarket = type("Polymarket", (), {})

    # agents.utils.objects — Market / PolymarketEvent / ClobReward / Tag
    # are referenced as type hints + constructors. Real deps if available.
    try:
        from agents.utils import objects as _real_objects  # noqa: F401
    except Exception:
        utils_mod = _ensure_module("agents.utils.objects")
        for cls_name in ("Market", "PolymarketEvent", "ClobReward", "Tag"):
            if not hasattr(utils_mod, cls_name):
                def _make(name: str):
                    def __init__(self, **kwargs):
                        self.__dict__.update(kwargs)
                        self.__name__ = name
                    return type(name, (), {"__init__": __init__})
                setattr(utils_mod, cls_name, _make(cls_name))


class TestGammaParserDrift(unittest.TestCase):
    """Drift fix 1: ``get_events(parse_pydantic=True)`` calls the
    correct parser method."""

    def setUp(self) -> None:
        _install_gamma_stubs()
        # Force a re-import in case a previous test loaded a cached copy
        sys.modules.pop("agents.polymarket.gamma", None)
        import importlib
        self.gamma_mod = importlib.import_module("agents.polymarket.gamma")

    def test_parse_pydantic_event_exists(self) -> None:
        cls = self.gamma_mod.GammaMarketClient
        self.assertTrue(callable(getattr(cls, "parse_pydantic_event", None)))

    def test_parse_event_typo_is_gone(self) -> None:
        """The dead alias ``parse_event`` must NOT be re-introduced —
        ``get_events(parse_pydantic=True)`` would silently regress."""
        cls = self.gamma_mod.GammaMarketClient
        self.assertFalse(
            hasattr(cls, "parse_event"),
            "GammaMarketClient.parse_event must not exist; "
            "use parse_pydantic_event for the unnested-event path.",
        )

    def test_get_events_pydantic_path_uses_parse_pydantic_event(self) -> None:
        """Simulate a 200 response with two event objects and confirm
        ``get_events(parse_pydantic=True)`` routes each through
        ``parse_pydantic_event`` exactly once."""
        cls = self.gamma_mod.GammaMarketClient
        client = cls()

        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = [{"id": "a"}, {"id": "b"}]

        with unittest.mock.patch.object(
            self.gamma_mod.httpx, "get", return_value=fake_response
        ):
            with unittest.mock.patch.object(
                client, "parse_pydantic_event"
            ) as mocked:
                mocked.side_effect = lambda obj: ("parsed", obj["id"])
                result = client.get_events(parse_pydantic=True)

        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(result, [("parsed", "a"), ("parsed", "b")])


class TestExecutorFilterEventsRemoval(unittest.TestCase):
    """Drift fix 2: the dead ``Executor.filter_events`` method (which
    raised TypeError on every call) must stay removed."""

    def test_filter_events_method_does_not_exist(self) -> None:
        # Stub anthropic + dotenv so executor.py can be imported without
        # the runtime deps. We reach for the source file directly to
        # avoid running the heavy module-level side effects.
        import importlib.util
        import pathlib

        # Static check: the source file must not define `def filter_events(`.
        src = (
            pathlib.Path(__file__).resolve().parent.parent
            / "agents" / "application" / "executor.py"
        ).read_text()
        self.assertNotIn(
            "def filter_events(self,",
            src,
            "Executor.filter_events was removed because it called "
            "prompter.filter_events(events) but Prompter.filter_events() "
            "takes no args. Re-introducing it without fixing the prompt "
            "signature would TypeError on first call.",
        )
        # And filter_events_with_rag must still exist (production path).
        self.assertIn("def filter_events_with_rag(", src)


class TestGammaUsesLogger(unittest.TestCase):
    """Drift fix 3: gamma.py must not use ``print()`` — money-handling
    code needs an audit trail. Verified via static source inspection
    so the test runs in stdlib mode without importing gamma."""

    def test_no_print_calls_in_gamma(self) -> None:
        import pathlib
        import re

        src = (
            pathlib.Path(__file__).resolve().parent.parent
            / "agents" / "polymarket" / "gamma.py"
        ).read_text()
        # Strip strings/comments isn't necessary — we want to catch
        # any literal `print(` in active code. False positives in
        # docstrings are unlikely here.
        offenders = [
            m for m in re.finditer(r"^\s*print\(", src, re.MULTILINE)
        ]
        self.assertEqual(
            offenders,
            [],
            f"gamma.py still has {len(offenders)} print() call(s); use "
            f"logger instead per CLAUDE.md.",
        )

    def test_logger_is_initialized(self) -> None:
        import pathlib

        src = (
            pathlib.Path(__file__).resolve().parent.parent
            / "agents" / "polymarket" / "gamma.py"
        ).read_text()
        self.assertIn("import logging", src)
        self.assertIn("logger = logging.getLogger(__name__)", src)


if __name__ == "__main__":
    unittest.main()
