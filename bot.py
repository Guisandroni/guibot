"""Kick chatbot with commands, moderation, timers, and optional AI replies."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import random
import re
import time
from typing import Any

import yaml
from kickforge_core import KickApp
from dotenv import load_dotenv

from agent import AgentCooldown, run_agent, set_comment_meme_samples

logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(r"https?://\S+|www\.\S+|\S+\.\S+/\S+", re.IGNORECASE)
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
    with open("config.yaml", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


config = _load_config()
kick_cfg = config.get("kick") or {}
webhook_cfg = config.get("webhook") or {}
bot_cfg = config.get("bot") or {}
agent_cfg = config.get("agent") or {}
moderation_cfg = bot_cfg.get("moderation") or {}
comments_cfg = bot_cfg.get("comment_spam") or {}

load_dotenv()


def _cfg_env(key: str, fallback: str = "") -> str:
    return os.getenv(key, "").strip() or str(kick_cfg.get(key.lower(), fallback)).strip()


client_id = _cfg_env("KICK_CLIENT_ID")
client_secret = _cfg_env("KICK_CLIENT_SECRET")
app_mode = (os.getenv("KICK_MODE", "").strip() or str(kick_cfg.get("mode") or "websocket")).lower()
channel = str(kick_cfg.get("channel") or os.getenv("KICK_CHANNEL", "")).strip()
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


def _caps_ratio(message: str) -> float:
    alpha_chars = [char for char in message if char.isalpha()]
    if not alpha_chars:
        return 0.0
    caps = sum(1 for char in alpha_chars if char.isupper())
    return caps / len(alpha_chars) * 100


def _contains_unapproved_link(message: str) -> bool:
    if moderation_cfg.get("links_allowed", False):
        return False
    whitelist = [str(item).lower() for item in moderation_cfg.get("link_whitelist", [])]
    for url in URL_PATTERN.findall(message):
        url_lower = url.lower()
        if not any(domain in url_lower for domain in whitelist):
            return True
    return False


def _fit_chat_message(message: str, max_bytes: int = CHAT_MESSAGE_MAX_BYTES) -> str:
    message = " ".join(message.split()).strip()
    if len(message.encode("utf-8")) <= max_bytes:
        return message
    trimmed = message
    while trimmed and len((trimmed + "…").encode("utf-8")) > max_bytes:
        trimmed = trimmed[:-1].rstrip()
    return (trimmed + "…") if trimmed else ""


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
            fitted = _fit_chat_message(line)
            if fitted:
                extracted.append(fitted)
    return extracted


def _load_comment_corpus() -> tuple[list[str], list[str]]:
    directory = str(comments_cfg.get("directory") or "comments").strip()
    sample_limit = int(comments_cfg.get("prompt_samples", 12))
    base_path = Path(directory)
    if not base_path.is_absolute():
        base_path = Path.cwd() / base_path

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
            fitted_stem = _fit_chat_message(stem)
            if fitted_stem:
                spam_candidates.append(fitted_stem)

        spam_candidates.extend(lines)

    deduped_spam = list(dict.fromkeys(spam_candidates))
    deduped_prompt = list(dict.fromkeys(prompt_candidates))[:sample_limit]
    return deduped_spam, deduped_prompt


def _has_blocked_word(message: str) -> bool:
    blocked_words = moderation_cfg.get("blocked_words", [])
    lowered = message.lower()
    return any(str(word).lower() in lowered for word in blocked_words)


def _is_privileged(sender: Any) -> bool:
    if not sender:
        return False
    badges = [str(badge).lower() for badge in getattr(sender, "badges", [])]
    return "broadcaster" in badges or "moderator" in badges


def _is_spam(sender_id: int, message: str) -> bool:
    window = float(moderation_cfg.get("spam_window_seconds", 30))
    max_identical = int(moderation_cfg.get("spam_max_identical", 3))
    cutoff = time.time() - window
    history = spam_history.setdefault(sender_id, [])
    history[:] = [(msg, ts) for msg, ts in history if ts > cutoff]
    normalized = message.lower().strip()
    history.append((normalized, time.time()))
    identical_count = sum(1 for msg, _ in history if msg == normalized)
    return identical_count > max_identical


async def _safe_say(message: str, reply_to: str | None = None) -> None:
    try:
        fitted = _fit_chat_message(message)
        if not fitted:
            return
        await app.say(fitted, reply_to=reply_to)
    except Exception:
        logger.exception("Failed to send chat message")


async def _timeout_user(event: Any, reason: str) -> None:
    sender = event.sender
    if not sender or not app._broadcaster_id:
        return
    duration = int(moderation_cfg.get("timeout_duration", 600))
    try:
        await app.api.ban_user(
            broadcaster_id=app._broadcaster_id,
            user_id=sender.user_id,
            duration=duration,
            reason=reason,
        )
    except Exception:
        logger.exception("Failed to timeout user %s", sender.username)


async def _delete_message(message_id: str) -> None:
    if not message_id:
        return
    try:
        await app.api.delete_message(message_id)
    except Exception:
        logger.exception("Failed to delete message %s", message_id)


async def _handle_moderation(event: Any) -> bool:
    sender = event.sender
    if not sender or _is_privileged(sender):
        return False

    message = event.message.strip()
    username = sender.username
    reply_to = getattr(event, "message_id", None)

    if _has_blocked_word(message):
        await _delete_message(reply_to or "")
        await _timeout_user(event, "Blocked word/phrase detected")
        await _safe_say(f"@{username} mensagem removida por violar as regras.", reply_to=reply_to)
        return True

    min_caps_length = int(moderation_cfg.get("min_caps_length", 8))
    if len(message) >= min_caps_length and _caps_ratio(message) > float(
        moderation_cfg.get("max_caps_percent", 70)
    ):
        warn_template = str(
            moderation_cfg.get("warn_message") or "@{username}, please avoid excessive caps."
        )
        await _safe_say(warn_template.format(username=username), reply_to=reply_to)
        return True

    if _contains_unapproved_link(message):
        await _delete_message(reply_to or "")
        await _timeout_user(event, "Unauthorized link")
        await _safe_say(f"@{username} links não autorizados não são permitidos.", reply_to=reply_to)
        return True

    if _is_spam(sender.user_id, message):
        await _delete_message(reply_to or "")
        await _timeout_user(event, "Spam detected")
        await _safe_say(f"@{username} evita repetir a mesma mensagem.", reply_to=reply_to)
        return True

    return False


def _ensure_timers_started() -> None:
    global timers_started, comment_messages, comment_prompt_samples
    if timers_started:
        return

    timed_messages = bot_cfg.get("timed_messages", [])
    if comments_cfg.get("enabled", True) and not comment_messages and not comment_prompt_samples:
        comment_messages, comment_prompt_samples = _load_comment_corpus()
        set_comment_meme_samples(comment_prompt_samples)

    if not timed_messages and not comment_messages:
        timers_started = True
        return

    async def timer_loop(message: str, interval: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                await _safe_say(message)
        except asyncio.CancelledError:
            return

    async def comment_timer_loop(interval: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                if not comment_messages:
                    continue
                message = random.choice(comment_messages)
                if bot_username:
                    message = f"{bot_username} {message}"
                await _safe_say(message)
        except asyncio.CancelledError:
            return

    for item in timed_messages:
        message = str(item.get("message") or "").strip()
        interval = float(item.get("interval", 900))
        if not message or interval <= 0:
            continue
        timed_tasks.append(asyncio.create_task(timer_loop(message, interval)))

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

    if not msg or _is_self_message(username):
        return

    if moderation_cfg.get("enabled", True) and await _handle_moderation(event):
        return

    agent_enabled = agent_cfg.get("enabled", True)
    prompt = _extract_agent_prompt(msg) if agent_enabled else None
    if prompt is not None:
        if not prompt:
            await _safe_say(f"@{username} usa: {_agent_usage_text()}", reply_to=reply_to)
            return
        if not agent_cooldown.allow(username.lower()):
            await _safe_say(
                f"@{username} espera um pouco antes de perguntar de novo para "
                f"{bot_username or 'o bot'}.",
                reply_to=reply_to,
            )
            return
        try:
            reply = await run_agent(prompt, username, cfg=agent_cfg)
        except Exception:
            logger.exception("Agent failed for %s", username)
            await _safe_say(
                f"@{username} algo correu mal ao falar com o assistente. Tenta mais tarde.",
                reply_to=reply_to,
            )
            return
        await _safe_say(f"@{username} {reply}", reply_to=reply_to)
        return

    prefix = str(bot_cfg.get("prefix") or "!").strip()
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
                )
                return
            _mark_command_used(cmd, username)
            await _safe_say(str(command_def.get("response") or ""), reply_to=reply_to)
        return

    if "hello" in msg.lower() or "selam" in msg.lower():
        await _safe_say(f"Welcome {username}! Type !schedule for stream times.", reply_to=reply_to)


@app.on("channel.followed")
async def on_follow(event: Any) -> None:
    await _safe_say(f"Welcome to the family, {event.follower_username}!")


@app.on("kicks.gifted")
async def on_gift(event: Any) -> None:
    await _safe_say(
        f"{event.gifter_username} just sent {event.kicks_amount} kicks! Thank you!"
    )


@app.on("channel.subscription.new")
async def on_sub(event: Any) -> None:
    await _safe_say(
        f"{event.subscriber_username} just subscribed! Welcome to the squad!"
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


async def prepare_app() -> None:
    if not channel:
        return

    try:
        await app.connect(channel)
        if app_mode in {"webhook", "hybrid"}:
            existing = _existing_subscription_names(await app.api.get_subscriptions())
            missing = [name for name in WEBHOOK_EVENTS if name not in existing]
            if missing:
                await app.subscribe(missing)
                logger.info("Created webhook subscriptions: %s", ", ".join(missing))
            else:
                logger.info("Webhook subscriptions already present")
    finally:
        await app.api.close()


if __name__ == "__main__":
    asyncio.run(prepare_app())
    app.run(channel=channel, host=host, port=port)
