"""Persisted chat activity events per channel for rankings and !sorteio."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FILE_VERSION = 1
_DURATION_RE = re.compile(r"^(\d+)\s*([smh])$", re.IGNORECASE)

# Sufixos após o número (ex.: !sorteio 2min, !sorteio 2 min, !sorteio 90s)
_TIME_SUFFIX_SECONDS: dict[str, int] = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "minuto": 60,
    "minutos": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
    "hora": 3600,
    "horas": 3600,
}

_VALID_TIERS = frozenset({"none", "sub", "vip"})


def normalize_username(username: str) -> str:
    return username.strip().lower()


def normalize_tier(raw: str | None) -> str:
    if not raw:
        return "none"
    t = str(raw).strip().lower()
    return t if t in _VALID_TIERS else "none"


def parse_window_seconds(parts: list[str]) -> int | None:
    """
    Parse duration after the command token.

    Examples: ``!sorteio 120`` (segundos), ``!sorteio 2min`` ou ``!sorteio 2 min`` (minutos),
    ``!sorteio 1h`` (horas).
    """
    if len(parts) < 2:
        return None
    raw = "".join(x.strip() for x in parts[1:]).lower()
    if not raw:
        return None
    if raw.isdigit():
        return max(1, int(raw))
    m = _DURATION_RE.match(raw)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        mult = {"s": 1, "m": 60, "h": 3600}.get(unit, 1)
        return max(1, n * mult)
    m2 = re.match(r"^(\d+)([a-z]+)$", raw)
    if not m2:
        return None
    n = int(m2.group(1))
    suffix = m2.group(2).lower()
    mult = _TIME_SUFFIX_SECONDS.get(suffix)
    if mult is None:
        return None
    return max(1, n * mult)


class ChatActivityStore:
    """In-memory events with JSON persistence (debounced)."""

    def __init__(
        self,
        path: Path,
        *,
        max_retention_seconds: int,
        max_events_per_channel: int,
        debounce_seconds: float,
    ) -> None:
        self.path = path
        self.max_retention_seconds = max(60, max_retention_seconds)
        self.max_events_per_channel = max(1000, max_events_per_channel)
        self.debounce_seconds = max(0.3, debounce_seconds)
        self._channels: dict[str, list[dict[str, Any]]] = {}
        self._save_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    def _trim_retention(self, now: float | None = None) -> None:
        t0 = now if now is not None else time.time()
        cutoff = t0 - self.max_retention_seconds
        for key in list(self._channels):
            evs = self._channels[key]
            self._channels[key] = [e for e in evs if float(e.get("t", 0)) >= cutoff]
            self._trim_cap(key)

    def _trim_cap(self, channel_key: str) -> None:
        evs = self._channels.get(channel_key, [])
        over = len(evs) - self.max_events_per_channel
        if over > 0:
            self._channels[channel_key] = evs[over:]

    async def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = await asyncio.to_thread(self.path.read_text, encoding="utf-8")
            data = json.loads(raw)
        except Exception:
            logger.exception("Failed to load chat activity from %s", self.path)
            return
        if not isinstance(data, dict):
            return
        ch = data.get("channels")
        if not isinstance(ch, dict):
            return
        loaded: dict[str, list[dict[str, Any]]] = {}
        for k, v in ch.items():
            if not isinstance(k, str) or not isinstance(v, list):
                continue
            events: list[dict[str, Any]] = []
            for item in v:
                if not isinstance(item, dict):
                    continue
                try:
                    ts = float(item["t"])
                    u = str(item["u"]).strip().lower()
                except (KeyError, TypeError, ValueError):
                    continue
                if not u:
                    continue
                ev: dict[str, Any] = {"t": ts, "u": u}
                if "tier" in item:
                    ev["tier"] = normalize_tier(str(item.get("tier")))
                events.append(ev)
            loaded[k] = events
        self._channels = loaded
        self._trim_retention()

    async def _flush(self) -> None:
        async with self._lock:
            self._trim_retention()
            payload = {
                "version": FILE_VERSION,
                "channels": self._channels,
            }
            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")

            def _write() -> None:
                tmp.write_text(text, encoding="utf-8")
                tmp.replace(self.path)

            try:
                await asyncio.to_thread(_write)
            except Exception:
                logger.exception("Failed to save chat activity to %s", self.path)

    def _schedule_save(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        if self._save_task and not self._save_task.done():
            self._save_task.cancel()

        async def _debounced() -> None:
            try:
                await asyncio.sleep(self.debounce_seconds)
                await self._flush()
            except asyncio.CancelledError:
                return

        self._save_task = loop.create_task(_debounced())

    async def record(self, channel_key: str, username: str, tier: str = "none") -> None:
        u = normalize_username(username)
        if not u:
            return
        tier_n = normalize_tier(tier)
        now = time.time()
        async with self._lock:
            evs = self._channels.setdefault(channel_key, [])
            evs.append({"t": now, "u": u, "tier": tier_n})
            self._trim_retention(now)
            self._trim_cap(channel_key)
        self._schedule_save()

    async def clear_channel(self, channel_key: str) -> None:
        """Remove all recorded chat events for this channel (persisted)."""
        async with self._lock:
            self._channels[channel_key] = []
        self._schedule_save()

    def _events_for_window(
        self,
        channel_key: str,
        window_seconds: float,
        *,
        session_start_ts: float | None,
    ) -> list[dict[str, Any]]:
        evs = self._channels.get(channel_key, [])
        now = time.time()
        start = now - window_seconds
        if session_start_ts is not None:
            start = max(start, session_start_ts)
        out: list[dict[str, Any]] = []
        for e in evs:
            t = float(e.get("t", 0))
            if t >= start and t <= now:
                out.append(e)
        return out

    def _events_session(self, channel_key: str, session_start_ts: float) -> list[dict[str, Any]]:
        evs = self._channels.get(channel_key, [])
        now = time.time()
        out: list[dict[str, Any]] = []
        for e in evs:
            t = float(e.get("t", 0))
            if t >= session_start_ts and t <= now:
                out.append(e)
        return out

    def events_for_sorteio_scope(
        self,
        channel_key: str,
        window_seconds: float,
        *,
        session_start_ts: float | None,
        use_session_only: bool,
    ) -> list[dict[str, Any]]:
        if use_session_only and session_start_ts is not None:
            return self._events_session(channel_key, session_start_ts)
        return self._events_for_window(
            channel_key, window_seconds, session_start_ts=session_start_ts if use_session_only else None
        )

    def counts_in_window(
        self,
        channel_key: str,
        window_seconds: float,
        *,
        session_start_ts: float | None = None,
    ) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self._events_for_window(
            channel_key, window_seconds, session_start_ts=session_start_ts
        ):
            u = str(e.get("u", ""))
            if u:
                counts[u] = counts.get(u, 0) + 1
        return counts

    def session_counts(
        self,
        channel_key: str,
        session_start_ts: float,
    ) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self._events_session(channel_key, session_start_ts):
            u = str(e.get("u", ""))
            if u:
                counts[u] = counts.get(u, 0) + 1
        return counts

    def get_counts_for_scope(
        self,
        channel_key: str,
        window_seconds: float,
        *,
        session_start_ts: float | None,
        use_session_only: bool,
    ) -> dict[str, int]:
        if use_session_only and session_start_ts is not None:
            return self.session_counts(channel_key, session_start_ts)
        return self.counts_in_window(
            channel_key,
            window_seconds,
            session_start_ts=session_start_ts if use_session_only else None,
        )

    def pick_sorteio_top_messages(
        self,
        channel_key: str,
        window_seconds: float,
        *,
        session_start_ts: float | None,
        use_session_only: bool,
    ) -> tuple[str | None, dict[str, int]]:
        counts = self.get_counts_for_scope(
            channel_key,
            window_seconds,
            session_start_ts=session_start_ts,
            use_session_only=use_session_only,
        )
        if not counts:
            return None, counts
        best = max(counts.values())
        tied = [u for u, c in counts.items() if c == best]
        return random.choice(tied), counts

    def pick_sorteio_weighted(
        self,
        channel_key: str,
        window_seconds: float,
        *,
        session_start_ts: float | None,
        use_session_only: bool,
        multiplier_default: float,
        multiplier_subscriber: float,
        multiplier_vip: float,
    ) -> tuple[str | None, dict[str, float], dict[str, int]]:
        """
        Bilhetes por mensagem conforme tier. Retorna (winner, tickets_por_user, msg_counts).
        """
        events = self.events_for_sorteio_scope(
            channel_key,
            window_seconds,
            session_start_ts=session_start_ts,
            use_session_only=use_session_only,
        )
        md = max(0.0, float(multiplier_default))
        ms = max(0.0, float(multiplier_subscriber))
        mv = max(0.0, float(multiplier_vip))
        tier_mult = {"none": md, "sub": ms, "vip": mv}

        tickets: dict[str, float] = defaultdict(float)
        msg_counts: dict[str, int] = defaultdict(int)

        for e in events:
            u = str(e.get("u", "")).strip().lower()
            if not u:
                continue
            tier = normalize_tier(str(e.get("tier")))
            m = tier_mult.get(tier, md)
            tickets[u] += m
            msg_counts[u] += 1

        if not tickets:
            return None, {}, {}

        total = sum(tickets.values())
        if total <= 0:
            return None, dict(tickets), dict(msg_counts)

        r = random.random() * total
        winner: str | None = None
        for u in sorted(tickets.keys()):
            r -= tickets[u]
            if r < 0:
                winner = u
                break
        if winner is None:
            winner = sorted(tickets.keys())[-1]
        return winner, dict(tickets), dict(msg_counts)

    def pick_sorteio_winner(
        self,
        channel_key: str,
        window_seconds: float,
        *,
        session_start_ts: float | None,
        use_session_only: bool,
        mode: str = "top_messages",
        multiplier_default: float = 1.0,
        multiplier_subscriber: float = 10.0,
        multiplier_vip: float = 10.0,
    ) -> tuple[str | None, dict[str, int], dict[str, Any]]:
        """
        mode: top_messages | weighted

        Retorna (winner, counts_for_display, meta).
        counts_for_display: contagens de mensagens em ambos os modos (para mensagem ao chat).
        meta inclui tickets em modo weighted.
        """
        meta: dict[str, Any] = {"mode": mode}
        counts = self.get_counts_for_scope(
            channel_key,
            window_seconds,
            session_start_ts=session_start_ts,
            use_session_only=use_session_only,
        )

        if mode == "weighted":
            w, tickets, msg_counts = self.pick_sorteio_weighted(
                channel_key,
                window_seconds,
                session_start_ts=session_start_ts,
                use_session_only=use_session_only,
                multiplier_default=multiplier_default,
                multiplier_subscriber=multiplier_subscriber,
                multiplier_vip=multiplier_vip,
            )
            meta["tickets"] = tickets
            meta["message_counts"] = msg_counts
            return w, counts, meta

        win, _ = self.pick_sorteio_top_messages(
            channel_key,
            window_seconds,
            session_start_ts=session_start_ts,
            use_session_only=use_session_only,
        )
        return win, counts, meta


def channel_key_from_bid(broadcaster_user_id: Any, fallback: str = "default") -> str:
    if broadcaster_user_id is None:
        return fallback
    try:
        return str(int(broadcaster_user_id))
    except (TypeError, ValueError):
        return fallback
