"""Tests for agent web search helper (no live Tavily calls)."""

from __future__ import annotations

import pytest

from agent_web_search import _truncate_block, web_search_enabled


def test_truncate_block() -> None:
    s = "a" * 100
    out = _truncate_block(s, 20)
    assert len(out) == 20
    assert out.endswith("…")


def test_web_search_enabled_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "env-key")
    assert web_search_enabled({"web_search": {"enabled": True}}) is True


def test_web_search_enabled_yaml_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert web_search_enabled({"web_search": {"enabled": True, "api_key": "yaml-key"}}) is True


def test_web_search_disabled_or_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert web_search_enabled({}) is False
    assert web_search_enabled({"web_search": {"enabled": False}}) is False
    assert web_search_enabled({"web_search": {"enabled": True}}) is False
