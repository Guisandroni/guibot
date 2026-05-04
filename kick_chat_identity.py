"""Deriva tier (vip / sub / none) a partir do objeto sender do KickForge."""

from __future__ import annotations

from typing import Any


def normalize_badge_types(sender: Any) -> list[str]:
    """Lista de tipos de badge em minúsculas (WebSocket ou webhook)."""
    raw = getattr(sender, "badges", None) or []
    out: list[str] = []
    if isinstance(raw, list):
        for b in raw:
            if isinstance(b, str) and b.strip():
                out.append(b.strip().lower())
            elif isinstance(b, dict):
                t = b.get("type") or b.get("text")
                if t:
                    out.append(str(t).strip().lower())
    return out


def classify_sender_tier(sender: Any, vip_badge_types: list[str]) -> str:
    """
    vip_badge_types: strings configuráveis (ex. tipo real do badge VIP na Kick).
    Ordem: VIP > sub > none.
    """
    badges = set(normalize_badge_types(sender))
    vip_set = {str(v).strip().lower() for v in vip_badge_types if str(v).strip()}
    if vip_set and badges & vip_set:
        return "vip"
    if getattr(sender, "is_subscriber", False):
        return "sub"
    if "subscriber" in badges:
        return "sub"
    return "none"
