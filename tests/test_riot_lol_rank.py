"""Unit tests for Riot LoL rank parsing and formatting."""

from __future__ import annotations

import pytest

from riot_lol_rank import (
    _format_entry,
    normalize_riot_api_key,
    parse_rank_tokens,
    resolve_rank_query,
    riot_tokens_after_command,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  abc  ", "abc"),
        ('"RGAPI-deadbeef"', "RGAPI-deadbeef"),
        ("'key'", "key"),
    ],
)
def test_normalize_riot_api_key(raw: str, expected: str) -> None:
    assert normalize_riot_api_key(raw) == expected


@pytest.mark.parametrize(
    ("parts", "command", "word_position", "expected"),
    [
        (["elo"], "elo", "start", []),
        (["elo", "Nick#TAG", "br1"], "elo", "start", ["Nick#TAG", "br1"]),
        (["qual", "o", "elo"], "elo", "start", None),
        (["qual", "o", "elo"], "elo", "anywhere", []),
        (
            ["me", "mostra", "elo", "Nick#TAG", "br1"],
            "elo",
            "anywhere",
            ["Nick#TAG", "br1"],
        ),
        ([], "elo", "anywhere", None),
        (["sem", "comando"], "elo", "anywhere", None),
        (["qual", "o", "elo"], "elo", "bogus", None),
    ],
)
def test_riot_tokens_after_command(
    parts: list[str],
    command: str,
    word_position: str,
    expected: list[str] | None,
) -> None:
    assert riot_tokens_after_command(
        parts, command, word_position=word_position
    ) == expected


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        (["Player#BR1", "br1"], ("Player", "BR1", "br1")),
        (["Two", "Words#TAG", "euw1"], ("Two Words", "TAG", "euw1")),
        (["OnlyOne"], None),
        (["NoHash", "br1"], None),
        (["Bad#Tag", "zz99"], None),
    ],
)
def test_parse_rank_tokens(
    tokens: list[str],
    expected: tuple[str, str, str] | None,
) -> None:
    assert parse_rank_tokens(tokens) == expected


def test_resolve_rank_query_defaults() -> None:
    assert resolve_rank_query(
        [],
        default_riot="BinBin宝贝#CN1",
        default_platform="br1",
    ) == ("BinBin宝贝", "CN1", "br1")


def test_resolve_rank_query_tokens_win() -> None:
    assert resolve_rank_query(
        ["Other#EUW", "euw1"],
        default_riot="BinBin宝贝#CN1",
        default_platform="br1",
    ) == ("Other", "EUW", "euw1")


def test_resolve_rank_query_no_defaults_empty() -> None:
    assert resolve_rank_query([]) is None


def test_format_entry_solo_gold() -> None:
    s = _format_entry(
        {
            "tier": "GOLD",
            "rank": "II",
            "leaguePoints": 40,
            "wins": 12,
            "losses": 10,
        }
    )
    assert "Gold" in s
    assert "II" in s
    assert "40 LP" in s
    assert "12W/10L" in s


def test_format_entry_master() -> None:
    s = _format_entry(
        {
            "tier": "MASTER",
            "rank": "",
            "leaguePoints": 42,
            "wins": 100,
            "losses": 80,
        }
    )
    assert "Master" in s
    assert "42 LP" in s


def _extract_tokens_both(msg: str, prefix: str, cmd: str, word_pos: str) -> list[str] | None:
    """Simula a lógica do bot.py para trigger_mode='both'."""
    parts = msg.split()
    if not parts:
        return None
    # Prefix trigger
    if msg.startswith(prefix) and parts[0][len(prefix):].lower() == cmd:
        return parts[1:]
    # Word trigger
    return riot_tokens_after_command(parts, cmd, word_position=word_pos)


@pytest.mark.parametrize(
    ("msg", "expected"),
    [
        ("!elo Nick#TAG br1", ["Nick#TAG", "br1"]),
        ("elo Nick#TAG br1", ["Nick#TAG", "br1"]),
        ("qual elo ele ta?", ["ele", "ta?"]),
        ("me mostra elo", []),
        ("!elo", []),
        ("elo", []),
        ("random message", None),
    ],
)
def test_both_trigger_mode(msg: str, expected: list[str] | None) -> None:
    assert _extract_tokens_both(msg, prefix="!", cmd="elo", word_pos="anywhere") == expected
