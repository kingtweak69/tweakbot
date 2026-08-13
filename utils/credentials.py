from __future__ import annotations

import asyncio
import time
from typing import Any


class CredentialVault:
    """Process-local OAuth session store.

    OAuth credentials are deliberately never persisted. Tokens live only in RAM
    for the lifetime of this bot process and disappear on restart. The database
    stores account metadata only.
    """

    _sessions: dict[tuple[int, str], dict[str, Any]] = {}
    _lock = asyncio.Lock()

    async def put(self, user_id: int, provider: str, payload: dict[str, Any]) -> None:
        # Never serialize or persist this object. Expiration is deliberately
        # short-lived; provider refresh tokens are only retained while active.
        item = dict(payload)
        item["_session_expires_at"] = time.time() + 24 * 60 * 60
        async with self._lock:
            self._sessions[(int(user_id), str(provider))] = item

    async def get(self, user_id: int, provider: str) -> dict[str, Any] | None:
        async with self._lock:
            value = self._sessions.get((int(user_id), str(provider)))
            if value is None:
                return None
            if float(value.get("_session_expires_at", 0)) <= time.time():
                self._sessions.pop((int(user_id), str(provider)), None)
                return None
            return dict(value)

    async def delete(self, user_id: int, provider: str) -> None:
        async with self._lock:
            self._sessions.pop((int(user_id), str(provider)), None)

    async def clear(self) -> None:
        async with self._lock:
            self._sessions.clear()

    def __bool__(self) -> bool:
        return True
