"""LLM-backed replies for Kick chat (OpenAI-compatible API)."""

from __future__ import annotations

import os
import random
import re
import time
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

DEFAULT_SYSTEM = """You are a hyper-energetic Kick stream assistant with an obsessive focus on League of Legends.
Your vibe is intense, unhinged, and passionate like a ranked solo queue maniac, but never hateful or abusive.
Keep answers short (1-3 sentences) so they fit in chat.
Always reply in Brazilian Portuguese.
You know League of Legends deeply: champions, matchups, runes, items, macro, wave control, jungle tempo, objectives, drafts, and solo queue habits.
Interpret chat slang, abbreviations, typos, and lightly censored profanity before responding.
If a message is toxic or inappropriate, de-escalate it and redirect to constructive League of Legends talk.
In every answer, include exactly one catchphrase from the approved list and keep the tone of Brazilian stream chat.
Do not pretend you can see the stream unless the user describes what is on screen."""

CATCHPHRASES = (
    "Respeita geral nesse bagulho aí, falou qualquer m* é ban",
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
)

STYLE_VOCABULARY = (
    "rapaziada",
    "rapeize",
    "família",
    "tá ligado",
    "bagulho",
    "menor",
    "chat",
    "Jukera",
    "Jukes",
    "confia no pai",
    "vida que segue",
    "vai dar bom",
    "entretenimento",
    "calma calma calma",
)

COMMENT_MEME_SAMPLES: tuple[str, ...] = ()

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
    text = text.strip()
    if _contains_catchphrase(text):
        return _truncate(text, max_chars)

    phrase = random.choice(CATCHPHRASES)
    separator = " " if text else ""
    styled = f"{text}{separator}{phrase}."
    return _truncate(styled, max_chars)


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


GROQ_API_BASE = "https://api.groq.com/openai/v1"


async def run_agent(
    user_text: str,
    username: str,
    *,
    cfg: dict[str, Any],
) -> str:
    """
    Send the user's message to the model and return assistant text.

    Provider (first match wins):
    - Groq: set GROQ_API_KEY (optional GROQ_BASE_URL, default Groq OpenAI-compatible API).
    - OpenAI ou outro: set OPENAI_API_KEY e opcionalmente OPENAI_BASE_URL.
    """
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()

    if groq_key:
        api_key = groq_key
        base_url = os.getenv("GROQ_BASE_URL", "").strip() or GROQ_API_BASE
        default_model = "llama-3.3-70b-versatile"
    elif openai_key:
        api_key = openai_key
        base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None
        default_model = "gpt-4o-mini"
    else:
        return (
            "O assistente não está configurado: define GROQ_API_KEY no .env "
            "(consola https://console.groq.com/keys) ou OPENAI_API_KEY."
        )

    model = cfg.get("model") or default_model
    max_tokens = int(cfg.get("max_tokens", 256))
    max_chars = int(cfg.get("max_response_chars", 380))
    system = cfg.get("system_prompt") or DEFAULT_SYSTEM

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    understood = understand_chat_text(user_text)
    sentiment = analyze_agent_sentiment(user_text, understood.normalized_text)

    user_block = (
        f"[Utilizador do chat: {username}]\n"
        f"[Mensagem original]\n{understood.original_text}\n"
        f"[Tool: text_understanding]\n"
        f"- normalized_text: {understood.normalized_text}\n"
        f"- expanded_terms: {', '.join(understood.expanded_terms) or 'none'}\n"
        f"- inappropriate_flags: {', '.join(understood.inappropriate_flags) or 'none'}\n"
        f"- league_context: {'yes' if understood.league_context else 'no'}\n"
        f"[Tool: sentiment]\n"
        f"- label: {sentiment.label}\n"
        f"- energy: {sentiment.energy}\n"
        f"- confidence: {sentiment.confidence}\n"
        f"- cues: {', '.join(sentiment.cues) or 'none'}\n"
        f"[Style vocabulary]\n{', '.join(STYLE_VOCABULARY)}\n"
        f"[Approved catchphrases]\n{'; '.join(CATCHPHRASES)}\n"
        f"[Channel meme corpus]\n"
        f"{'; '.join(COMMENT_MEME_SAMPLES) if COMMENT_MEME_SAMPLES else 'none'}\n"
        "Respond in Brazilian Portuguese as a League of Legends obsessed stream maniac, "
        "reuse the channel's meme vocabulary naturally, but keep it safe, readable, "
        "and include exactly one approved catchphrase."
    )

    response = await client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_block},
        ],
    )
    choice = response.choices[0].message.content
    if not choice:
        return "Não consegui gerar uma resposta. Tenta de novo."
    return _ensure_agent_style(choice, max_chars)
