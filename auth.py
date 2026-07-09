import hashlib
import hmac

import streamlit as st

from app_config import get_env


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def require_streamlit_auth() -> None:
    username = get_env("STREAMLIT_AUTH_USERNAME", default="")
    password_hash = get_env("STREAMLIT_AUTH_PASSWORD_HASH", default="")

    if not username or not password_hash:
        st.warning("Streamlit auth non configurata. La dashboard e pubblica.")
        return

    if st.session_state.get("authenticated"):
        if st.sidebar.button("Logout"):
            st.session_state["authenticated"] = False
            st.rerun()
        return

    st.title("Accesso dashboard")
    with st.form("streamlit_login"):
        entered_username = st.text_input("Username")
        entered_password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Accedi")

    if not submitted:
        st.stop()

    entered_hash = _hash_password(entered_password)
    username_ok = hmac.compare_digest(entered_username, username)
    password_ok = hmac.compare_digest(entered_hash, password_hash)

    if username_ok and password_ok:
        st.session_state["authenticated"] = True
        st.rerun()

    st.error("Credenziali non valide")
    st.stop()
