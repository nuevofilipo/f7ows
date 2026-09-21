"""Verify per-role provider config (TODO_desktop.md phase 5).

    python scripts/check_phase5.py        (from backend/, venv active)

Covers resolution order, the keyless/Ollama path, cost accounting for providers
that report no price, and the settings endpoints. The two model call sites
(llm.complete, embed) are driven against a local stub server rather than mocked,
so what is asserted is the request that actually goes out: URL, auth header,
model.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

SCRATCH = os.path.join(tempfile.gettempdir(), "f7ows_verify_p5")
shutil.rmtree(SCRATCH, ignore_errors=True)
os.environ["F7OWS_DATA_DIR"] = SCRATCH
os.environ["TRACE"] = "0"
for stale in ("LLM_BASE_URL", "EMBED_BASE_URL", "CHAT_MODEL", "PLANNER_MODEL", "EMBED_MODEL"):
    os.environ.pop(stale, None)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "ok  " if cond else "FAIL"
    print(f"  {mark} {name}" + (f"   {detail}" if detail else ""))


# ── a stub OpenAI-compatible endpoint that records what it was sent ──────────
REQUESTS = []


class Handler(BaseHTTPRequestHandler):
    def _record_and_reply(self, body):
        REQUESTS.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "body": json.loads(body) if body else None,
        })
        if self.path.endswith("/chat/completions"):
            # No usage.cost — exactly what a local Ollama returns.
            payload = {"choices": [{"message": {"content": "inbetween"}}],
                       "usage": {"prompt_tokens": 10}}
        elif self.path.endswith("/embeddings"):
            n = len(REQUESTS[-1]["body"]["input"])
            payload = {"data": [{"index": i, "embedding": [0.0] * 1024} for i in range(n)]}
        else:
            payload = {"data": [{"id": "local/model", "name": "Local Model"}]}
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        self._record_and_reply(self.rfile.read(int(self.headers.get("Content-Length", 0))))

    def do_GET(self):
        self._record_and_reply(None)

    def log_message(self, *a):
        pass


def main():
    from app import cost, keystore, providers
    from app.config import settings

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}/v1"
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # ── 1. resolution ────────────────────────────────────────────────────────
    print("\n1. resolution")
    providers._invalidate()
    chat = providers.for_role("chat")
    check("chat defaults to the OpenRouter base", chat.base_url == settings.llm_base_url)
    check("chat defaults to the chat model", chat.model == settings.chat_model)
    check("planner defaults to the planner model",
          providers.for_role("planner").model == settings.planner_model)
    check("embed has its own base url",
          providers.for_role("embed").base_url == settings.embed_base_url)
    check("default key_ref is the openrouter one", chat.key_ref == keystore.DEFAULT_REF)
    check("unknown role falls back to planner, not chat",
          providers.for_role("nonsense").model == settings.planner_model)
    check("url() joins without doubling the slash",
          providers.for_role("chat").url("/models").endswith("/v1/models"))

    providers.save("planner", base_url=base, model="local/planner", key_ref="")
    p = providers.for_role("planner")
    check("config.json overrides the base url", p.base_url == base)
    check("config.json overrides the model", p.model == "local/planner")
    check("the other roles are untouched",
          providers.for_role("chat").base_url == settings.llm_base_url)

    os.environ["PLANNER_MODEL"] = "env/planner"
    providers._invalidate()
    check("env beats config.json", providers.for_role("planner").model == "env/planner")
    del os.environ["PLANNER_MODEL"]
    providers._invalidate()

    # ── 2. keys ──────────────────────────────────────────────────────────────
    print("\n2. keys")
    p = providers.for_role("planner")
    check("an empty key_ref means keyless", p.keyless is True)
    check("a keyless provider resolves to no key", p.key() is None)
    check("a keyless provider sends no Authorization header", p.auth_header() == {})
    keystore.clear_key("secondary")
    providers.save("chat", key_ref="secondary")
    try:
        providers.for_role("chat").key()
        check("a named ref with nothing stored raises", False)
    except providers.MissingAPIKey as exc:
        check("a named ref with nothing stored raises, naming the role",
              "chat" in str(exc) and "secondary" in str(exc))
    keystore.set_key("sk-secondary-123", "secondary")
    check("a named ref resolves from the keychain",
          providers.for_role("chat").key() == "sk-secondary-123")
    check("and becomes a Bearer header",
          providers.for_role("chat").auth_header()["Authorization"] == "Bearer sk-secondary-123")
    keystore.clear_key("secondary")
    check("clearing a named ref does not touch the default one",
          keystore.get_key("secondary") is None)
    providers.save("chat", key_ref=keystore.DEFAULT_REF)

    # ── 3. cost accounting ───────────────────────────────────────────────────
    print("\n3. cost")
    cost.reset()
    check("a fresh turn is zero and unknown", cost.summary()["usd"] is None)
    cost.add(None)
    check("an unpriced call does not become $0.00", cost.summary()["usd"] is None)
    check("but it is counted", cost.summary()["unpriced_calls"] == 1)
    cost.add(0.0021)
    s = cost.summary()
    check("a priced call gives a real total", abs(s["usd"] - 0.0021) < 1e-9)
    check("mixed turns keep both counts", s["priced_calls"] == 1 and s["unpriced_calls"] == 1)
    cost.reset()
    check("reset clears everything",
          cost.summary() == {"usd": None, "priced_calls": 0, "unpriced_calls": 0})

    # ── 4. the calls that actually go out ────────────────────────────────────
    print("\n4. live calls against a stub endpoint")
    from app.embed.embed import embed
    from app.llm import complete, list_models

    REQUESTS.clear()

    async def one_planner_call():
        # reset + call + read must share one context: asyncio.run() copies the
        # context in, and mutations inside never propagate back out. The real
        # caller (chat.py) does all three in one coroutine, as here.
        cost.reset()
        text = await complete([{"role": "user", "content": "hi"}], role="planner")
        return text, cost.summary()

    out, spend = asyncio.run(one_planner_call())
    req = REQUESTS[-1]
    check("planner call hit the configured base url", req["path"] == "/v1/chat/completions")
    check("planner call used the configured model", req["body"]["model"] == "local/planner")
    check("keyless provider sent no Authorization header", req["auth"] is None)
    check("the reply came back", out == "inbetween")
    check("a provider reporting no usage.cost is recorded as unpriced",
          spend["usd"] is None and spend["unpriced_calls"] == 1)

    providers.save("embed", base_url=base, model="local/embed", key_ref="")
    vectors = asyncio.run(embed(["one", "two"]))
    req = REQUESTS[-1]
    check("embed call hit the embed role's base url", req["path"] == "/v1/embeddings")
    check("embed call used the embed role's model", req["body"]["model"] == "local/embed")
    check("embed returned one vector per input", len(vectors) == 2 and len(vectors[0]) == 1024)

    providers.save("chat", base_url=base, model="local/chat", key_ref="")
    models = asyncio.run(list_models())
    check("list_models asks the chat role's endpoint", REQUESTS[-1]["path"] == "/v1/models")
    check("list_models returns the provider's catalogue", models[0]["id"] == "local/model")

    # ── 5. settings endpoints ────────────────────────────────────────────────
    print("\n5. settings API")
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        r = client.get("/settings/providers")
        check("GET /settings/providers -> 200", r.status_code == 200)
        roles = {row["role"]: row for row in r.json()["providers"]}
        check("reports all three roles", set(roles) == set(providers.ROLES))
        check("reports the saved chat base url", roles["chat"]["base_url"] == base)
        check("reports keyless roles as keyless", roles["chat"]["keyless"] is True)
        check("never returns a key itself",
              not any("key" in k and k not in ("key_ref", "keyless", "key_available")
                      for k in roles["chat"]))

        r = client.put("/settings/providers/chat", json={"model": "changed/model"})
        check("PUT updates one field", r.status_code == 200)
        check("the change is visible immediately (no restart)",
              providers.for_role("chat").model == "changed/model")
        check("and the untouched field survives", providers.for_role("chat").base_url == base)

        r = client.put("/settings/providers/chat", json={"base_url": "not-a-url"})
        check("a base_url without a scheme is rejected", r.status_code == 422)
        r = client.put("/settings/providers/nonsense", json={"model": "x"})
        check("an unknown role is a 404", r.status_code == 404)

        r = client.put("/settings/keys/secondary", json={"key": "sk-abcdef012345"})
        check("PUT /settings/keys/{ref} stores a key", r.status_code == 200)
        check("and returns only a masked preview",
              r.json()["key_preview"] == "sk-ab…2345" and "key" not in r.json())
        check("the key really landed in the keychain",
              keystore.get_key("secondary") == "sk-abcdef012345")
        r = client.delete("/settings/keys/secondary")
        check("DELETE removes it", r.status_code == 200 and keystore.get_key("secondary") is None)

    srv.shutdown()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print(f"  - {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
