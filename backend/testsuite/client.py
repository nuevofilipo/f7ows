"""Thin HTTP client — the same calls the frontend will make (cookie auth included).

Deliberately knows nothing about backend internals; it only speaks HTTP, so the
traces it triggers are exactly what a browser session would produce.
"""
from __future__ import annotations

import time
from uuid import uuid4

import httpx

from testsuite.config import (BASE_URL, TEST_EMAIL, TEST_PASSWORD, TEST_PROJECT_NAME)


class ApiError(RuntimeError):
    pass


class Api:
    def __init__(self, base_url: str = BASE_URL) -> None:
        # httpx.Client keeps the auth cookie in its jar across requests.
        self.http = httpx.Client(base_url=base_url, timeout=120.0)

    # -- low-level ------------------------------------------------------------
    def _json(self, resp: httpx.Response):
        if resp.status_code >= 400:
            raise ApiError(f"{resp.request.method} {resp.request.url} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json() if resp.content else None

    # -- auth -----------------------------------------------------------------
    def ensure_user(self) -> dict:
        """Register the test user (idempotent) then log in to set the cookie."""
        self.http.post("/auth/register", json={"email": TEST_EMAIL, "password": TEST_PASSWORD})
        return self._json(self.http.post("/auth/login", json={"email": TEST_EMAIL, "password": TEST_PASSWORD}))

    # -- projects -------------------------------------------------------------
    def ensure_project(self, name: str = TEST_PROJECT_NAME) -> dict:
        for p in self._json(self.http.get("/projects")) or []:
            if p["name"] == name:
                return p
        return self._json(self.http.post("/projects", json={"name": name}))

    # -- documents ------------------------------------------------------------
    def upload_pdfs(self, project_id: str, paths: list) -> dict:
        files = [("files", (p.name, p.read_bytes(), "application/pdf")) for p in paths]
        return self._json(self.http.post(f"/projects/{project_id}/documents/batch", files=files))

    def list_documents(self, project_id: str) -> list[dict]:
        return self._json(self.http.get(f"/projects/{project_id}/documents")) or []

    def wait_until_ready(self, project_id: str, *, timeout: float = 600.0, interval: float = 3.0,
                         on_tick=None) -> list[dict]:
        """Poll documents until every one is terminal (ready/error)."""
        deadline = time.time() + timeout
        while True:
            docs = self.list_documents(project_id)
            states = {d["id"]: (d["status"] or {}).get("state") for d in docs}
            if on_tick:
                on_tick(docs)
            if docs and all(s in ("ready", "error") for s in states.values()):
                return docs
            if time.time() > deadline:
                raise ApiError(f"documents not ready in {timeout}s: {states}")
            time.sleep(interval)

    # -- chat -----------------------------------------------------------------
    def send_message(self, project_id: str, chat_id: str, content: str, *,
                     command: str | None = None, parent_path: str | None = None,
                     intent: str | None = None, settings: dict | None = None) -> dict:
        body: dict = {"content": content}
        for key, val in (("command", command), ("parent_path", parent_path),
                         ("intent", intent), ("settings", settings)):
            if val is not None:
                body[key] = val
        return self._json(self.http.post(f"/projects/{project_id}/chats/{chat_id}/messages", json=body))

    def list_messages(self, project_id: str, chat_id: str) -> list[dict]:
        return self._json(self.http.get(f"/projects/{project_id}/chats/{chat_id}/messages")) or []

    def wait_for_message(self, project_id: str, chat_id: str, message_id: str, *,
                         timeout: float = 600.0, interval: float = 2.0) -> dict:
        """Poll a (background) assistant message until metadata.state is terminal."""
        deadline = time.time() + timeout
        while True:
            for m in self.list_messages(project_id, chat_id):
                if m["id"] == message_id:
                    state = (m.get("metadata") or {}).get("state")
                    if state in ("ready", "error"):
                        return m
            if time.time() > deadline:
                raise ApiError(f"message {message_id} not ready in {timeout}s")
            time.sleep(interval)

    @staticmethod
    def new_chat_id() -> str:
        return str(uuid4())

    def close(self) -> None:
        self.http.close()
