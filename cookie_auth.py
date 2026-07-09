import hashlib
import hmac
import json
import time

import streamlit as st
from streamlit_cookies_manager import EncryptedCookieManager

from app_config import get_env

AUTH_COOKIE_NAME = "tradebot_auth"


def _cookie_manager() -> EncryptedCookieManager:
    secret = get_env("STREAMLIT_COOKIE_SECRET", required=True)
    return EncryptedCookieManager(prefix="tradebot/", password=secret)


def _sign_payload(payload: dict, secret: str) -> str:
    message = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def _build_token(username: str, secret: str, ttl_seconds: int) -> str:
    payload = {"u": username, "exp": int(time.time()) + ttl_seconds}
    return json.dumps({"payload": payload, "sig": _sign_payload(payload, secret)}, separators=(",", ":"))


def _verify_token(token: str, secret: str) -> bool:
    try:
        parsed = json.loads(token)
        payload = parsed["payload"]
        signature = parsed["sig"]
    except Exception:
        return False

    expected = _sign_payload(payload, secret)
    if not hmac.compare_digest(signature, expected):
        return False

    return int(payload.get("exp", 0)) > int(time.time())


def require_cookie_auth() -> None:
    username = get_env("STREAMLIT_AUTH_USERNAME", required=True)
    password_hash = get_env("STREAMLIT_AUTH_PASSWORD_HASH", required=True)
    cookie_secret = get_env("STREAMLIT_COOKIE_SECRET", required=True)
    cookies = _cookie_manager()
    cookies.ready()

    existing_token = cookies.get(AUTH_COOKIE_NAME)
    if existing_token and _verify_token(existing_token, cookie_secret):
        if st.sidebar.button("Logout"):
            del cookies[AUTH_COOKIE_NAME]
            cookies.save()
            st.rerun()
        return

    st.title("Accesso dashboard")
    with st.form("streamlit_login"):
        entered_username = st.text_input("Username")
        entered_password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Accedi")

    if not submitted:
        st.stop()

    entered_hash = hashlib.sha256(entered_password.encode("utf-8")).hexdigest()
    if hmac.compare_digest(entered_username, username) and hmac.compare_digest(entered_hash, password_hash):
        cookies[AUTH_COOKIE_NAME] = _build_token(username, cookie_secret, ttl_seconds=60 * 60 * 24 * 7)
        cookies.save()
        st.rerun()

    st.error("Credenziali non valide")
    st.stop()
