"""End-to-end smoke test of the auth flow against the real database.

Cleans up the test user at the end so the script is re-runnable.
"""
import asyncio
import sys

import asyncpg
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

TEST_EMAIL = "smoke-test@example.com"
TEST_PASSWORD = "correcthorsebatterystaple"


async def _cleanup() -> None:
    conn = await asyncpg.connect(settings.postgres_url)
    try:
        await conn.execute("DELETE FROM users WHERE email = $1", TEST_EMAIL)
    finally:
        await conn.close()


def _expect(condition: bool, label: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    if not condition:
        sys.exit(1)


def main() -> None:
    asyncio.run(_cleanup())  # start clean

    with TestClient(app) as client:
        print("1. Register new user")
        r = client.post(
            "/auth/register",
            json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        _expect(r.status_code == 201, f"register returns 201 (got {r.status_code}: {r.text})")
        _expect(r.json()["email"] == TEST_EMAIL, "register returns correct email")
        _expect(settings.cookie_name in r.cookies, "register sets auth cookie")
        user_id = r.json()["id"]

        print("2. Duplicate registration is rejected")
        r = client.post(
            "/auth/register",
            json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        _expect(r.status_code == 409, f"duplicate register returns 409 (got {r.status_code})")

        print("3. /auth/me works while authenticated")
        r = client.get("/auth/me")
        _expect(r.status_code == 200, f"/me returns 200 (got {r.status_code})")
        _expect(r.json()["id"] == user_id, "/me returns the registered user")

        print("4. Logout clears cookie")
        r = client.post("/auth/logout")
        _expect(r.status_code == 204, f"logout returns 204 (got {r.status_code})")
        client.cookies.clear()

        print("5. /auth/me without cookie is 401")
        r = client.get("/auth/me")
        _expect(r.status_code == 401, f"/me without cookie returns 401 (got {r.status_code})")

        print("6. Login with wrong password is 401")
        r = client.post(
            "/auth/login",
            json={"email": TEST_EMAIL, "password": "wrong-password"},
        )
        _expect(r.status_code == 401, f"bad-password login returns 401 (got {r.status_code})")

        print("7. Login with correct password works")
        r = client.post(
            "/auth/login",
            json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        _expect(r.status_code == 200, f"login returns 200 (got {r.status_code})")
        _expect(settings.cookie_name in r.cookies, "login sets auth cookie")

        print("8. /auth/me works after login")
        r = client.get("/auth/me")
        _expect(r.status_code == 200 and r.json()["id"] == user_id, "/me returns same user after re-login")

        print("9. Tampered cookie is rejected")
        client.cookies.set(settings.cookie_name, "not.a.valid.jwt")
        r = client.get("/auth/me")
        _expect(r.status_code == 401, f"bad token returns 401 (got {r.status_code})")

    asyncio.run(_cleanup())
    print("\nAll auth checks passed.")


if __name__ == "__main__":
    main()
