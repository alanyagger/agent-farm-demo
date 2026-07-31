from __future__ import annotations

import os
from typing import Any

import httpx


class FarmApiClient:
    def __init__(self) -> None:
        self.base_url = os.getenv(
            "FARM_API_BASE_URL",
            "http://127.0.0.1:8010",
        ).rstrip("/")
        self.token = os.getenv("FARM_API_TOKEN", "")
        self.timeout = float(os.getenv("FARM_API_TIMEOUT_SECONDS", "10"))

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.token:
            return {
                "ok": False,
                "status": "CONFIG_ERROR",
                "message": "OpenClaw 农场 Token 尚未配置",
            }

        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout,
                follow_redirects=False,
                headers={"Authorization": f"Bearer {self.token}"},
            ) as client:
                response = client.request(method, path, json=payload)
        except httpx.TimeoutException:
            return {
                "ok": False,
                "status": "TIMEOUT",
                "message": "农场后端响应超时，本次操作未确认成功",
            }
        except httpx.RequestError:
            return {
                "ok": False,
                "status": "BACKEND_UNAVAILABLE",
                "message": "无法连接本机农场后端，请先启动 Demo",
            }

        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.is_success and isinstance(data, dict):
            return data

        detail = data.get("detail") if isinstance(data, dict) else None
        return {
            "ok": False,
            "status": f"HTTP_{response.status_code}",
            "message": str(detail or "农场后端拒绝了本次操作")[:240],
        }
