"""Web search for agent tool calling via Tavily (https://tavily.com)."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

TAVILY_URL = "https://api.tavily.com/search"

# OpenAI-style tool schema for chat.completions
WEB_SEARCH_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Pesquisa na web factos actuais, notícias, preços, datas, ou qualquer informação "
            "que precise de fontes externas. Usa uma query curta e objectiva (pt ou en)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Termos de pesquisa (ex.: cotação euro hoje, resultado jogo X).",
                }
            },
            "required": ["query"],
        },
    },
}

SYSTEM_APPEND_WEB_TOOLS = (
    "\n\n## Ferramenta web_search\n"
    "Se o utilizador precisar de informação actualizada, factual ou que não conheces de certeza, "
    "chama web_search com uma query curta. Depois resume a resposta final para o chat em 1–3 frases, "
    "sem listar URLs completas (no máximo uma fonte curta se fizer sentido)."
)


def resolve_tavily_api_key(cfg: dict[str, Any] | None) -> str:
    raw = cfg or {}
    return (
        os.getenv("TAVILY_API_KEY", "").strip()
        or str(raw.get("api_key") or "").strip()
    )


def web_search_enabled(cfg: dict[str, Any] | None) -> bool:
    raw = (cfg or {}).get("web_search")
    if not isinstance(raw, dict):
        return False
    if not raw.get("enabled", False):
        return False
    return bool(resolve_tavily_api_key(raw))


async def run_web_search(
    query: str,
    *,
    cfg: dict[str, Any] | None = None,
    api_key: str | None = None,
) -> str:
    """
    Executa pesquisa Tavily e devolve texto compacto para o modelo (role=tool).
    Em erro, mensagem curta em texto plano.
    """
    key = (api_key or resolve_tavily_api_key(cfg or {})).strip()
    if not key:
        return "Erro: TAVILY_API_KEY não configurada."

    q = " ".join(query.split()).strip()
    if not q:
        return "Erro: query de pesquisa vazia."

    ws = (cfg or {}).get("web_search") if isinstance(cfg, dict) else {}
    if not isinstance(ws, dict):
        ws = {}
    try:
        max_results = int(ws.get("max_results", 5))
    except (TypeError, ValueError):
        max_results = 5
    max_results = max(1, min(max_results, 10))

    payload = {
        "api_key": key,
        "query": q,
        "max_results": max_results,
        "search_depth": str(ws.get("search_depth") or "basic").strip() or "basic",
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(TAVILY_URL, json=payload)
    except httpx.RequestError as e:
        logger.warning("Tavily request failed: %s", e)
        return f"Erro de rede na pesquisa: {e!s}"

    if r.status_code != 200:
        logger.warning("Tavily HTTP %s: %s", r.status_code, r.text[:300])
        return f"Erro na API de pesquisa (HTTP {r.status_code})."

    try:
        data = r.json()
    except json.JSONDecodeError:
        return "Erro: resposta da pesquisa inválida."

    results = data.get("results")
    if not isinstance(results, list) or not results:
        answer = data.get("answer")
        if isinstance(answer, str) and answer.strip():
            return _truncate_block(f"Resumo: {answer.strip()}", 6000)
        return "Nenhum resultado útil encontrado para esta query."

    lines: list[str] = []
    for i, item in enumerate(results[:max_results], 1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        snippet = str(item.get("content") or item.get("snippet") or "").strip()
        bit = f"{i}. {title}" if title else f"{i}."
        if url:
            bit += f" | {url}"
        if snippet:
            bit += f"\n   {snippet}"
        lines.append(bit)

    if not lines:
        return "Resultados vazios."

    block = "Resultados da pesquisa:\n" + "\n".join(lines)
    return _truncate_block(block, 8000)


def _truncate_block(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def assistant_with_tools_to_dict(message: Any) -> dict[str, Any]:
    """Serializa ChatCompletionMessage do OpenAI SDK para o próximo turno."""
    d: dict[str, Any] = {
        "role": message.role,
        "content": message.content,
    }
    tcs = getattr(message, "tool_calls", None)
    if not tcs:
        return d
    tool_calls: list[dict[str, Any]] = []
    for tc in tcs:
        fn = getattr(tc, "function", None)
        tool_calls.append(
            {
                "id": tc.id,
                "type": getattr(tc, "type", None) or "function",
                "function": {
                    "name": fn.name if fn else "",
                    "arguments": fn.arguments if fn else "{}",
                },
            }
        )
    d["tool_calls"] = tool_calls
    return d
