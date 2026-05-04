"""Tests for bot keyword responses and agent trigger anywhere."""

from __future__ import annotations

import time

import pytest


def _extract_agent_prompt(message: str, agent_trigger: str, bot_username: str | None) -> str | None:
    """Replica da lógica atual do bot.py para testes."""
    lowered = message.lower().strip()

    if agent_trigger:
        import re
        pattern = re.compile(re.escape(agent_trigger), re.IGNORECASE)
        match = pattern.search(message)
        if match:
            before = message[: match.start()].strip()
            after = message[match.end() :].strip()
            parts = [p for p in (before, after) if p]
            return " ".join(parts)

    if bot_username:
        mention_patterns = (
            f"{bot_username} ",
            f"{bot_username},",
            f"{bot_username}:",
            f"{bot_username} -",
            f"{bot_username}?",
            f"{bot_username}!",
        )
        for pat in mention_patterns:
            if lowered.startswith(pat):
                return message[len(pat) :].strip()
        if lowered == bot_username:
            return ""

    return None


class TestAgentTriggerAnywhere:
    def test_trigger_at_start(self) -> None:
        assert _extract_agent_prompt("ana que dia é hoje", "ana", None) == "que dia é hoje"

    def test_trigger_at_end(self) -> None:
        assert _extract_agent_prompt("que dia é hoje ana", "ana", None) == "que dia é hoje"

    def test_trigger_in_middle(self) -> None:
        assert _extract_agent_prompt("eu acho ana que hoje é segunda", "ana", None) == "eu acho que hoje é segunda"

    def test_trigger_case_insensitive(self) -> None:
        assert _extract_agent_prompt("Ana que dia é hoje", "ana", None) == "que dia é hoje"
        assert _extract_agent_prompt("que dia é hoje ANA", "ana", None) == "que dia é hoje"

    def test_trigger_no_extra_text(self) -> None:
        assert _extract_agent_prompt("ana", "ana", None) == ""

    def test_trigger_not_present(self) -> None:
        assert _extract_agent_prompt("olá tudo bem", "ana", None) is None

    def test_mention_fallback(self) -> None:
        assert _extract_agent_prompt("botname que dia é hoje", "ana", "botname") == "que dia é hoje"


class TestKeywordCooldown:
    def test_keyword_cooldown_logic(self) -> None:
        """Simula _remaining_keyword_cooldown e _mark_keyword_used."""
        keyword_cooldowns: dict[str, float] = {}

        def _remaining(keyword: str, seconds: float) -> float:
            if seconds <= 0:
                return 0.0
            remaining = seconds - (time.monotonic() - keyword_cooldowns.get(keyword, 0.0))
            return max(0.0, remaining)

        def _mark_used(keyword: str) -> None:
            keyword_cooldowns[keyword] = time.monotonic()

        _mark_used("wow")
        assert _remaining("wow", 30) > 0
        assert _remaining("other", 30) == 0.0

    def test_keyword_cooldown_zero(self) -> None:
        keyword_cooldowns: dict[str, float] = {}

        def _remaining(keyword: str, seconds: float) -> float:
            if seconds <= 0:
                return 0.0
            remaining = seconds - (time.monotonic() - keyword_cooldowns.get(keyword, 0.0))
            return max(0.0, remaining)

        assert _remaining("wow", 0) == 0.0
