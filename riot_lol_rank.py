"""League of Legends ranked info via Riot API (Riot ID + platform).

Used by the configured chat command (default ``!elo``) and optional ``$(leagueoflegends Name#Tag platform)`` macro expansion.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

# Platform routing host (lowercase) -> regional cluster for Account v1
_PLATFORM_TO_REGIONAL: dict[str, str] = {
    "br1": "americas",
    "la1": "americas",
    "la2": "americas",
    "na1": "americas",
    "oc1": "americas",
    "jp1": "asia",
    "kr": "asia",
    "eun1": "europe",
    "euw1": "europe",
    "tr1": "europe",
    "ru": "europe",
    "sg2": "sea",
    "tw2": "sea",
    "vn2": "sea",
    "ph2": "sea",
    "th2": "sea",
    "me1": "europe",
}

_SOLO_QUEUE = "RANKED_SOLO_5x5"

_LEAGUE_MACRO_RE = re.compile(
    r"\$\(\s*leagueoflegends\s+([^)]+)\)",
    re.IGNORECASE,
)

_client: httpx.AsyncClient | None = None
_cache: dict[tuple[str, str, str], tuple[float, str]] = {}
_CACHE_TTL = 90.0


def normalize_riot_api_key(raw: str | None) -> str:
    """Strip whitespace and optional surrounding quotes from .env paste mistakes."""
    if not raw:
        return ""
    s = str(raw).strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    return s


def known_platforms() -> frozenset[str]:
    return frozenset(_PLATFORM_TO_REGIONAL)


def regional_host(platform: str) -> str | None:
    r = _PLATFORM_TO_REGIONAL.get(platform.lower().strip())
    return f"{r}.api.riotgames.com" if r else None


def riot_tokens_after_command(
    parts: list[str],
    command: str,
    *,
    word_position: str,
) -> list[str] | None:
    """
    Tokens after the rank command word when ``trigger_mode`` is ``word``.

    - ``start``: first token must equal ``command`` (case-insensitive).
    - ``anywhere``: first token equal to ``command`` anywhere in ``parts``; return tokens after it.
    """
    cmd = command.strip().lower()
    wp = (word_position or "start").strip().lower()
    if wp not in ("start", "anywhere"):
        wp = "start"
    if wp == "anywhere":
        for i, tok in enumerate(parts):
            if tok.lower() == cmd:
                return parts[i + 1 :]
        return None
    if parts and parts[0].lower() == cmd:
        return parts[1:]
    return None


def parse_rank_tokens(tokens: list[str]) -> tuple[str, str, str] | None:
    """
    Parse ``Nome#Tag plataforma`` from tokens after the command (sem o prefixo ``!elo`` / comando configurado).

    Last token = platform (e.g. ``br1``). Everything before, joined with spaces,
    must contain ``#`` for gameName#tagLine.
    """
    if len(tokens) < 2:
        return None
    platform = tokens[-1].strip().lower()
    if platform not in _PLATFORM_TO_REGIONAL:
        return None
    riot_part = " ".join(tokens[:-1]).strip()
    if "#" not in riot_part:
        return None
    game_name, tag_line = riot_part.split("#", 1)
    game_name = game_name.strip()
    tag_line = tag_line.strip()
    if not game_name or not tag_line:
        return None
    return (game_name, tag_line, platform)


def resolve_rank_query(
    tokens: list[str],
    *,
    default_riot: str | None = None,
    default_platform: str | None = None,
) -> tuple[str, str, str] | None:
    """
    If ``tokens`` form ``Nome#Tag plataforma``, return that triple.
    If ``tokens`` is empty and defaults are set (``default_riot`` like ``Nome#Tag`` +
    ``default_platform`` e.g. ``br1``), return parsed defaults.
    """
    if tokens:
        return parse_rank_tokens(tokens)
    raw_riot = (default_riot or "").strip()
    plat = (default_platform or "").strip().lower()
    if not raw_riot or not plat:
        return None
    if plat not in _PLATFORM_TO_REGIONAL:
        return None
    if "#" not in raw_riot:
        return None
    game_name, tag_line = raw_riot.split("#", 1)
    game_name = game_name.strip()
    tag_line = tag_line.strip()
    if not game_name or not tag_line:
        return None
    return (game_name, tag_line, plat)


def _roman_div(rank_div: str) -> str:
    return {"I": "I", "II": "II", "III": "III", "IV": "IV"}.get(
        rank_div.strip().upper(), rank_div.strip()
    )


def _format_entry(entry: dict[str, Any]) -> str:
    tier = str(entry.get("tier") or "").upper()
    rank_div = str(entry.get("rank") or "").strip()
    lp = entry.get("leaguePoints")
    wins = entry.get("wins")
    losses = entry.get("losses")
    parts: list[str] = []
    if tier in ("CHALLENGER", "GRANDMASTER", "MASTER"):
        parts.append(tier.title())
        if isinstance(lp, int):
            parts.append(f"{lp} LP")
    else:
        parts.append(tier.title() if tier else "?")
        if rank_div:
            parts.append(_roman_div(rank_div))
        if isinstance(lp, int):
            parts.append(f"{lp} LP")
    wl = ""
    if isinstance(wins, int) and isinstance(losses, int):
        wl = f" ({wins}W/{losses}L)"
    return " ".join(parts) + wl


async def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=15.0)
    return _client


def _cache_get(key: tuple[str, str, str]) -> str | None:
    row = _cache.get(key)
    if not row:
        return None
    ts, val = row
    if time.monotonic() - ts > _CACHE_TTL:
        del _cache[key]
        return None
    return val


def _cache_set(key: tuple[str, str, str], value: str) -> None:
    _cache[key] = (time.monotonic(), value)


async def fetch_solo_rank_line(
    game_name: str,
    tag_line: str,
    platform: str,
    *,
    api_key: str | None = None,
) -> str:
    """
    Returns a single-line human summary or an error string (no exceptions for API errors).
    """
    key_env = normalize_riot_api_key(api_key or os.getenv("RIOT_API_KEY", ""))
    if not key_env:
        return "Riot API: define RIOT_API_KEY no .env."

    plat = platform.lower().strip()
    reg_host = regional_host(plat)
    if not reg_host:
        return f"Plataforma inválida: {platform}. Usa br1, euw1, na1, …"

    cache_key = (game_name.lower(), tag_line.lower(), plat)
    hit = _cache_get(cache_key)
    if hit is not None:
        return hit

    plat_host = f"{plat}.api.riotgames.com"
    enc_game = quote(game_name, safe="")
    enc_tag = quote(tag_line, safe="")
    headers = {"X-Riot-Token": key_env}

    account_url = (
        f"https://{reg_host}/riot/account/v1/accounts/by-riot-id/{enc_game}/{enc_tag}"
    )
    client = await _http()
    try:
        ar = await client.get(account_url, headers=headers)
    except httpx.RequestError as e:
        logger.warning("Riot account request failed: %s", e)
        line = "Riot: rede indisponível."
        _cache_set(cache_key, line)
        return line

    if ar.status_code == 404:
        line = "Conta Riot não encontrada (nome#tag ou região?)."
        _cache_set(cache_key, line)
        return line
    if ar.status_code == 401:
        return (
            "Riot API 401: chave inválida ou expirada. Chaves de desenvolvimento "
            "renovam em ~24h no portal (developer.riotgames.com). Confirma RIOT_API_KEY "
            "no .env sem aspas nem espaços a mais."
        )
    if ar.status_code == 403:
        return "Riot API recusou (chave sem permissão ou conta suspensa?)."
    if ar.status_code == 429:
        return "Riot API rate limit — tenta daqui a pouco."
    if ar.status_code != 200:
        logger.warning("Riot account HTTP %s: %s", ar.status_code, ar.text[:200])
        return f"Riot account erro HTTP {ar.status_code}."

    try:
        account = ar.json()
    except Exception:
        return "Riot: resposta inválida (account)."

    puuid = account.get("puuid")
    if not isinstance(puuid, str) or not puuid:
        return "Riot: sem puuid na resposta."

    league_url = f"https://{plat_host}/lol/league/v4/entries/by-puuid/{puuid}"
    try:
        lr = await client.get(league_url, headers=headers)
    except httpx.RequestError as e:
        logger.warning("Riot league request failed: %s", e)
        return "Riot: rede indisponível (league)."

    if lr.status_code == 404:
        line = f"{game_name}#{tag_line} — sem ranked (Solo/Duo)."
        _cache_set(cache_key, line)
        return line
    if lr.status_code != 200:
        logger.warning("Riot league HTTP %s: %s", lr.status_code, lr.text[:200])
        return f"Riot league erro HTTP {lr.status_code}."

    try:
        entries: list[Any] = lr.json()
    except Exception:
        return "Riot: resposta inválida (league)."

    if not isinstance(entries, list):
        return "Riot: formato league inesperado."

    solo: dict[str, Any] | None = None
    for e in entries:
        if isinstance(e, dict) and e.get("queueType") == _SOLO_QUEUE:
            solo = e
            break

    if solo is None:
        line = f"{game_name}#{tag_line} — sem Solo/Duo ranqueado."
        _cache_set(cache_key, line)
        return line

    body = _format_entry(solo)
    line = f"{game_name}#{tag_line} [{plat.upper()}] Solo/Duo: {body}"
    _cache_set(cache_key, line)
    return line


def _parse_macro_inner(inner: str) -> tuple[str, str, str] | None:
    """Inner part: ``NomeInvocador#Tag br1`` (last token = platform)."""
    parts = inner.split()
    return parse_rank_tokens(parts)


async def expand_leagueoflegends_macros(text: str) -> str:
    """
    Replace ``$(leagueoflegends Nome#Tag br1)`` (case-insensitive keyword) with rank text.
    If API key missing or lookup fails, uses a short fallback so timers keep working.
    """
    if not _LEAGUE_MACRO_RE.search(text):
        return text

    api_key = os.getenv("RIOT_API_KEY", "").strip()
    if not api_key:

        def _no_key(_: re.Match[str]) -> str:
            return "[LoL: RIOT_API_KEY]"

        return _LEAGUE_MACRO_RE.sub(_no_key, text)

    pos = 0
    out: list[str] = []

    for m in _LEAGUE_MACRO_RE.finditer(text):
        out.append(text[pos : m.start()])
        inner = (m.group(1) or "").strip()
        parsed = _parse_macro_inner(inner)
        if not parsed:
            repl = "[LoL: formato Nome#Tag br1]"
        else:
            g, t, p = parsed
            repl = await fetch_solo_rank_line(g, t, p, api_key=api_key)
        out.append(repl)
        pos = m.end()

    out.append(text[pos:])
    return "".join(out)
