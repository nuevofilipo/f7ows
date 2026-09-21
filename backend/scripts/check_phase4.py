"""Phase 4 verification: local user, auto-auth, keychain, JWT secret, config.json."""
import asyncio, os, sys

PASS = FAIL = 0
def check(label, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ok   {label}")
    else:    FAIL += 1; print(f"  FAIL {label}")


def sync_checks():
    from app import keystore, paths, providers
    from app import config as cfg
    from app.config import settings, write_config_json, CONFIG_FILE

    print("JWT secret")
    f = paths.data_dir() / "jwt_secret"
    check("not present before first use", not f.exists())
    s1 = settings.jwt_secret_key
    check("generated on first read", f.is_file())
    check("64 hex chars", len(s1) == 64 and all(c in "0123456789abcdef" for c in s1))
    check("stable across reads (no regeneration)", settings.jwt_secret_key == s1)
    check("a second process adopts the same secret", keystore.jwt_secret() == s1)

    print("OS keychain")
    keystore.clear_openrouter_key()
    check("absent reads as None", keystore.get_openrouter_key() is None)
    check("clearing an absent key does not raise", keystore.clear_openrouter_key() is None)
    keystore.set_openrouter_key("sk-or-v1-abcdef0123456789")
    check("round-trips", keystore.get_openrouter_key() == "sk-or-v1-abcdef0123456789")
    check("mask hides the middle", keystore.mask("sk-or-v1-abcdef0123456789") == "sk-or…6789")
    check("mask of a short value reveals nothing", set(keystore.mask("abc")) == {"•"})
    providers._invalidate()
    check("a provider prefers the keychain over env",
          providers.for_role("chat").key() == "sk-or-v1-abcdef0123456789")
    keystore.clear_openrouter_key()
    object.__setattr__(settings, "openrouter_key", "")     # simulate no env fallback
    providers._invalidate()
    try:
        providers.for_role("chat").key(); check("no key anywhere -> MissingAPIKey", False)
    except providers.MissingAPIKey as exc:
        check("no key anywhere -> MissingAPIKey naming the fix", "Settings" in str(exc))

    print("config.json")
    p = paths.data_dir() / CONFIG_FILE
    check("an absent file is not an error", cfg._load_config_json() == {})
    write_config_json({"chat_model": "anthropic/claude-opus-5"})
    check("written and re-read", cfg._load_config_json()["chat_model"] == "anthropic/claude-opus-5")
    write_config_json({"planner_model": "x/y"})
    both = cfg._load_config_json()
    check("merges rather than replaces",
          both["chat_model"] == "anthropic/claude-opus-5" and both["planner_model"] == "x/y")
    check("no .part file left behind", not list(p.parent.glob("*.part")))
    p.write_text("{not json", encoding="utf-8")
    check("a corrupt file is ignored, not fatal", cfg._load_config_json() == {})
    p.unlink()

    # Precedence. _get reads the snapshot taken at import (settings are read once
    # per process, by design), so refresh it the way a restart would.
    cfg._file_config = {"chat_model": "file/loses"}
    os.environ["CHAT_MODEL"] = "env/wins"
    check("env beats config.json", cfg._get("CHAT_MODEL", "default") == "env/wins")
    del os.environ["CHAT_MODEL"]
    check("config.json beats the default", cfg._get("CHAT_MODEL", "default") == "file/loses")
    check("the default applies when neither is set", cfg._get("NOT_SET_ANYWHERE", "d") == "d")
    cfg._file_config = {}


async def db_checks():
    """Direct-DB checks on our own pool, before the app opens its own."""
    from app.config import settings
    from app.db import create_pool, close_pool, acquire
    from app.local_user import ensure_local_user
    import app.local_user as lu

    print("local user")
    await create_pool()
    try:
        uid = await ensure_local_user()
        check("seeded on first call", uid is not None)
        lu._cached_id = None                       # force a real second lookup
        check("idempotent — same id, no second row", await ensure_local_user() == uid)
        async with acquire() as c:
            n = await c.fetchval("SELECT count(*) FROM users")
            row = await c.fetchrow("SELECT email, hashed_password FROM users WHERE id = ?", uid)
        check("exactly one user row", n == 1)
        check("email is the configured local one", row["email"] == settings.single_user_email)
        check("password is a real argon2 hash, not a fixed value",
              row["hashed_password"].startswith("$argon2"))
    finally:
        await close_pool()
    return uid


def http_checks(uid):
    """Everything through the real app, with its own lifespan and pool."""
    from fastapi.testclient import TestClient
    from app.config import settings
    from app.main import app

    print("auto-issued auth")
    with TestClient(app) as client:
        r = client.get("/auth/me")
        check("GET /auth/me with no cookie -> 200", r.status_code == 200)
        check("resolves to the seeded local user", r.json()["id"] == uid)
        check("cookie auto-issued", settings.cookie_name in r.cookies)
        check("no secret field leaks into the response",
              not any(k.startswith("_") or "key" in k for k in r.json()))
        check("reports single_user so the UI can hide the account controls",
              r.json()["single_user"] is True)
        r2 = client.get("/settings")               # now carrying the issued cookie
        check("a protected route works on the issued cookie", r2.status_code == 200)
        check("key status is read from the keychain", r2.json()["openrouter_key_set"] is False)
        client.cookies.set(settings.cookie_name, "garbage.not.a.jwt")
        r3 = client.get("/auth/me")
        check("a corrupt cookie falls back instead of 401",
              r3.status_code == 200 and r3.json()["id"] == uid)


sync_checks()
uid = asyncio.run(db_checks())
http_checks(uid)
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
