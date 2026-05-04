"""LLM-backed replies for Kick chat (NVIDIA NIM, OpenCode Go, or OpenAI-compatible).

Default model IDs per provider (overridden by ``agent.model`` in config):
NVIDIA → ``deepseek-ai/deepseek-v4-flash``; OpenCode Go → ``glm-5.1``; OpenAI → ``gpt-4o-mini``.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Any, NamedTuple

from agent_web_search import (
    SYSTEM_APPEND_WEB_TOOLS,
    WEB_SEARCH_TOOL,
    assistant_with_tools_to_dict,
    run_web_search,
    web_search_enabled,
)
from openai import APIError, AsyncOpenAI

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM = (
    "Você é um assistente de chat na Kick: responde o pedido do usuário com naturalidade, "
    "sem forçar tema.\n\n"
    "## PERSONALIDADE\n"
    "- Brasileiro, irônico e bem-humorado; tom de stream, nada de robô burocrático\n\n"
    "## CONTEÚDO\n"
    "- Perguntas gerais (cultura, curiosidade, tech, piadas, serviços cotidianos, conversa): "
    "responda direto, útil e adequado ao pedido. Não desvie para League of Legends nem invente "
    "piadas de jogo se o assunto não for jogo ou stream.\n"
    "- League of Legends (patch, campeões, meta, gameplay, solo queue, humor de elo): use "
    "conhecimento de LoL e o tom característico quando o chat estiver claramente nesse tema.\n"
    "- Live/stream (placar, jogada, hype): só finja que está vendo se descreverem o que está "
    "acontecendo ou se for óbvio pelo contexto.\n"
    "- Se não souber: diga em uma frase curta; não invente fatos nem empurre LoL como resposta.\n\n"
    "## REGRAS DE RESPOSTA\n"
    "- Máximo 1-3 frases (chat rápido, ninguém lê textão)\n"
    "- NUNCA comece com seu nome ou apelido — o chat já mostra quem fala\n\n"
    "## EXEMPLOS\n"
    'Chat: "qual a capital da França?"\n'
    'Resposta: "Paris."\n\n'
    'Chat: "qual o elo do streamer?"\n'
    'Resposta: "Pelo gameplay? parece que tá farmando derrota em todas as ligas, mas e so digitar elo no chat que eu falo."\n\n'
    'Chat: "que jogada horrível"\n'
    'Resposta: "muito ruim slk kkkk..."\n\n'
    'Chat: "vc é bot?"\n'
    'Resposta: "Sou sim, assistente automático do canal. Pergunta à vontade."\n\n'
    'Chat: "boa partida, jogou muito"\n'
    'Resposta: "monstro demais — quando aposentar, banem a conta de tão forte."\n\n'
    'Chat: "eae"\n'
    'Resposta: "oi"\n\n'
)

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

    if random.random() < 0.25:
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
    """Build [Identidade] block from config; safe defaults if keys missing."""
    raw = cfg.get("identity")
    ident: dict[str, Any] = raw if isinstance(raw, dict) else {}
    display = str(ident.get("display_name") or "").strip()
    if not display:
        display = (
            os.getenv("KICK_BOT_USERNAME", "").strip() or "este assistente do chat"
        )
    purpose = str(ident.get("purpose") or "").strip()
    if not purpose:
        purpose = "Ajudar no chat da live: perguntas gerais e, quando fizer sentido, League of Legends"
    creator = str(ident.get("creator_note") or "").strip()
    if not creator:
        creator = "Configurado pelo streamer / dono do canal."
    return (
        "[Identidade]\n"
        f"- nome no chat: {display}\n"
        f"- propósito: {purpose}\n"
        f"- origem/criador: {creator}\n"
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
NVIDIA_DEFAULT_BASE = "https://integrate.api.nvidia.com/v1"


class ResolvedLLM(NamedTuple):
    provider: str  # nvidia | opencode | openai
    api_key: str
    base_url: str | None
    model: str


def _opencode_model_from_cfg(cfg: dict[str, Any], default_model: str) -> str:
    """OpenCode Go chat/completions rejects NVIDIA Build-style IDs (org/model)."""
    raw = str(cfg.get("model") or default_model).strip()
    if "/" in raw:
        logger.warning(
            "OpenCode Go não suporta o ID %r (formato catálogo NVIDIA). "
            "A usar glm-5.1. Para DeepSeek na NVIDIA define NVIDIA_API_KEY no .env.",
            raw,
        )
        return "glm-5.1"
    return raw


def _resolve_llm(cfg: dict[str, Any]) -> ResolvedLLM | None:
    """Pick provider (env order: NVIDIA → OpenCode → OpenAI) and merge cfg.model."""
    nvidia_key = os.getenv("NVIDIA_API_KEY", "").strip()
    opencode_key = os.getenv("OPENCODE_API_KEY", "").strip()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()

    if nvidia_key:
        base = os.getenv("NVIDIA_BASE_URL", "").strip() or NVIDIA_DEFAULT_BASE
        default_model = "deepseek-ai/deepseek-v4-flash"
        return ResolvedLLM(
            "nvidia",
            nvidia_key,
            base,
            str(cfg.get("model") or default_model).strip(),
        )
    if opencode_key:
        base = os.getenv("OPENCODE_BASE_URL", "").strip() or OPENCODE_GO_API_BASE
        default_model = "glm-5.1"
        return ResolvedLLM(
            "opencode",
            opencode_key,
            base,
            _opencode_model_from_cfg(cfg, default_model),
        )
    if openai_key:
        base_raw = os.getenv("OPENAI_BASE_URL", "").strip()
        base: str | None = base_raw or None
        default_model = "gpt-4o-mini"
        return ResolvedLLM(
            "openai",
            openai_key,
            base,
            str(cfg.get("model") or default_model).strip(),
        )
    return None


def _llm_help_hint(provider: str) -> str:
    """Long hint for logs only (may contain URLs)."""
    if provider == "nvidia":
        return (
            "Verifica NVIDIA_API_KEY, NVIDIA_BASE_URL (se usares um custom), "
            "o nome exacto do modelo no catálogo Build e quota em https://build.nvidia.com/"
        )
    if provider == "opencode":
        return "Verifica OPENCODE_API_KEY, agent.model no tier Go e quota em https://opencode.ai/"
    return "Verifica OPENAI_API_KEY, OPENAI_BASE_URL e quotas na API."


def _llm_chat_error_short(provider: str) -> str:
    """Kick chat rejects long replies with URLs (MAX_SPECIAL_CHARS_ERROR)."""
    if provider == "nvidia":
        return "Erro na API. Ver env NVIDIA e modelo no Build."
    if provider == "opencode":
        return "Erro na API OpenCode. Ver modelo Go no config ou usa NVIDIA_API_KEY para DeepSeek."
    return "Erro na API. Ver OPENAI_API_KEY e modelo."


async def probe_llm(cfg: dict[str, Any]) -> tuple[bool, str]:
    """
    Minimal chat completion with the configured model (validates key + model ID).

    ``GET /v1/models`` alone is not enough: OpenCode returns 200 while chat rejects
    NVIDIA-style model IDs.
    """
    resolved = _resolve_llm(cfg)
    if resolved is None:
        return (
            False,
            "sem chave LLM no .env (NVIDIA_API_KEY / OPENCODE_API_KEY / OPENAI_API_KEY)",
        )

    client = AsyncOpenAI(api_key=resolved.api_key, base_url=resolved.base_url)
    base_log = resolved.base_url or "https://api.openai.com/v1"
    try:
        await client.chat.completions.create(
            model=resolved.model,
            max_tokens=1,
            messages=[{"role": "user", "content": "."}],
        )
        return (
            True,
            f"{resolved.provider} endpoint OK (base_url={base_log}, model={resolved.model})",
        )
    except APIError as exc:
        return False, f"{resolved.provider}: {exc}"
    except Exception as exc:
        return False, f"{resolved.provider}: {exc}"
    finally:
        try:
            await client.close()
        except Exception:
            pass


async def run_agent(
    user_text: str,
    username: str,
    *,
    cfg: dict[str, Any],
) -> str:
    """
    Send the user's message to the model and return assistant text.

    Provider (first match wins):
    - NVIDIA (OpenAI-compatible): set NVIDIA_API_KEY (and optionally NVIDIA_BASE_URL, default https://integrate.api.nvidia.com/v1).
    - OpenCode Go: set OPENCODE_API_KEY (optional OPENCODE_BASE_URL, default Go tier API).
    - OpenAI or other OpenAI-compatible: set OPENAI_API_KEY and optionally OPENAI_BASE_URL.
    """
    resolved = _resolve_llm(cfg)
    if resolved is None:
        return "Assistente nao configurado: define NVIDIA_API_KEY ou OPENCODE_API_KEY ou OPENAI_API_KEY no env."

    provider = resolved.provider
    model = resolved.model
    max_tokens = int(cfg.get("max_tokens", 256))
    max_chars = int(cfg.get("max_response_chars", 280))
    system = cfg.get("system_prompt") or DEFAULT_SYSTEM
    mod_cfg = cfg.get("moderation")
    if isinstance(mod_cfg, dict) and mod_cfg.get("enabled", False):
        extra = str(mod_cfg.get("system_append") or "").strip()
        if extra:
            system = f"{system.rstrip()}\n\n{extra}"

    client = AsyncOpenAI(api_key=resolved.api_key, base_url=resolved.base_url)
    understood = understand_chat_text(user_text)
    sentiment = analyze_agent_sentiment(user_text, understood.normalized_text)

    extra_flags = ""
    if understood.inappropriate_flags:
        extra_flags = (
            "[Linguagem detectada]\n"
            "mensagem contém linguagem inadequada — trate como emocional/tiltado, não literal; "
            "responda com humor leve ou redirecione educado, sem atacar a pessoa.\n\n"
        )

    user_block = (
        f"[Usuário: {username}]\n"
        f"Mensagem: {understood.original_text}\n"
        f"[Texto normalizado]\n{understood.normalized_text}\n"
        f"{extra_flags}"
        f"[Sentimento]\n"
        f"- label: {sentiment.label}\n"
        f"- energia: {sentiment.energy}\n"
        f"- dicas: {', '.join(sentiment.cues) or 'nenhuma'}\n"
        f"{_identity_block(cfg)}"
        f"[Canal meme corpus]\n"
        f"{'; '.join(COMMENT_MEME_SAMPLES) if COMMENT_MEME_SAMPLES else 'nenhum'}\n"
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

    use_tools = web_search_enabled(cfg)
    if use_tools:
        system = f"{system.rstrip()}{SYSTEM_APPEND_WEB_TOOLS}"

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_block},
    ]

    create_base: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        **completion_kwargs,
    }

    try:
        if use_tools:
            try:
                response = await client.chat.completions.create(
                    **create_base,
                    messages=messages,
                    tools=[WEB_SEARCH_TOOL],
                    tool_choice="auto",
                )
            except Exception as first_exc:
                logger.warning(
                    "LLM call with web_search tools failed (%s); retrying without tools",
                    first_exc,
                )
                use_tools = False
                response = await client.chat.completions.create(
                    **create_base,
                    messages=messages,
                )
        else:
            response = await client.chat.completions.create(
                **create_base,
                messages=messages,
            )

        msg = response.choices[0].message

        if use_tools and getattr(msg, "tool_calls", None):
            messages.append(assistant_with_tools_to_dict(msg))
            for tc in msg.tool_calls:
                fn = getattr(tc, "function", None)
                name = fn.name if fn else ""
                raw_args = fn.arguments if fn else "{}"
                try:
                    args = json.loads(raw_args or "{}")
                except json.JSONDecodeError:
                    args = {}
                if name == "web_search":
                    q = str(args.get("query", "")).strip()
                    tool_body = (
                        await run_web_search(q, cfg=cfg) if q else "Query vazia."
                    )
                else:
                    tool_body = f'{{"error":"unknown_tool","name":"{name}"}}'
                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": tool_body}
                )
            response = await client.chat.completions.create(
                **create_base,
                messages=messages,
            )
            msg = response.choices[0].message

    except APIError as exc:
        err_lower = str(exc).lower()
        code = getattr(exc, "status_code", None)
        if (
            provider == "opencode"
            and code == 401
            and (
                "insufficient balance" in err_lower
                or "creditserror" in err_lower
                or "credits" in err_lower
            )
        ):
            logger.warning("OpenCode: insufficient balance / credits (401)")
            return "OpenCode sem saldo ou creditos. Resolve no billing e tenta de novo."
        if code == 429:
            return "rate limit na API, espera um pouco."
        logger.warning("LLM API error (%s): %s", provider, exc)
        logger.info("LLM hint (logs): %s", _llm_help_hint(provider))
        return _llm_chat_error_short(provider)
    except Exception as exc:
        logger.warning("LLM request failed (%s, non-API): %s", provider, exc)
        logger.info("LLM hint (logs): %s", _llm_help_hint(provider))
        return _llm_chat_error_short(provider)

    choice = msg.content
    if not choice:
        if use_tools and getattr(msg, "tool_calls", None):
            return "A pesquisa correu mas não consegui resumir. Tenta de novo."
        return "Não consegui gerar uma resposta. Tenta de novo."
    cleaned = _strip_leading_bot_handle(choice, cfg)
    return _ensure_agent_style(cleaned, max_chars)
