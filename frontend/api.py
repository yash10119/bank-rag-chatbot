"""Thin HTTP client for the banking API. All state (token, user) lives in st.session_state,
which Streamlit scopes to one browser session, so users never share credentials."""
import json
import os
from collections.abc import Iterator

import httpx

API_URL = os.environ.get("API_URL", "http://localhost:8000")


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _raise(r: httpx.Response) -> None:
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except Exception:
            detail = r.text
        if isinstance(detail, list):  # pydantic validation errors
            detail = "; ".join(d.get("msg", "invalid input") for d in detail)
        raise ApiError(r.status_code, str(detail))


class Api:
    def __init__(self, token: str | None = None):
        self.token = token

    @property
    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def request(self, method: str, path: str, **kw) -> object:
        try:
            r = httpx.request(method, f"{API_URL}{path}", headers=self._h, timeout=kw.pop("timeout", 30), **kw)
        except httpx.HTTPError as exc:
            raise ApiError(0, f"Cannot reach the banking service ({type(exc).__name__}). Is the API running?") from exc
        _raise(r)
        return r.json()

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, **kw):
        return self.request("POST", path, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)

    def patch(self, path, **kw):
        return self.request("PATCH", path, **kw)

    def stream_message(self, conversation_id: str, text: str) -> Iterator[dict]:
        """Yields SSE events: step | token | final | interrupt | error."""
        try:
            with httpx.stream(
                "POST", f"{API_URL}/conversations/{conversation_id}/messages", headers=self._h,
                json={"text": text}, timeout=httpx.Timeout(300, connect=10),
            ) as r:
                if r.status_code >= 400:
                    r.read()
                    _raise(r)
                for line in r.iter_lines():
                    if line.startswith("data: "):
                        yield json.loads(line[6:])
        except httpx.HTTPError as exc:
            raise ApiError(0, f"Connection to the banking service was lost ({type(exc).__name__}).") from exc
