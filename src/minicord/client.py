from __future__ import annotations

from dataclasses import dataclass
from .gateway import GatewayHandler, GatewayOpcode, Intents
from typing import Any

import httpx


@dataclass(slots=True)
class MinicordClient:
    """
    Minimal Discord API client that can connect to the gateway and send REST requests.
    Minicord intends to primarily be used with Python 3.13+ freethreaded builds.
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

    def connect(self, intents: Intents) -> GatewayHandler:
        gateway_info = self._request("GET", "/gateway/bot")
        gateway_url = gateway_info["url"]
        gateway_handler = GatewayHandler(token=self.token, intents=intents, gateway_url=gateway_url)
        gateway_handler.connect()
        return gateway_handler
