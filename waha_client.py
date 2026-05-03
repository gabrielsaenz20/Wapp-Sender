import base64
import httpx
import asyncio
import random
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
        """Create (and start) a session.

        If the session already exists (WAHA returns 422) we fall back to
        ``POST /api/sessions/{name}/start`` so that a stopped/disconnected
        session can be restarted without deleting and re-creating it.
        """
        async with httpx.AsyncClient(headers=self.headers, timeout=15) as client:
            r = await client.post(
                f"{self.base_url}/api/sessions",
                json={"name": name}
            )
            if r.status_code == 422:
                # Session already exists – restart it instead
                r2 = await client.post(f"{self.base_url}/api/sessions/{name}/start")
                r2.raise_for_status()
                return r2.json() if r2.content else {}
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

    async def send_seen(self, phone: str) -> None:
        """Send a read receipt (mark chat as seen) for a contact."""
        chat_id = self._normalize_phone(phone)
        async with httpx.AsyncClient(headers=self.headers, timeout=10) as client:
            try:
                r = await client.post(
                    f"{self.base_url}/api/sendSeen",
                    json={
                        "chatId": chat_id,
                        "session": self.session_name,
                    }
                )
                # WAHA may return 404/422 if endpoint is unavailable in the
                # installed plan – ignore failures so sending still proceeds.
                if r.status_code not in (200, 201, 204):
                    return
            except Exception:
                return

    async def start_typing(self, phone: str) -> None:
        """Start the typing indicator for a contact chat."""
        chat_id = self._normalize_phone(phone)
        async with httpx.AsyncClient(headers=self.headers, timeout=10) as client:
            try:
                r = await client.post(
                    f"{self.base_url}/api/startTyping",
                    json={
                        "chatId": chat_id,
                        "session": self.session_name,
                    }
                )
                if r.status_code not in (200, 201, 204):
                    return
            except Exception:
                return

    async def stop_typing(self, phone: str) -> None:
        """Stop the typing indicator for a contact chat."""
        chat_id = self._normalize_phone(phone)
        async with httpx.AsyncClient(headers=self.headers, timeout=10) as client:
            try:
                r = await client.post(
                    f"{self.base_url}/api/stopTyping",
                    json={
                        "chatId": chat_id,
                        "session": self.session_name,
                    }
                )
                if r.status_code not in (200, 201, 204):
                    return
            except Exception:
                return

    async def send_text_humanized(
        self,
        phone: str,
        message: str,
        *,
        min_delay: float = 5.0,
        max_delay: float = 20.0,
        typing_cpm: float = 200.0,
        seen_probability: float = 0.85,
        post_seen_min_delay: float = 0.5,
        post_seen_max_delay: float = 2.5,
        thinking_min_pause: float = 1.0,
        thinking_max_pause: float = 4.0,
        post_typing_min_pause: float = 0.3,
        post_typing_max_pause: float = 1.2,
        min_typing_seconds: float = 1.5,
        max_typing_seconds: float = 25.0,
    ) -> dict:
        """Send a text message with human-like behaviour.

        Sequence:
          1. Random pre-message pause (simulates finishing reading the previous chat).
          2. Optionally send a "seen" read receipt (controlled by *seen_probability*).
          3. Short thinking pause before starting to type.
          4. Start typing indicator.
          5. Wait for a duration proportional to the message length at *typing_cpm*
             characters per minute, with ±20 % jitter.
          6. Stop typing (brief "reviewing what I wrote" pause).
          7. Send the actual message.

        All delays are randomised so that traffic patterns are not uniform and
        therefore harder to fingerprint.

        Args:
            phone: Phone number in international format.
            message: Text message to send.
            min_delay: Minimum seconds to wait before starting the sequence.
            max_delay: Maximum seconds to wait before starting the sequence.
            typing_cpm: Characters-per-minute used to estimate typing duration.
            seen_probability: Probability (0–1) of sending a read receipt first.
            post_seen_min_delay: Min pause (s) after sending the read receipt.
            post_seen_max_delay: Max pause (s) after sending the read receipt.
            thinking_min_pause: Min "thinking" pause (s) before starting to type.
            thinking_max_pause: Max "thinking" pause (s) before starting to type.
            post_typing_min_pause: Min pause (s) after stopping the typing indicator.
            post_typing_max_pause: Max pause (s) after stopping the typing indicator.
            min_typing_seconds: Lower clamp (s) for the typing indicator duration.
            max_typing_seconds: Upper clamp (s) for the typing indicator duration.
        """
        # 1. Pre-message pause – randomised between min_delay and max_delay.
        pre_delay = random.uniform(min_delay, max_delay)
        await asyncio.sleep(pre_delay)

        # 2. Optionally mark the chat as "read" before replying.
        if random.random() < seen_probability:
            await self.send_seen(phone)
            # Brief pause after reading, before starting to type.
            await asyncio.sleep(random.uniform(post_seen_min_delay, post_seen_max_delay))

        # 3. Short thinking pause (deciding what to write).
        thinking_pause = random.uniform(thinking_min_pause, thinking_max_pause)
        await asyncio.sleep(thinking_pause)

        # 4. Start typing indicator.
        await self.start_typing(phone)

        # 5. Simulate typing duration based on message length.
        #    typing_cpm is characters per minute; add ±20 % jitter.
        char_count = max(len(message), 1)
        base_typing_seconds = (char_count / typing_cpm) * 60.0
        jitter = random.uniform(0.80, 1.20)
        typing_duration = base_typing_seconds * jitter
        # Clamp to a reasonable range so it never looks instant or suspiciously long.
        typing_duration = max(min_typing_seconds, min(typing_duration, max_typing_seconds))
        await asyncio.sleep(typing_duration)

        # 6. Stop typing – micro-pause before hitting "send".
        await self.stop_typing(phone)
        await asyncio.sleep(random.uniform(post_typing_min_pause, post_typing_max_pause))

        # 7. Send the actual message.
        return await self.send_text(phone, message)

    def _normalize_phone(self, phone: str) -> str:
        """Normalize phone number to WhatsApp chat ID format."""
        digits = "".join(c for c in phone if c.isdigit())
        return f"{digits}@c.us"
