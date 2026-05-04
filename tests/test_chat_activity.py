"""Tests for chat activity store, especially sorteio scope/timestamp handling."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from chat_activity import ChatActivityStore, parse_window_seconds


@pytest.fixture
def store(tmp_path: Path) -> ChatActivityStore:
    return ChatActivityStore(
        path=tmp_path / "chat_activity.json",
        max_retention_seconds=604800,
        max_events_per_channel=50000,
        debounce_seconds=0.1,
    )


class TestParseWindowSeconds:
    def test_seconds_only(self) -> None:
        assert parse_window_seconds(["!sorteio", "120"]) == 120

    def test_minutes_suffix(self) -> None:
        assert parse_window_seconds(["!sorteio", "2min"]) == 120

    def test_hours_suffix(self) -> None:
        assert parse_window_seconds(["!sorteio", "1h"]) == 3600

    def test_no_duration(self) -> None:
        assert parse_window_seconds(["!sorteio"]) is None


class TestSorteioScopeTimestamp:
    """Ensure explicit window (!sorteio 15m) ignores session_start_ts."""

    def test_explicit_window_ignores_session_start(self, store: ChatActivityStore) -> None:
        now = time.time()
        channel = "test"
        # Event 10 min ago
        store._channels[channel] = [{"t": now - 600, "u": "alice"}]

        # Session started 2 min ago — older events should be included for a 15m window
        session_start = now - 120

        events = store.events_for_sorteio_scope(
            channel,
            window_seconds=900,  # 15 min
            session_start_ts=session_start,
            use_session_only=False,
        )
        assert len(events) == 1
        assert events[0]["u"] == "alice"

    def test_session_only_uses_session_start(self, store: ChatActivityStore) -> None:
        now = time.time()
        channel = "test"
        store._channels[channel] = [
            {"t": now - 600, "u": "alice"},   # 10 min ago
            {"t": now - 60, "u": "bob"},      # 1 min ago
        ]
        session_start = now - 120  # 2 min ago

        events = store.events_for_sorteio_scope(
            channel,
            window_seconds=3600,
            session_start_ts=session_start,
            use_session_only=True,
        )
        assert len(events) == 1
        assert events[0]["u"] == "bob"

    def test_counts_explicit_window_ignores_session_start(self, store: ChatActivityStore) -> None:
        now = time.time()
        channel = "test"
        store._channels[channel] = [{"t": now - 600, "u": "alice"}]
        session_start = now - 120

        counts = store.get_counts_for_scope(
            channel,
            window_seconds=900,
            session_start_ts=session_start,
            use_session_only=False,
        )
        assert counts == {"alice": 1}

    def test_counts_session_only_uses_session_start(self, store: ChatActivityStore) -> None:
        now = time.time()
        channel = "test"
        store._channels[channel] = [
            {"t": now - 600, "u": "alice"},
            {"t": now - 60, "u": "bob"},
        ]
        session_start = now - 120

        counts = store.get_counts_for_scope(
            channel,
            window_seconds=3600,
            session_start_ts=session_start,
            use_session_only=True,
        )
        assert counts == {"bob": 1}
