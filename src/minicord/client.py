from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(slots=True)
class MinicordClient:
    """Very small Discord REST client.

    This is intentionally minimal and intended as a starter for extension.
    """

    token: str
    base_url: str = "https://discord.com/api/v10"
    timeout_seconds: float = 15.0

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bot {self.token}",
            "User-Agent": "minicord (https://example.local, 0.1.0)",
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.request(method, url, headers=self._headers(), **kwargs)
        response.raise_for_status()
        return response.json()

    def get_gateway_bot(self) -> dict[str, Any]:
        """Return metadata for connecting to Discord Gateway."""
        return self._request("GET", "/gateway/bot")
