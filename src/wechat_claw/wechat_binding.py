"""Persistent single-user WeChat binding state."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


class BindingError(RuntimeError):
    """Raised when persisted binding state is invalid."""


@dataclass(frozen=True, slots=True)
class WeChatBinding:
    user_id: str
    context_token: str
    bound_at: str
    context_updated_at: str


class BindingStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> WeChatBinding | None:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError
            user_id = payload["user_id"]
            context_token = payload.get("context_token", "")
            bound_at = payload["bound_at"]
            context_updated_at = payload.get("context_updated_at", bound_at)
            if not all(
                isinstance(value, str)
                for value in (user_id, context_token, bound_at, context_updated_at)
            ):
                raise TypeError
            if not user_id or not bound_at:
                raise ValueError
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BindingError(f"invalid WeChat binding file: {self.path}") from exc
        return WeChatBinding(user_id, context_token, bound_at, context_updated_at)

    def save(
        self,
        user_id: str,
        context_token: str,
        *,
        bound_at: str | None = None,
    ) -> WeChatBinding:
        if not user_id:
            raise ValueError("user_id is required")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        binding = WeChatBinding(
            user_id=user_id,
            context_token=context_token,
            bound_at=bound_at or now,
            context_updated_at=now,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(asdict(binding), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, self.path)
        self.path.chmod(0o600)
        return binding

    def refresh(self, user_id: str, context_token: str) -> WeChatBinding | None:
        binding = self.load()
        if binding is None or binding.user_id != user_id:
            return None
        return self.save(
            binding.user_id,
            context_token,
            bound_at=binding.bound_at,
        )
