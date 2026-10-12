"""Simple role-based login.

Users come from Streamlit secrets (`[users.<username>]` with name, role, salt, password_hash).
If no secrets are configured, two DEMO users are used so the app works out of the box:
    planner  / planner123   (role: planner)
    approver / approver123  (role: approver)
Replace them with real users in secrets before sharing outside a demo.
Generate a hash with:  python auth.py <password>
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sys

ITERATIONS = 200_000


def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS).hex()


def _demo(username, name, role, password, salt):
    return username, {"name": name, "role": role, "salt": salt, "password_hash": hash_password(password, salt)}


DEMO_USERS = dict([
    _demo("planner", "Maria Lopez", "planner", "planner123", "a1b2c3d4e5f60718293a4b5c6d7e8f90"),
    _demo("approver", "David Brooks", "approver", "approver123", "0f1e2d3c4b5a69788796a5b4c3d2e1f0"),
])


def load_users(secrets) -> tuple[dict, bool]:
    """Return (users, is_demo)."""
    try:
        users = secrets.get("users") if secrets is not None else None
    except Exception:
        users = None
    if users:
        return {u: dict(v) for u, v in users.items()}, False
    return DEMO_USERS, True


def check_login(users: dict, username: str, password: str) -> dict | None:
    u = users.get((username or "").strip().lower())
    if not u:
        return None
    if hmac.compare_digest(hash_password(password or "", u["salt"]), u["password_hash"]):
        return {"username": username.strip().lower(), "name": u["name"], "role": u["role"]}
    return None


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python auth.py <password>")
    salt = os.urandom(16).hex()
    print(f'salt = "{salt}"\npassword_hash = "{hash_password(sys.argv[1], salt)}"')
