import base64
import httpx
import asyncio
from typing import Optional


class WAHAClient:
    def __init__(self, base_url: str, api_key: Optional[str] = None, session_name: str = "default"):
        self.base_url = base_url.rstrip("/")
        self.session_name = session_name
        self.headers = {}
        if api_key:
            self.headers["X-Api-Key"] = api_key

    async def get_session_status(self) -> dict:
        async with httpx.AsyncClient(headers=self.headers, timeout=10) as client:
            r = await client.get(f"{self.base_url}/api/sessions/{self.session_name}")
            r.raise_for_status()
            return r.json()

    async def list_sessions(self) -> list:
        async with httpx.AsyncClient(headers=self.headers, timeout=10) as client:
            r = await client.get(f"{self.base_url}/api/sessions")
            r.raise_for_status()
            return r.json()

    async def create_session(self, name: str) -> dict:
        """Create (and start) a new session with an arbitrary name."""
        async with httpx.AsyncClient(headers=self.headers, timeout=15) as client:
            r = await client.post(
                f"{self.base_url}/api/sessions",
                json={"name": name}
            )
            r.raise_for_status()
            return r.json() if r.content else {}

    async def start_session(self) -> dict:
        """Create (and start) the configured session."""
        return await self.create_session(self.session_name)

    async def stop_session(self) -> dict:
        async with httpx.AsyncClient(headers=self.headers, timeout=15) as client:
            r = await client.delete(f"{self.base_url}/api/sessions/{self.session_name}")
            r.raise_for_status()
            # WAHA may return 204 No Content with an empty body
            return r.json() if r.content else {}

    async def stop_session_by_name(self, name: str) -> dict:
        """Stop (delete) a session by an arbitrary name."""
        async with httpx.AsyncClient(headers=self.headers, timeout=15) as client:
            r = await client.delete(f"{self.base_url}/api/sessions/{name}")
            r.raise_for_status()
            return r.json() if r.content else {}

    async def get_qr(self) -> Optional[dict]:
        """Returns QR code as base64 image or None if already authenticated."""
        async with httpx.AsyncClient(headers=self.headers, timeout=10) as client:
            r = await client.get(
                f"{self.base_url}/api/{self.session_name}/auth/qr",
                params={"format": "image"}
            )
            if r.status_code == 200:
                b64 = base64.b64encode(r.content).decode()
                content_type = r.headers.get("content-type", "image/png")
                return {"data_url": f"data:{content_type};base64,{b64}"}
            return None

    async def get_me(self) -> Optional[dict]:
        """Get info about the connected WhatsApp account."""
        async with httpx.AsyncClient(headers=self.headers, timeout=10) as client:
            r = await client.get(f"{self.base_url}/api/{self.session_name}/auth/me")
            if r.status_code == 200:
                return r.json()
            return None

    async def send_text(self, phone: str, message: str) -> dict:
        """Send a text message. Phone should be in international format (e.g. 5219981234567)."""
        chat_id = self._normalize_phone(phone)
        async with httpx.AsyncClient(headers=self.headers, timeout=30) as client:
            r = await client.post(
                f"{self.base_url}/api/sendText",
                json={
                    "chatId": chat_id,
                    "text": message,
                    "session": self.session_name,
                }
            )
            r.raise_for_status()
            return r.json()

    def _normalize_phone(self, phone: str) -> str:
        """Normalize phone number to WhatsApp chat ID format."""
        digits = "".join(c for c in phone if c.isdigit())
        return f"{digits}@c.us"
