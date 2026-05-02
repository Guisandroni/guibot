"""Kick chatbot with commands, timers, optional AI replies, etc.

Loads ``.env`` and ``config.yaml`` from the directory that contains this file
(so you can run ``python bot.py`` from any working directory).

Multiple chats in one process: set ``KICK_CHANNELS=slug1,slug2`` (requires ``KICK_MODE=websocket``).
Moderation (timeouts, deletes) applies only to slugs in ``KICK_MODERATION_CHANNELS`` or
``bot.moderation.channels`` (default: first configured channel). For webhook/hybrid with multiple
channels, run one bot process per ``KICK_CHANNEL`` instead.
"""

from __future__ import annotations

import asyncio
from collections import deque
import logging
import os
from pathlib import Path
import random
import re
import time
from typing import Any, NamedTuple

import yaml
from kickforge_core import KickApp, KickForgeError
from kickforge_core.websocket import PusherClient
from dotenv import load_dotenv

from agent import AgentCooldown, probe_llm, run_agent, set_comment_meme_samples
from chat_activity import (
    ChatActivityStore,
    channel_key_from_bid,
    parse_window_seconds,
)

logger = logging.getLogger(__name__)

if not logging.getLogger().hasHandlers():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

PACKAGE_ROOT = Path(__file__).resolve().parent

URL_PATTERN = re.compile(r"https?://\S+|www\.\S+|\S+\.\S+/\S+", re.IGNORECASE)
# Outbound chat: bare https URLs trigger Kick MAX_SPECIAL_CHARS_ERROR on some messages.
HTTP_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
CHAT_MESSAGE_MAX_BYTES = 380
WEBHOOK_EVENTS = [
    "chat.message.sent",
    "channel.followed",
    "channel.subscription.new",
    "channel.subscription.gifts",
    "kicks.gifted",
    "livestream.status.updated",
]


def _load_config() -> dict[str, Any]:
    cfg_path = PACKAGE_ROOT / "config.yaml"
    try:
        with open(cfg_path, encoding="utf-8") as file:
            return yaml.safe_load(file) or {}
    except FileNotFoundError:
        return {}


def _parse_channel_slugs(kcfg: dict[str, Any]) -> list[str]:
    """
    Channel slug resolution order (first wins):
    1. KICK_CHANNELS (env, comma-separated)
    2. kick.channels (YAML list)
    3. KICK_CHANNEL (env, single) — overrides kick.channel YAML when set
    4. kick.channel (YAML fallback)
    """
    raw = os.getenv("KICK_CHANNELS", "").strip()
    if raw:
        return [s.strip() for s in raw.split(",") if s.strip()]
    raw_list = kcfg.get("channels")
    if isinstance(raw_list, list) and raw_list:
        return [str(x).strip() for x in raw_list if str(x).strip()]
    env_single = os.getenv("KICK_CHANNEL", "").strip()
    if env_single:
        return [env_single]
    single = str(kcfg.get("channel") or "").strip()
    return [single] if single else []


load_dotenv(PACKAGE_ROOT / ".env")

config = _load_config()
kick_cfg = config.get("kick") or {}
webhook_cfg = config.get("webhook") or {}
bot_cfg = config.get("bot") or {}
agent_cfg = config.get("agent") or {}
moderation_cfg = bot_cfg.get("moderation") or {}
comments_cfg = bot_cfg.get("comment_spam") or {}
chat_activity_cfg = bot_cfg.get("chat_activity") or {}


def _kick_chat_poster_type() -> str:
    """Kick POST /public/v1/chat: user sends to broadcaster_user_id; bot posts as bot account."""
    env_raw = os.getenv("KICK_CHAT_POSTER_TYPE", "").strip().lower()
    if env_raw in ("user", "bot"):
        return env_raw
    yaml_raw = str(kick_cfg.get("chat_poster_type") or "user").strip().lower()
    return yaml_raw if yaml_raw in ("user", "bot") else "user"


chat_poster_type = _kick_chat_poster_type()


def _cfg_env(key: str, fallback: str = "") -> str:
    return os.getenv(key, "").strip() or str(kick_cfg.get(key.lower(), fallback)).strip()


client_id = _cfg_env("KICK_CLIENT_ID")
client_secret = _cfg_env("KICK_CLIENT_SECRET")
app_mode = (os.getenv("KICK_MODE", "").strip() or str(kick_cfg.get("mode") or "websocket")).lower()
channel_slugs = _parse_channel_slugs(kick_cfg)
channel = channel_slugs[0] if channel_slugs else ""
host = str(webhook_cfg.get("host") or "0.0.0.0").strip()
port = int(webhook_cfg.get("port", 8420))
webhook_path = str(webhook_cfg.get("path") or "/webhook").strip()
bot_username = os.getenv("KICK_BOT_USERNAME", "").strip().lower()

if not client_id or not client_secret:
    raise RuntimeError(
        "Kick credentials missing. Define KICK_CLIENT_ID and KICK_CLIENT_SECRET in .env "
        "or set them in config.yaml."
    )

app = KickApp(
    client_id=client_id,
    client_secret=client_secret,
    mode=app_mode,
    webhook_path=webhook_path,
)

agent_cooldown = AgentCooldown(float(agent_cfg.get("cooldown_seconds", 20)))
agent_trigger = str(agent_cfg.get("trigger") or "!ask").strip().lower()
command_cooldowns: dict[tuple[str, str], float] = {}
spam_history: dict[int, list[tuple[str, float]]] = {}
timed_tasks: list[asyncio.Task[None]] = []
timers_started = False
comment_messages: list[str] = []
comment_prompt_samples: list[str] = []
# Cache de mensagens enviadas pelo bot para evitar loop quando chat_poster_type=user
# (o Pusher reenvia a mensagem com username do broadcaster, não do bot)
_recent_bot_messages: deque[str] = deque(maxlen=30)


class ChannelSession(NamedTuple):
    slug: str
    broadcaster_id: int
    chatroom_id: int | None


channel_sessions: list[ChannelSession] = []
moderation_broadcaster_ids: set[int] = set()
primary_broadcaster_id: int | None = None
multichannel_pushers: list[PusherClient] = []

chat_activity_store: ChatActivityStore | None = None
chat_session_started_at: float | None = None


def _moderation_slug_list() -> list[str]:
    env_raw = os.getenv("KICK_MODERATION_CHANNELS", "").strip()
    if env_raw:
        return [s.strip().lower() for s in env_raw.split(",") if s.strip()]
    cfg_list = moderation_cfg.get("channels")
    if isinstance(cfg_list, list) and cfg_list:
        return [str(x).strip().lower() for x in cfg_list if str(x).strip()]
    if channel_slugs:
        return [channel_slugs[0].lower()]
    return []


def _warn_env_kick_ids_vs_resolved(slug: str) -> None:
    """Warn when env overrides disagree with Kick API (common cause of POST /chat 404)."""
    resolved_bid = app._broadcaster_id
    env_bid_raw = os.getenv("KICK_BROADCASTER_ID", "").strip()
    if env_bid_raw and resolved_bid is not None:
        try:
            env_bid = int(env_bid_raw)
        except ValueError:
            logger.warning("KICK_BROADCASTER_ID must be an integer, got: %s", env_bid_raw)
        else:
            if env_bid != resolved_bid:
                logger.warning(
                    "KICK_BROADCASTER_ID (%s) does not match API broadcaster_id (%s) for "
                    "channel %s. Remove or fix KICK_BROADCASTER_ID to avoid chat send failures.",
                    env_bid,
                    resolved_bid,
                    slug,
                )
    if os.getenv("KICK_CHATROOM_ID", "").strip():
        logger.warning(
            "KICK_CHATROOM_ID is set. If it belongs to another channel than %s, Pusher may "
            "subscribe to the wrong room and Kick may return 404 on POST /public/v1/chat. "
            "Remove it or run `kickforge auth --channel %s` and refresh ~/.kickforge/tokens.json.",
            slug,
            slug,
        )


def _is_self_message(username: str) -> bool:
    return bool(bot_username) and username.lower() == bot_username


def _extract_agent_prompt(message: str) -> str | None:
    lowered = message.lower().strip()
    if lowered.startswith(agent_trigger):
        return message[len(agent_trigger) :].strip()

    if bot_username:
        mention_patterns = (
            f"{bot_username} ",
            f"{bot_username},",
            f"{bot_username}:",
            f"{bot_username} -",
            f"{bot_username}?",
            f"{bot_username}!",
        )
        for pattern in mention_patterns:
            if lowered.startswith(pattern):
                return message[len(pattern) :].strip()
        if lowered == bot_username:
            return ""

    return None


def _agent_usage_text() -> str:
    if bot_username:
        return f"{bot_username} <pergunta> ou {agent_trigger} <pergunta>"
    return f"{agent_trigger} <pergunta>"


def _remaining_cooldown(command_name: str, username: str, seconds: float) -> float:
    if seconds <= 0:
        return 0.0
    key = (command_name, username.lower())
    remaining = seconds - (time.monotonic() - command_cooldowns.get(key, 0.0))
    return max(0.0, remaining)


def _mark_command_used(command_name: str, username: str) -> None:
    command_cooldowns[(command_name, username.lower())] = time.monotonic()


def _contains_unapproved_link(message: str) -> bool:
    if moderation_cfg.get("links_allowed", False):
        return False
    whitelist = [str(item).lower() for item in moderation_cfg.get("link_whitelist", [])]
    for url in URL_PATTERN.findall(message):
        url_lower = url.lower()
        if not any(domain in url_lower for domain in whitelist):
            return True
    return False


def _has_blocked_word(message: str) -> bool:
    blocked_words = moderation_cfg.get("blocked_words", [])
    lowered = message.lower()
    return any(str(word).lower() in lowered for word in blocked_words)


def _is_privileged(sender: Any) -> bool:
    if not sender:
        return False
    badges = [str(badge).lower() for badge in getattr(sender, "badges", [])]
    return "broadcaster" in badges or "moderator" in badges


def _caps_ratio(message: str) -> float:
    alpha_chars = [char for char in message if char.isalpha()]
    if not alpha_chars:
        return 0.0
    caps = sum(1 for char in alpha_chars if char.isupper())
    return caps / len(alpha_chars) * 100


def _is_spam(sender_id: int, message: str) -> bool:
    window = float(moderation_cfg.get("spam_window_seconds", 30))
    max_identical = int(moderation_cfg.get("spam_max_identical", 3))
    cutoff = time.time() - window
    history = spam_history.setdefault(sender_id, [])
    history[:] = [(m, ts) for m, ts in history if ts > cutoff]
    normalized = message.lower().strip()
    history.append((normalized, time.time()))
    identical_count = sum(1 for m, _ in history if m == normalized)
    return identical_count > max_identical


def _fit_chat_message(message: str, max_bytes: int = CHAT_MESSAGE_MAX_BYTES) -> str:
    message = HTTP_URL_RE.sub("", message)
    message = " ".join(message.split()).strip()
    if len(message.encode("utf-8")) <= max_bytes:
        return message
    trimmed = message
    while trimmed and len((trimmed + "…").encode("utf-8")) > max_bytes:
        trimmed = trimmed[:-1].rstrip()
    return (trimmed + "…") if trimmed else ""


def _comment_spam_max_bytes() -> int:
    raw_mb = comments_cfg.get("max_message_bytes")
    raw_mc = comments_cfg.get("max_chars")
    cap = CHAT_MESSAGE_MAX_BYTES
    if raw_mb is not None and str(raw_mb).strip():
        try:
            cap = min(cap, int(raw_mb))
        except ValueError:
            pass
    elif raw_mc is not None and str(raw_mc).strip():
        try:
            cap = min(cap, int(raw_mc))
        except ValueError:
            pass
    return max(1, cap)


def _comment_min_chars() -> int:
    try:
        v = int(comments_cfg.get("min_chars", 0))
    except (TypeError, ValueError):
        return 0
    return max(0, v)


def _comment_spam_fit(message: str) -> str:
    max_b = _comment_spam_max_bytes()
    fitted = _fit_chat_message(message, max_bytes=max_b)
    if not fitted:
        return ""
    min_c = _comment_min_chars()
    if min_c <= 0 or len(fitted) >= min_c:
        return fitted
    pad = " CHAT"
    while len(fitted) < min_c:
        next_s = (fitted + pad).strip()
        next_fit = _fit_chat_message(next_s, max_bytes=max_b)
        if len(next_fit) <= len(fitted):
            break
        fitted = next_fit
    return fitted


def _is_comment_heading(line: str) -> bool:
    stripped = " ".join(line.split()).strip(" -—–|")
    if len(stripped) < 4:
        return False
    if "http://" in stripped.lower() or "https://" in stripped.lower():
        return False
    has_letter = any(char.isalpha() for char in stripped)
    return has_letter and stripped.upper() == stripped


def _extract_comment_lines(content: str) -> list[str]:
    extracted: list[str] = []
    for raw_line in content.splitlines():
        line = " ".join(raw_line.split()).strip()
        if not line:
            continue
        if _is_comment_heading(line):
            fitted = _comment_spam_fit(line)
            if fitted:
                extracted.append(fitted)
    return extracted


def _load_comment_corpus() -> tuple[list[str], list[str]]:
    directory = str(comments_cfg.get("directory") or "comments").strip()
    sample_limit = int(comments_cfg.get("prompt_samples", 12))
    base_path = Path(directory)
    if not base_path.is_absolute():
        base_path = PACKAGE_ROOT / base_path

    spam_candidates: list[str] = []
    prompt_candidates: list[str] = []
    if not base_path.exists():
        logger.warning("Comment spam directory does not exist: %s", base_path)
        return spam_candidates, prompt_candidates

    for path in sorted(base_path.glob("*.txt")):
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            logger.exception("Failed to read comment file: %s", path)
            continue

        lines = _extract_comment_lines(content)
        prompt_candidates.extend(lines)

        stem = path.stem.strip()
        if stem:
            fitted_stem = _comment_spam_fit(stem)
            if fitted_stem:
                spam_candidates.append(fitted_stem)

        spam_candidates.extend(lines)

    deduped_spam = list(dict.fromkeys(spam_candidates))
    deduped_prompt = list(dict.fromkeys(prompt_candidates))[:sample_limit]
    return deduped_spam, deduped_prompt


async def _safe_say(
    message: str, reply_to: str | None = None, broadcaster_id: int | None = None
) -> None:
    global _recent_bot_messages
    try:
        fitted = _fit_chat_message(message)
        if not fitted:
            return
        target_bid = broadcaster_id if broadcaster_id is not None else primary_broadcaster_id
        if chat_poster_type == "user":
            if not target_bid:
                logger.error("Cannot send chat message — no broadcaster_id (configure KICK_CHANNEL/KICK_CHANNELS).")
                return
        else:
            target_bid = int(target_bid or 0)
        # Registra no cache ANTES de enviar, pra detectar loop próprio
        _recent_bot_messages.append(fitted.lower().strip())
        await app.api.send_message(
            broadcaster_id=target_bid,
            content=fitted,
            reply_to=reply_to,
            poster_type=chat_poster_type,
        )
    except Exception:
        logger.exception("Failed to send chat message")


async def _timeout_user(broadcaster_id: int, user_id: int, duration: int) -> None:
    if user_id <= 0:
        return
    try:
        await app.api.ban_user(broadcaster_id, user_id, duration=duration, reason="automod")
    except Exception:
        logger.exception("Moderation timeout failed user_id=%s", user_id)


async def _delete_message(message_id: str | None) -> None:
    if not message_id:
        return
    try:
        await app.api.delete_message(message_id)
    except Exception:
        logger.exception("Moderation delete failed message_id=%s", message_id)


async def _handle_moderation(event: Any, msg: str, reply_to: str | None, channel_bid: int | None) -> bool:
    """Return True if the chat message was acted on and normal handling should stop."""
    if not channel_bid or channel_bid not in moderation_broadcaster_ids:
        return False
    if _is_privileged(event.sender):
        return False

    sender_id = int(getattr(event.sender, "user_id", 0) or 0)
    username = event.sender.username
    timeout_sec = int(moderation_cfg.get("timeout_duration", 300) or 300)
    warn_tpl = str(moderation_cfg.get("warn_message") or "").strip()

    async def _moderation_public_warn(reason: str) -> None:
        if warn_tpl:
            try:
                msg_out = warn_tpl.format(username=username, reason=reason)
            except (KeyError, ValueError):
                msg_out = f"@{username} {reason}"
        else:
            msg_out = f"@{username} {reason}"
        await _safe_say(msg_out, reply_to=reply_to, broadcaster_id=channel_bid)

    if _has_blocked_word(msg):
        await _delete_message(reply_to)
        await _timeout_user(channel_bid, sender_id, timeout_sec)
        await _moderation_public_warn("mensagem removida (palavra bloqueada).")
        return True

    if _contains_unapproved_link(msg):
        await _delete_message(reply_to)
        await _timeout_user(channel_bid, sender_id, timeout_sec)
        await _moderation_public_warn("Linkzao e esse .")
        return True

    if moderation_cfg.get("repetition_enabled", False) and _is_spam(sender_id, msg):
        await _delete_message(reply_to)
        await _timeout_user(channel_bid, sender_id, timeout_sec)
        await _moderation_public_warn("spam / repetição.")
        return True

    if moderation_cfg.get("caps_warning_enabled", False):
        threshold = float(moderation_cfg.get("caps_threshold_percent", 85) or 85)
        if _caps_ratio(msg) >= threshold and len(msg) >= int(moderation_cfg.get("caps_min_length", 12) or 12):
            await _delete_message(reply_to)
            caps_msg = moderation_cfg.get("caps_warn_message") or (
                f"@{username} Punhetinha? goza no muquinha."
            )
            await _safe_say(str(caps_msg), reply_to=reply_to, broadcaster_id=channel_bid)
            return True

    return False


def _iter_timed_message_pools() -> list[tuple[list[str], float]]:
    """Each YAML item: `messages` + `interval` (random pick) or `message` + `interval` (single-line pool)."""
    raw = bot_cfg.get("timed_messages", [])
    if not isinstance(raw, list):
        return []
    pools: list[tuple[list[str], float]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        interval = float(item.get("interval", 900) or 0)
        if interval <= 0:
            continue
        msgs = item.get("messages")
        if isinstance(msgs, list) and msgs:
            pool = [str(m).strip() for m in msgs if str(m).strip()]
            if pool:
                pools.append((pool, interval))
            continue
        single = str(item.get("message") or "").strip()
        if single:
            pools.append(([single], interval))
    return pools


def _ensure_timers_started() -> None:
    global timers_started, comment_messages, comment_prompt_samples
    if timers_started:
        return

    timed_pools = _iter_timed_message_pools()
    if comments_cfg.get("enabled", True) and not comment_messages and not comment_prompt_samples:
        comment_messages, comment_prompt_samples = _load_comment_corpus()
        set_comment_meme_samples(comment_prompt_samples)

    if not timed_pools and not comment_messages:
        timers_started = True
        return

    async def pooled_timer_loop(pool: list[str], interval: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                line = random.choice(pool)
                await _safe_say(line)
        except asyncio.CancelledError:
            return

    async def comment_timer_loop(interval: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                if not comment_messages:
                    continue
                message = random.choice(comment_messages)
                await _safe_say(message)
        except asyncio.CancelledError:
            return

    for pool, interval in timed_pools:
        timed_tasks.append(asyncio.create_task(pooled_timer_loop(pool, interval)))

    if comment_messages:
        interval = float(comments_cfg.get("interval", 60))
        if interval > 0:
            timed_tasks.append(asyncio.create_task(comment_timer_loop(interval)))

    timers_started = True


@app.on_all()
async def on_any_event(_: Any) -> None:
    _ensure_timers_started()


@app.on("chat.message.sent")
async def on_chat(event: Any) -> None:
    if not event.sender:
        return

    msg = event.message.strip()
    username = event.sender.username
    reply_to = getattr(event, "message_id", None)
    channel_bid = getattr(event, "broadcaster_user_id", None)
    prefix = str(bot_cfg.get("prefix") or "!").strip()

    if not msg or _is_self_message(username):
        return

    # Ignora mensagens que o próprio bot acabou de enviar
    # (chat_poster_type=user faz o Pusher reenviar como broadcaster, não como bot)
    if _recent_bot_messages and msg.lower().strip() in _recent_bot_messages:
        logger.debug("Ignoring own message (recent bot cache): %s", msg[:60])
        return

    if moderation_cfg.get("enabled", True) and await _handle_moderation(
        event, msg, reply_to=reply_to, channel_bid=channel_bid
    ):
        return

    if chat_activity_store and chat_activity_cfg.get("enabled"):
        if _should_count_chat_activity_message(msg, prefix):
            await chat_activity_store.record(
                _resolve_chat_activity_channel_key(channel_bid),
                username,
            )

    if await _handle_chat_activity_commands(
        event,
        msg,
        username,
        reply_to,
        channel_bid,
        prefix=prefix,
    ):
        return

    agent_enabled = agent_cfg.get("enabled", True)
    prompt = _extract_agent_prompt(msg) if agent_enabled else None
    if prompt is not None:
        if not prompt:
            await _safe_say(
                f"@{username} usa: {_agent_usage_text()}",
                reply_to=reply_to,
                broadcaster_id=channel_bid,
            )
            return
        if not agent_cooldown.allow(username.lower()):
            await _safe_say(
                f"@{username} manda o papo seu doente"
                f"{bot_username or 'o bot'}.",
                reply_to=reply_to,
                broadcaster_id=channel_bid,
            )
            return
        try:
            reply = await run_agent(prompt, username, cfg=agent_cfg)
        except Exception:
            logger.exception("Agent failed for %s", username)
            await _safe_say(
                f"@{username} algo correu mal ao falar com o assistente. Tenta mais tarde.",
                reply_to=reply_to,
                broadcaster_id=channel_bid,
            )
            return
        await _safe_say(f"@{username} {reply}", reply_to=reply_to, broadcaster_id=channel_bid)
        return

    if msg.startswith(prefix):
        parts = msg.split()
        cmd = parts[0][len(prefix) :].lower()
        commands = bot_cfg.get("commands", {})
        command_def = commands.get(cmd)
        if command_def:
            cooldown_seconds = float(command_def.get("cooldown", 0))
            remaining = _remaining_cooldown(cmd, username, cooldown_seconds)
            if remaining > 0:
                await _safe_say(
                    f"@{username} espera {int(remaining) + 1}s para usar !{cmd} novamente.",
                    reply_to=reply_to,
                    broadcaster_id=channel_bid,
                )
                return
            _mark_command_used(cmd, username)
            await _safe_say(
                str(command_def.get("response") or ""),
                reply_to=reply_to,
                broadcaster_id=channel_bid,
            )
        return

    if "hello" in msg.lower() or "selam" in msg.lower():
        await _safe_say(
            f"Welcome {username}! Type !schedule for stream times.",
            reply_to=reply_to,
            broadcaster_id=channel_bid,
        )


@app.on("channel.followed")
async def on_follow(event: Any) -> None:
    bid = getattr(event, "broadcaster_user_id", None)
    await _safe_say(
        f"Welcome to the family, {event.follower_username}!",
        broadcaster_id=bid,
    )


@app.on("kicks.gifted")
async def on_gift(event: Any) -> None:
    bid = getattr(event, "broadcaster_user_id", None)
    await _safe_say(
        f"{event.gifter_username} just sent {event.kicks_amount} kicks! Thank you!",
        broadcaster_id=bid,
    )


@app.on("channel.subscription.new")
async def on_sub(event: Any) -> None:
    bid = getattr(event, "broadcaster_user_id", None)
    await _safe_say(
        f"{event.subscriber_username} just subscribed! Welcome to the squad!",
        broadcaster_id=bid,
    )


def _existing_subscription_names(payload: dict[str, Any]) -> set[str]:
    entries = payload.get("data", payload)
    if not isinstance(entries, list):
        return set()
    names: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("event")
        if isinstance(name, str) and name:
            names.add(name)
    return names


def _resolve_chat_activity_channel_key(channel_bid: int | None) -> str:
    fb = str(primary_broadcaster_id) if primary_broadcaster_id else "default"
    return channel_key_from_bid(channel_bid, fb)


def _should_count_chat_activity_message(message: str, prefix: str) -> bool:
    if chat_activity_cfg.get("count_command_messages", True):
        return True
    return not message.startswith(prefix)


async def _init_chat_activity_store() -> None:
    global chat_activity_store, chat_session_started_at
    chat_activity_store = None
    chat_session_started_at = None
    if not chat_activity_cfg.get("enabled", False):
        return
    path_raw = str(chat_activity_cfg.get("path") or "data/chat_activity.json").strip()
    path = Path(path_raw)
    if not path.is_absolute():
        path = PACKAGE_ROOT / path
    store = ChatActivityStore(
        path,
        max_retention_seconds=int(chat_activity_cfg.get("max_retention_seconds", 604800)),
        max_events_per_channel=int(chat_activity_cfg.get("max_events_per_channel", 50_000)),
        debounce_seconds=float(chat_activity_cfg.get("debounce_seconds", 1.5)),
    )
    try:
        await store.load()
    except Exception:
        logger.exception("Chat activity load failed; stats disabled.")
        return
    chat_activity_store = store
    chat_session_started_at = time.time()
    logger.info(
        "Chat activity enabled (file=%s, session_ts=%s)",
        path,
        chat_session_started_at,
    )


async def _handle_chat_activity_commands(
    event: Any,
    msg: str,
    username: str,
    reply_to: str | None,
    channel_bid: int | None,
    *,
    prefix: str,
) -> bool:
    """Handle !sorteio / !topchat / !clear. Returns True if handled."""
    if not chat_activity_store or not chat_activity_cfg.get("enabled"):
        return False
    if not msg.startswith(prefix):
        return False
    parts = msg.split()
    if not parts:
        return False
    cmd = parts[0][len(prefix) :].lower()
    if cmd not in ("sorteio", "topchat", "clear"):
        return False

    ck = _resolve_chat_activity_channel_key(channel_bid)

    if cmd == "clear":
        if chat_activity_cfg.get("clear_mods_only", True) and not _is_privileged(
            event.sender
        ):
            await _safe_say(
                f"@{username} só mods/streamer podem usar !clear.",
                reply_to=reply_to,
                broadcaster_id=channel_bid,
            )
            return True

        cooldown_seconds = float(chat_activity_cfg.get("cooldown_clear", 10))
        remaining = _remaining_cooldown("clear", username, cooldown_seconds)
        if remaining > 0:
            await _safe_say(
                f"@{username} espera {int(remaining) + 1}s para !clear.",
                reply_to=reply_to,
                broadcaster_id=channel_bid,
            )
            return True
        _mark_command_used("clear", username)

        await chat_activity_store.clear_channel(ck)
        await _safe_say(
            _fit_chat_message(
                "Stats deste canal limpas (mensagens guardadas pelo bot). "
                "Novas msgs voltam a contar daqui."
            ),
            reply_to=reply_to,
            broadcaster_id=channel_bid,
        )
        return True

    if cmd == "sorteio" and chat_activity_cfg.get(
        "sorteio_mods_only"
    ) and not _is_privileged(event.sender):
        await _safe_say(
            f"@{username} só mods/streamer podem usar !sorteio.",
            reply_to=reply_to,
            broadcaster_id=channel_bid,
        )
        return True

    cooldown_key = "sorteio" if cmd == "sorteio" else "topchat"
    cooldown_seconds = float(
        chat_activity_cfg.get(
            "cooldown_sorteio" if cmd == "sorteio" else "cooldown_topchat",
            60 if cmd == "sorteio" else 30,
        )
    )
    remaining = _remaining_cooldown(cooldown_key, username, cooldown_seconds)
    if remaining > 0:
        await _safe_say(
            f"@{username} espera {int(remaining) + 1}s para !{cmd}.",
            reply_to=reply_to,
            broadcaster_id=channel_bid,
        )
        return True
    _mark_command_used(cooldown_key, username)

    sess_ts = chat_session_started_at

    parsed_window = parse_window_seconds(parts)
    default_session = bool(chat_activity_cfg.get("default_sorteio_use_session", True))
    default_win = float(chat_activity_cfg.get("default_sorteio_window_seconds", 3600))

    if parsed_window is not None:
        use_session_only = False
        window_sec = float(parsed_window)
    else:
        use_session_only = default_session
        window_sec = default_win

    if cmd == "sorteio":
        winner, counts = chat_activity_store.pick_sorteio_winner(
            ck,
            window_sec,
            session_start_ts=sess_ts,
            use_session_only=use_session_only,
        )
        if winner is None:
            out = "Ninguém na disputa (sem msgs no período)."
        else:
            wcount = counts.get(winner, 0)
            best = max(counts.values())
            n_tied = sum(1 for c in counts.values() if c == best)
            tie_note = f" Empate no topo: {n_tied}." if n_tied > 1 else ""
            out = f"Sorteio: @{winner} — {wcount} msgs.{tie_note}"
        await _safe_say(_fit_chat_message(out), reply_to=reply_to, broadcaster_id=channel_bid)
        return True

    counts = chat_activity_store.get_counts_for_scope(
        ck,
        window_sec,
        session_start_ts=sess_ts,
        use_session_only=use_session_only,
    )
    limit = int(chat_activity_cfg.get("topchat_limit", 5))
    ranked = sorted(counts.items(), key=lambda x: (-x[1], x[0]))[: max(1, limit)]
    nu = len(counts)
    if not ranked:
        out = "Top chat: ninguém no período."
    else:
        bits = [f"{u}({c})" for u, c in ranked]
        out = f"Top: {' · '.join(bits)} | {nu} users."
    await _safe_say(_fit_chat_message(out), reply_to=reply_to, broadcaster_id=channel_bid)
    return True


async def _resolve_channel_session(slug: str) -> ChannelSession:
    channel_data = await app.api.get_channel(slug)
    channels = channel_data.get("data", [channel_data])
    if isinstance(channels, list) and channels:
        entry = channels[0]
    elif isinstance(channels, dict):
        entry = channels
    else:
        entry = {}
    broadcaster_id = int(entry.get("broadcaster_user_id") or 0)
    chatroom_id = await app.api.get_chatroom_id(slug, channel_data=channel_data)
    return ChannelSession(slug=slug, broadcaster_id=broadcaster_id, chatroom_id=chatroom_id)


async def prepare_app() -> None:
    global channel_sessions, primary_broadcaster_id, moderation_broadcaster_ids
    if not channel_slugs:
        return

    if len(channel_slugs) > 1 and app_mode in {"webhook", "hybrid"}:
        raise RuntimeError(
            "Multiple channels (KICK_CHANNELS) only support KICK_MODE=websocket. "
            "Run a separate bot process per channel if you need webhook or hybrid mode."
        )

    try:
        if len(channel_slugs) == 1:
            await app.connect(channel_slugs[0])
            primary_broadcaster_id = app._broadcaster_id
            channel_sessions = [
                ChannelSession(
                    channel_slugs[0],
                    int(primary_broadcaster_id or 0),
                    app._chatroom_id,
                )
            ]
            _warn_env_kick_ids_vs_resolved(channel_slugs[0])
            logger.info(
                "Kick channel resolved: slug=%s broadcaster_id=%s chatroom_id_after_connect=%s",
                channel_slugs[0],
                primary_broadcaster_id,
                app._chatroom_id,
            )
            logger.info("Kick chat send poster_type=%s", chat_poster_type)
        else:
            channel_sessions = []
            for slug in channel_slugs:
                channel_sessions.append(await _resolve_channel_session(slug))
            primary_broadcaster_id = channel_sessions[0].broadcaster_id or None
            app._broadcaster_id = primary_broadcaster_id
            app._chatroom_id = channel_sessions[0].chatroom_id

            logger.info(
                "Kick channels resolved: %s",
                "; ".join(
                    f"{s.slug} broadcaster_id={s.broadcaster_id} chatroom_id={s.chatroom_id}"
                    for s in channel_sessions
                ),
            )
            logger.info("Kick chat send poster_type=%s", chat_poster_type)

        moderation_broadcaster_ids = set()
        mod_slugs = {s.strip().lower() for s in _moderation_slug_list()}
        for sess in channel_sessions:
            if sess.slug.lower() in mod_slugs and sess.broadcaster_id:
                moderation_broadcaster_ids.add(sess.broadcaster_id)
        if mod_slugs:
            logger.info(
                "Moderation enabled for broadcaster_id(s): %s (slugs: %s)",
                sorted(moderation_broadcaster_ids),
                ", ".join(sorted(mod_slugs)),
            )

        if agent_cfg.get("enabled"):
            ok, probe_msg = await probe_llm(agent_cfg)
            if ok:
                logger.info("LLM probe: %s", probe_msg)
            else:
                logger.warning(
                    "LLM probe falhou — comandos do agente podem falhar: %s",
                    probe_msg,
                )

        if app_mode in {"webhook", "hybrid"}:
            existing = _existing_subscription_names(await app.api.get_subscriptions())
            missing = [name for name in WEBHOOK_EVENTS if name not in existing]
            if missing:
                await app.subscribe(missing)
                logger.info("Created webhook subscriptions: %s", ", ".join(missing))
            else:
                logger.info("Webhook subscriptions already present")

        await _init_chat_activity_store()
    finally:
        await app.api.close()


def run_multichannel_websocket(host: str, port: int) -> None:
    import signal

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app._shutdown_event = asyncio.Event()

    async def bootstrap() -> None:
        for sess in channel_sessions:
            if not sess.chatroom_id:
                slug = sess.slug
                raise KickForgeError(
                    f"Could not resolve chatroom_id for '{slug}'. "
                    f"Run `kickforge auth --channel {slug}` or set KICK_CHATROOM_ID."
                )
        if primary_broadcaster_id:
            app._broadcaster_id = primary_broadcaster_id
        if channel_sessions:
            app._chatroom_id = channel_sessions[0].chatroom_id

    async def serve() -> None:
        global multichannel_pushers
        multichannel_pushers = []
        tasks: list[asyncio.Task[None]] = []
        for sess in channel_sessions:
            assert sess.chatroom_id is not None
            client = PusherClient(
                bus=app.bus,
                chatroom_id=sess.chatroom_id,
                broadcaster_user_id=sess.broadcaster_id,
            )
            multichannel_pushers.append(client)
            tasks.append(asyncio.create_task(client.run()))
        await asyncio.gather(*tasks, return_exceptions=True)

    async def shutdown() -> None:
        logger.info("Shutting down multi-channel WebSocket clients...")
        for client in multichannel_pushers:
            await client.stop()
        try:
            await app.api.close()
        except Exception:
            logger.exception("Error during API client shutdown")
        if app._shutdown_event:
            app._shutdown_event.set()

    def signal_handler() -> None:
        loop.create_task(shutdown())

    try:
        loop.add_signal_handler(signal.SIGINT, signal_handler)
        loop.add_signal_handler(signal.SIGTERM, signal_handler)
    except NotImplementedError:
        pass

    logger.info(
        "Starting multi-channel WebSocket mode (%d channels: %s)",
        len(channel_sessions),
        ", ".join(s.slug for s in channel_sessions),
    )
    try:
        loop.run_until_complete(bootstrap())
        loop.run_until_complete(serve())
    except KeyboardInterrupt:
        loop.run_until_complete(shutdown())
    except Exception:
        logger.exception("Multi-channel run crashed")
        loop.run_until_complete(shutdown())
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass
        loop.close()
        logger.info("Multi-channel bot stopped.")


_pusher_timer_bootstrap_installed = False


def _install_pusher_timer_bootstrap() -> None:
    """Start periodic timers as soon as Pusher connects (no need to wait for a chat event)."""
    global _pusher_timer_bootstrap_installed
    if _pusher_timer_bootstrap_installed:
        return
    _orig_run = PusherClient.run

    async def _run_with_timers(self: PusherClient) -> None:
        _ensure_timers_started()
        await _orig_run(self)

    PusherClient.run = _run_with_timers  # type: ignore[method-assign, assignment]
    _pusher_timer_bootstrap_installed = True


if __name__ == "__main__":
    if not channel_slugs:
        raise RuntimeError(
            "No channel configured. Set KICK_CHANNEL, KICK_CHANNELS, or kick.channels in config.yaml."
        )
    asyncio.run(prepare_app())
    _install_pusher_timer_bootstrap()
    if len(channel_slugs) > 1:
        run_multichannel_websocket(host=host, port=port)
    else:
        app.run(channel=channel_slugs[0], host=host, port=port)
