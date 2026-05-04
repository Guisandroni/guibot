"""Landing HTTP + API (FastAPI) — mesmo processo que o bot; usa env LANDING_API_SECRET."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles

PACKAGE_ROOT = Path(__file__).resolve().parent
LANDING_DIR = PACKAGE_ROOT / "landing"
LANDING_WEB_DIST = PACKAGE_ROOT / "web" / ".output" / "public"

app = FastAPI(title="Guibot landing", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_SENSITIVE_KEY_SUBSTR = (
    "secret",
    "token",
    "password",
    "api_key",
    "apikey",
    "authorization",
    "bearer",
)


def _strip_sensitive(obj: Any) -> Any:
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            ks = str(k).lower()
            if ks == "client_secret":
                continue
            if any(s in ks for s in _SENSITIVE_KEY_SUBSTR):
                continue
            out[str(k)] = _strip_sensitive(v)
        return out
    if isinstance(obj, list):
        return [_strip_sensitive(x) for x in obj]
    return obj


def _safe_config_snapshot() -> dict[str, Any]:
    import bot

    return {
        "channel_slugs": list(bot.channel_slugs),
        "primary_broadcaster_id": bot.primary_broadcaster_id,
        "chat_poster_type": bot.chat_poster_type,
        "kick": _strip_sensitive(dict(bot.kick_cfg)),
        "bot": _strip_sensitive(dict(bot.bot_cfg)),
        "agent": _strip_sensitive(dict(bot.agent_cfg)),
        "chat_activity": dict(bot.chat_activity_cfg),
    }


def _commands_help_public() -> dict[str, str]:
    """Atalhos para a landing (sem sorteio)."""
    import bot

    prefix = str(bot.bot_cfg.get("prefix") or "!").strip()
    lines: dict[str, str] = {
        "agent": (
            f"Assistente LLM: usa agent.* no YAML; trigger actual «{bot.agent_trigger}» "
            "(e menção se KICK_BOT_USERNAME estiver definido)."
        ),
        "commands_yaml": (
            f"Comandos estáticos em bot.commands com prefixo {prefix!r} — "
            "respostas fixas no config."
        ),
    }
    riot = bot.bot_cfg.get("riot_rank") or {}
    if isinstance(riot, dict) and riot.get("enabled", True):
        cmd = str(riot.get("command") or "elo").strip().lower()
        tm = str(riot.get("trigger_mode") or "prefix").strip().lower()
        if tm == "word":
            wp = str(riot.get("word_position") or "start").strip().lower()
            anywhere_note = (
                " Palavra comando em qualquer posição na frase (word_position)."
                if wp == "anywhere"
                else ""
            )
            lines["elo"] = (
                f"Rank LoL: «{cmd}» ou «{cmd} Nome#TAG br1» — RIOT_API_KEY; "
                "defaults default_riot/default_platform opcionais."
                + anywhere_note
            )
        else:
            lines["elo"] = (
                f"Rank LoL: {prefix}{cmd} … — RIOT_API_KEY; "
                "macro $(leagueoflegends …) nas mensagens do bot."
            )
    try:
        from agent_web_search import web_search_enabled

        if web_search_enabled(bot.agent_cfg):
            lines["web_search"] = (
                "Pesquisa web Tavily via tool calling — TAVILY_API_KEY no .env."
            )
        else:
            lines["web_search"] = (
                "Pesquisa web: agent.web_search.enabled + TAVILY_API_KEY (ver documentação)."
            )
    except ImportError:
        lines["web_search"] = "Módulo de pesquisa web não disponível."
    return lines


def _require_bearer(authorization: str | None) -> None:
    secret = os.getenv("LANDING_API_SECRET", "").strip()
    if not secret:
        raise HTTPException(status_code=503, detail="LANDING_API_SECRET não configurado")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer obrigatório")
    token = authorization[7:].strip()
    if token != secret:
        raise HTTPException(status_code=403, detail="Token inválido")


@app.get("/api/public")
async def api_public() -> dict:
    """Estado só leitura para a página (sem segredo)."""
    import bot

    messages = list(bot._recent_bot_outbound)
    return {
        "channel_slug": bot.channel_slugs[0] if bot.channel_slugs else None,
        "recent_bot_messages": messages[-50:],
        "commands_help": _commands_help_public(),
        "chat_activity_enabled": bool(bot.chat_activity_cfg.get("enabled")),
    }


@app.get("/api/config")
async def api_config(authorization: str | None = Header(None)) -> dict[str, Any]:
    """Configuração efectiva (sanitizada). Requer o mesmo Bearer que os POSTs."""
    _require_bearer(authorization)
    return _safe_config_snapshot()


@app.post("/api/sorteio")
async def api_sorteio(
    authorization: str | None = Header(None),
    body: dict[str, Any] | None = Body(default=None),
) -> dict:
    _require_bearer(authorization)
    import bot

    parts = ["!sorteio"]
    if body and isinstance(body.get("args"), str):
        extra = body["args"].strip().split()
        parts.extend(extra)

    ck = bot._resolve_chat_activity_channel_key(bot.primary_broadcaster_id)
    res = await bot.execute_sorteio_draw(
        ck,
        parts,
        triggered_by="landing",
        source="landing",
    )
    msg = res.get("message") or ""
    if msg:
        await bot._safe_say(
            bot._fit_chat_message(msg),
            broadcaster_id=bot.primary_broadcaster_id,
        )
    return res


@app.post("/api/topchat")
async def api_topchat(
    authorization: str | None = Header(None),
    body: dict[str, Any] | None = Body(default=None),
) -> dict:
    _require_bearer(authorization)
    import bot

    parts = ["!topchat"]
    if body and isinstance(body.get("args"), str):
        parts.extend(body["args"].strip().split())

    ck = bot._resolve_chat_activity_channel_key(bot.primary_broadcaster_id)
    text = bot.execute_topchat_text(ck, parts)
    await bot._safe_say(bot._fit_chat_message(text), broadcaster_id=bot.primary_broadcaster_id)
    return {"ok": True, "message": text}


@app.post("/api/clear")
async def api_clear(authorization: str | None = Header(None)) -> dict:
    _require_bearer(authorization)
    import bot

    ck = bot._resolve_chat_activity_channel_key(bot.primary_broadcaster_id)
    await bot.execute_clear_channel(ck)
    await bot._safe_say(
        bot._fit_chat_message(
            "Stats deste canal limpas (via landing). Novas msgs voltam a contar daqui."
        ),
        broadcaster_id=bot.primary_broadcaster_id,
    )
    return {"ok": True}


def _prepare_spa_files() -> Path | None:
    """Garante index.html no dist a partir de _shell.html (build TanStack SPA)."""
    if LANDING_WEB_DIST.is_dir():
        index = LANDING_WEB_DIST / "index.html"
        shell = LANDING_WEB_DIST / "_shell.html"
        if not index.is_file() and shell.is_file():
            shutil.copyfile(shell, index)
        if index.is_file():
            return LANDING_WEB_DIST
    legacy = LANDING_DIR / "index.html"
    if legacy.is_file():
        return LANDING_DIR
    return None


_static_root = _prepare_spa_files()
if _static_root is not None:
    _static_base = _static_root.resolve()

    def _safe_file_under_root(rel: str) -> Path | None:
        """Resolve a single static file; reject traversal and API-like paths."""
        if not rel or rel.startswith("api"):
            return None
        rel_norm = rel.replace("\\", "/").strip("/")
        if any(p == ".." for p in rel_norm.split("/")):
            return None
        candidate = (_static_root / rel_norm).resolve()
        try:
            candidate.relative_to(_static_base)
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    _assets_dir = _static_root / "assets"
    if _assets_dir.is_dir():
        app.mount(
            "/assets",
            StaticFiles(directory=str(_assets_dir)),
            name="landing_assets",
        )

    @app.get("/")
    async def landing_index() -> FileResponse:
        idx = _static_root / "index.html"
        if not idx.is_file():
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(idx)

    @app.get("/{full_path:path}")
    async def landing_spa_or_static(full_path: str) -> FileResponse:
        # Unmatched /api/* should behave like FastAPI (JSON 404), not serve index.html.
        if full_path.startswith("api"):
            raise HTTPException(status_code=404, detail="Not Found")
        existing = _safe_file_under_root(full_path)
        if existing is not None:
            return FileResponse(existing)
        idx = _static_root / "index.html"
        if idx.is_file():
            return FileResponse(idx)
        raise HTTPException(status_code=404, detail="Not Found")

else:

    @app.get("/")
    async def landing_unavailable() -> JSONResponse:
        return JSONResponse(
            {
                "detail": (
                    "Landing SPA não encontrada. Compila o front-end: "
                    "cd web && npm ci && npm run build"
                ),
            },
            status_code=503,
        )
