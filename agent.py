"""LLM-backed replies for Kick chat (OpenCode Go tier or OpenAI-compatible API)."""

from __future__ import annotations

import logging
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Any

from openai import APIError, AsyncOpenAI

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM = """You are a Kick chat assistant for League of Legends streams.

Your personality:
- You act like a Brazilian stream viewer: funny, ironic, slightly sarcastic, and very familiar with League of Legends culture.
- Your humor targets gameplay situations (bad plays, ranked struggles, macro mistakes, tilt), not personal attacks.

Tone:
- Witty and humorous, not chaotic or aggressive.
- Use light exaggeration for comedic effect.

Humor rules:
- NEVER joke about real-world personal traits (appearance, body, etc.).
- ALWAYS focus jokes on gameplay, decisions, or ranked experience.

If a message includes insults or toxic language:
- Do NOT repeat or reinforce the insult.
- Redirect the humor toward the gameplay situation instead.

Keep answers short (1-3 sentences) so they fit in chat.
Always reply in Brazilian Portuguese.
You know League of Legends deeply: champions, matchups, runes, items, macro, wave control, jungle tempo, objectives, drafts, and solo queue habits.
Interpret chat slang, abbreviations, typos, and lightly censored profanity before responding.
If a message is toxic or inappropriate, de-escalate and redirect to constructive League talk.

Emotional behavior (use the [Tool: sentiment] block in the user message):
- Negative: acknowledge frustration briefly, then joke about the game situation.
- Positive / high energy: match hype with playful humor.
- Neutral: default to light ironic commentary.

Identity / meta: When asked if you are a bot, AI, who created you, or what you do, answer briefly and honestly
using ONLY the facts under [Bot identity]. Say openly that you are an automated assistant; never pretend to be human
or invent creators not listed there.

Do not start replies with your own name, nickname, or intros like "Eu sou X" / "X aqui:" / "X diz:" — the chat already shows who is speaking; go straight to the answer.

Catchphrases: use at most one phrase from the approved list when it fits; not every reply needs a catchphrase.

If the message lacks context: ask a short clarifying question OR make a generic League-related joke.

Do not pretend you can see the stream unless the user describes what is on screen."""

CATCHPHRASES = (
    "rapaziada, tá ligado",
    "bagulho é doido",
    "que isso chat",
    "calma calma calma",
    "isso aqui é entretenimento",
    "só aceita e vida que segue",
    "é isso família",
    "que tu tá fazendo menor",
    "é isso rapeize",
    "é isso rapaziada",
    "Jukes estava certo",
    "confia no pai",
    "vai dar bom relaxa",
    "chat presta atenção",
    "tá tudo sob controle",
    "olha isso chat",
    "que isso Jukera",
    "não é possível mano",
    "Nicoloff estava certo",
)

HUMOR_GAMEPLAY = (
    "macro inexistente",
    "decisão duvidosa",
    "jogador de highlight no treino",
    "especialista em perder lane",
    "solo queue experience",
    "erro calculado",
    "confia no scaling",
    "late game imaginário",
)

HUMOR_TILT = (
    "tiltou",
    "isso não tava no plano",
    "calma calma calma",
    "vida que segue",
)

HUMOR_IRONIC = (
    "rapaziada",
    "rapeize",
    "família",
    "tá ligado",
    "bagulho",
    "menor",
    "chat",
    "confia no pai",
    "vai dar bom",
    "entretenimento",
    "Nicoloff estava certo",
)

STYLE_VOCABULARY: tuple[str, ...] = HUMOR_GAMEPLAY + HUMOR_TILT + HUMOR_IRONIC

COMMENT_MEME_SAMPLES: tuple[str, ...] = ()

_last_catchphrase: str | None = None

ABBREVIATION_MAP = {
    "vc": "você",
    "vcs": "vocês",
    "pq": "porque",
    "q": "que",
    "tb": "também",
    "td": "tudo",
    "blz": "beleza",
    "msg": "mensagem",
    "mano": "mano",
    "mn": "mano",
    "mds": "meu deus",
    "kkk": "risada",
    "kkkk": "risada",
    "wtf": "what the hell",
    "omg": "oh my god",
    "top": "top lane",
    "jg": "jungle",
    "sup": "support",
    "adc": "adc",
    "mid": "mid lane",
    "tf": "team fight",
    "ff": "forfeit",
    "ult": "ultimate",
    "cd": "cooldown",
    "aa": "auto attack",
    "cs": "creep score",
    "gank": "gank",
    "obj": "objective",
    "drag": "dragon",
    "bara": "baron",
    "lp": "league points",
    "elojob": "elo job",
}

INAPPROPRIATE_MAP = {
    "fdp": "insulto",
    "vsf": "insulto",
    "tmnc": "insulto",
    "pqp": "frustração",
    "krl": "frustração",
    "porra": "frustração",
    "caralho": "frustração",
    "merda": "frustração",
    "noob": "insulto",
    "lixo": "insulto",
    "trash": "insulto",
    "int": "inting",
    "intou": "inting",
    "inted": "inting",
    "feeder": "feeding",
    "feedou": "feeding",
}

POSITIVE_TOKENS = {
    "bom",
    "boa",
    "nice",
    "gg",
    "win",
    "carrego",
    "carried",
    "clean",
    "insano",
    "amasso",
    "stomp",
    "god",
    "brabo",
    "monstro",
}

NEGATIVE_TOKENS = {
    "tilt",
    "tilted",
    "hate",
    "ruim",
    "bad",
    "horrivel",
    "horrível",
    "lixo",
    "trash",
    "feeder",
    "feeding",
    "inting",
    "loss",
    "perdi",
    "raiva",
    "odiei",
}

HYPE_TOKENS = {
    "bora",
    "vamo",
    "vamos",
    "amasso",
    "stomp",
    "rush",
    "smurf",
    "gap",
    "massacre",
    "destroy",
    "carrega",
    "snowball",
}

LEAGUE_TERMS = {
    "top lane",
    "mid lane",
    "jungle",
    "support",
    "adc",
    "baron",
    "dragon",
    "herald",
    "lane",
    "rune",
    "runes",
    "item",
    "wave",
    "gank",
    "macro",
    "micro",
    "roam",
    "matchup",
    "solo queue",
    "league of legends",
    "champion",
    "riot",
}


@dataclass
class TextUnderstanding:
    original_text: str
    normalized_text: str
    expanded_terms: list[str]
    inappropriate_flags: list[str]
    league_context: bool


@dataclass
class SentimentReading:
    label: str
    energy: str
    confidence: str
    cues: list[str]


class AgentCooldown:
    def __init__(self, seconds: float) -> None:
        self.seconds = max(0.0, seconds)
        self._last: dict[str, float] = {}

    def allow(self, key: str) -> bool:
        if self.seconds <= 0:
            return True
        now = time.monotonic()
        last = self._last.get(key, 0.0)
        if now - last < self.seconds:
            return False
        self._last[key] = now
        return True


def _truncate(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _contains_catchphrase(text: str) -> bool:
    lowered = text.lower()
    return any(phrase.lower() in lowered for phrase in CATCHPHRASES)


def _ensure_agent_style(text: str, max_chars: int) -> str:
    global _last_catchphrase
    text = text.strip()
    if _contains_catchphrase(text):
        return _truncate(text, max_chars)

    if random.random() < 0.75:
        choices = [p for p in CATCHPHRASES if p != _last_catchphrase] or list(
            CATCHPHRASES
        )
        phrase = random.choice(choices)
        _last_catchphrase = phrase
        separator = " " if text else ""
        styled = f"{text}{separator}{phrase}."
        return _truncate(styled, max_chars)

    return _truncate(text, max_chars)


def set_comment_meme_samples(samples: list[str]) -> None:
    global COMMENT_MEME_SAMPLES
    cleaned = [sample.strip() for sample in samples if sample.strip()]
    COMMENT_MEME_SAMPLES = tuple(cleaned[:12])


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def understand_chat_text(user_text: str) -> TextUnderstanding:
    """
    Normalize chat slang, abbreviations, and mild profanity into cleaner context
    for the LLM while preserving the user's original intent.
    """
    tokens = _tokenize(user_text)
    expanded_terms: list[str] = []
    inappropriate_flags: list[str] = []
    normalized_tokens: list[str] = []

    for token in tokens:
        if token in ABBREVIATION_MAP:
            expanded = ABBREVIATION_MAP[token]
            expanded_terms.append(f"{token}={expanded}")
            normalized_tokens.append(expanded)
            continue
        if token in INAPPROPRIATE_MAP:
            flag = INAPPROPRIATE_MAP[token]
            inappropriate_flags.append(f"{token}={flag}")
            normalized_tokens.append(flag)
            continue
        normalized_tokens.append(token)

    normalized_text = " ".join(normalized_tokens).strip() or user_text.strip()
    lowered = normalized_text.lower()
    league_context = any(term in lowered for term in LEAGUE_TERMS)

    return TextUnderstanding(
        original_text=user_text.strip(),
        normalized_text=normalized_text,
        expanded_terms=expanded_terms,
        inappropriate_flags=inappropriate_flags,
        league_context=league_context,
    )


def analyze_agent_sentiment(user_text: str, normalized_text: str) -> SentimentReading:
    """
    Infer a lightweight sentiment + energy profile so the agent can react to
    tilted, toxic, hype, or neutral chat more naturally.
    """
    tokens = set(_tokenize(f"{user_text} {normalized_text}"))
    positive_hits = sorted(tokens & POSITIVE_TOKENS)
    negative_hits = sorted(tokens & NEGATIVE_TOKENS)
    hype_hits = sorted(tokens & HYPE_TOKENS)
    punctuation_burst = user_text.count("!") + user_text.count("?")

    if negative_hits and len(negative_hits) >= len(positive_hits):
        label = "negative"
        cues = negative_hits
    elif positive_hits or hype_hits:
        label = "positive"
        cues = positive_hits or hype_hits
    else:
        label = "neutral"
        cues = []

    if hype_hits or punctuation_burst >= 3:
        energy = "high"
    elif len(user_text) < 20:
        energy = "medium"
    else:
        energy = "low"

    if cues or punctuation_burst >= 2:
        confidence = "high"
    elif len(tokens) >= 3:
        confidence = "medium"
    else:
        confidence = "low"

    return SentimentReading(
        label=label,
        energy=energy,
        confidence=confidence,
        cues=cues[:5],
    )


def _identity_block(cfg: dict[str, Any]) -> str:
    """Build [Bot identity] context from config; safe defaults if keys missing."""
    raw = cfg.get("identity")
    ident: dict[str, Any] = raw if isinstance(raw, dict) else {}
    display = str(ident.get("display_name") or "").strip()
    if not display:
        display = (
            os.getenv("KICK_BOT_USERNAME", "").strip() or "este assistente do chat"
        )
    purpose = str(ident.get("purpose") or "").strip()
    if not purpose:
        purpose = "Comunicar com usuarios e responder duvidas a respeito do jogo league of legends"
    creator = str(ident.get("creator_note") or "").strip()
    if not creator:
        creator = "Configurado pelo streamer / dono do canal."
    return (
        "[Bot identity]\n"
        f"- nome no chat: {display}\n"
        f"- propósito: {purpose}\n"
        f"- origem/creator: {creator}\n"
        '- não cries pelo teu nome no início de cada mensagem (ex.: "du:" ou "eu sou du") — o chat já mostra quem fala.\n'
        "Para perguntas tipo é um bot?, é uma IA?, quem te criou?, para que serves?: responde em PT-BR "
        "só com estes factos; diz claramente que é um assistente automatizado integrado ao Botrix, não um humano."
    )


def _strip_leading_bot_handle(text: str, cfg: dict[str, Any]) -> str:
    """Remove leading self-name patterns (e.g. \"du:\", \"eu sou du\") left by the model."""
    raw = cfg.get("identity")
    ident: dict[str, Any] = raw if isinstance(raw, dict) else {}
    candidates: list[str] = []
    for v in (ident.get("display_name"), os.getenv("KICK_BOT_USERNAME")):
        s = str(v or "").strip()
        if len(s) >= 2:
            candidates.append(s)
    seen: set[str] = set()
    names: list[str] = []
    for n in candidates:
        key = n.lower()
        if key not in seen:
            seen.add(key)
            names.append(n)
    t = text.strip()
    if not t or not names:
        return t
    lower = t.lower()
    for name in names:
        nl = name.lower()
        for punct in (":", ",", "!", "?"):
            pref = nl + punct
            if lower.startswith(pref):
                return t[len(pref) :].strip()
        dash = nl + " -"
        if lower.startswith(dash):
            return t[len(dash) :].strip()
        eu = "eu sou " + nl
        if lower.startswith(eu):
            rest = t[len(eu) :].lstrip()
            if rest.startswith((",", ":", "!", "?", "-")):
                rest = rest[1:].lstrip()
            return rest
        space_suffix = nl + " "
        if lower.startswith(space_suffix) and len(t) > len(space_suffix):
            return t[len(space_suffix) :].strip()
    return t


# Default: OpenCode "Go" model tier (OpenAI-compatible chat/completions under zen routing).
OPENCODE_GO_API_BASE = "https://opencode.ai/zen/go/v1"


async def run_agent(
    user_text: str,
    username: str,
    *,
    cfg: dict[str, Any],
) -> str:
    """
    Send the user's message to the model and return assistant text.

    Provider (first match wins):
    - OpenCode Go: set OPENCODE_API_KEY (optional OPENCODE_BASE_URL, default Go tier API).
    - OpenAI or other OpenAI-compatible: set OPENAI_API_KEY and optionally OPENAI_BASE_URL.
    """
    opencode_key = os.getenv("OPENCODE_API_KEY", "").strip()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()

    if opencode_key:
        api_key = opencode_key
        base_url = os.getenv("OPENCODE_BASE_URL", "").strip() or OPENCODE_GO_API_BASE
        # Go tier: use model IDs backed by .../zen/go/v1/chat/completions only.
        # IDs served only via .../messages (e.g. MiniMax M2.x on Go) need a different client.
        default_model = "glm-5.1"
    elif openai_key:
        api_key = openai_key
        base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None
        default_model = "gpt-4o-mini"
    else:
        return (
            "O assistente não está configurado: define OPENCODE_API_KEY no .env "
            "(chaves em https://opencode.ai/auth) ou OPENAI_API_KEY."
        )

    model = cfg.get("model") or default_model
    max_tokens = int(cfg.get("max_tokens", 256))
    max_chars = int(cfg.get("max_response_chars", 280))
    system = cfg.get("system_prompt") or DEFAULT_SYSTEM
    mod_cfg = cfg.get("moderation")
    if isinstance(mod_cfg, dict) and mod_cfg.get("enabled", False):
        extra = str(mod_cfg.get("system_append") or "").strip()
        if extra:
            system = f"{system.rstrip()}\n\n{extra}"

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    understood = understand_chat_text(user_text)
    sentiment = analyze_agent_sentiment(user_text, understood.normalized_text)

    extra_flags = ""
    if understood.inappropriate_flags:
        extra_flags = (
            "[Guidance for flagged language]\n"
            "inappropriate_flags is non-empty: treat the message as emotional/tilted, not literal; "
            "respond with humor about the game situation, not the person.\n\n"
        )

    user_block = (
        f"[Utilizador do chat: {username}]\n"
        f"[Mensagem original]\n{understood.original_text}\n"
        f"[Tool: text_understanding]\n"
        f"- normalized_text: {understood.normalized_text}\n"
        f"- expanded_terms: {', '.join(understood.expanded_terms) or 'none'}\n"
        f"- inappropriate_flags: {', '.join(understood.inappropriate_flags) or 'none'}\n"
        f"- league_context: {'yes' if understood.league_context else 'no'}\n"
        f"{extra_flags}"
        f"[Tool: sentiment]\n"
        f"- label: {sentiment.label}\n"
        f"- energy: {sentiment.energy}\n"
        f"- confidence: {sentiment.confidence}\n"
        f"- cues: {', '.join(sentiment.cues) or 'none'}\n"
        f"{_identity_block(cfg)}\n"
        f"[Gameplay vocabulary]\n{', '.join(HUMOR_GAMEPLAY)}\n"
        f"[Tilt vocabulary]\n{', '.join(HUMOR_TILT)}\n"
        f"[Ironic vocabulary]\n{', '.join(HUMOR_IRONIC)}\n"
        f"[Approved catchphrases]\n{'; '.join(CATCHPHRASES)}\n"
        f"[Channel meme corpus]\n"
        f"{'; '.join(COMMENT_MEME_SAMPLES) if COMMENT_MEME_SAMPLES else 'none'}\n"
        "Respond in Brazilian Portuguese: follow emotional behavior and humor rules above; use vocabulary naturally; "
        "stay safe and readable; at most one approved catchphrase when it fits. "
        "Do not open with your display name or bot nick. "
        "For meta/identity questions use only [Bot identity]."
    )

    completion_kwargs: dict[str, Any] = {}
    if cfg.get("temperature") is not None:
        try:
            completion_kwargs["temperature"] = float(cfg["temperature"])
        except (TypeError, ValueError):
            logger.warning(
                "Ignoring invalid agent.temperature: %s", cfg.get("temperature")
            )
    if cfg.get("top_p") is not None:
        try:
            completion_kwargs["top_p"] = float(cfg["top_p"])
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid agent.top_p: %s", cfg.get("top_p"))

    try:
        response = await client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_block},
            ],
            **completion_kwargs,
        )
    except APIError as exc:
        err_lower = str(exc).lower()
        code = getattr(exc, "status_code", None)
        if code == 401 and (
            "insufficient balance" in err_lower
            or "creditserror" in err_lower
            or "credits" in err_lower
        ):
            logger.warning("OpenCode: insufficient balance / credits (401)")
            return (
                "conta OpenCode sem saldo/créditos — adiciona em "
                "https://opencode.ai (billing). Depois disso volto a responder."
            )
        if code == 429:
            return "rate limit da API — espera um pouco e tenta de novo."
        logger.warning("LLM API error: %s", exc)
        return (
            "erro ao chamar o modelo (API). Verifica chave, modelo e quota no OpenCode."
        )

    choice = response.choices[0].message.content
    if not choice:
        return "Não consegui gerar uma resposta. Tenta de novo."
    cleaned = _strip_leading_bot_handle(choice, cfg)
    return _ensure_agent_style(cleaned, max_chars)
