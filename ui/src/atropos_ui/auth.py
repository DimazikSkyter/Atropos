"""Local account authentication; the configured legacy operator keeps its catalog."""
import getpass
import hashlib
import hmac
import os
import secrets
import time

import streamlit as st

from atropos_ui.service_api import ConfigurationError, account_hash, create_account


def password_hash(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600_000).hex()
    return f"pbkdf2_sha256$600000${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        method, iterations, salt, expected = encoded.split("$")
        count = int(iterations)
        if method != "pbkdf2_sha256" or not 100_000 <= count <= 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), count).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def login_page():
    st.title("Вход в Atropos")
    st.caption("Управление источниками, PromQL и производными метриками")
    if os.getenv("ATROPOS_UI_DEMO") == "1":
        st.warning("Деморежим: синтетические данные, запросы не отправляются в сеть.")
        if st.button("Войти в деморежим", type="primary"):
            st.session_state["identity"] = "demo"
            st.rerun()
        return
    username = os.getenv("ATROPOS_UI_USERNAME", "admin")
    encoded = os.getenv("ATROPOS_UI_PASSWORD_HASH", "")
    if not encoded:
        st.caption('Войдите в созданную учётную запись. Создание учёток доступно через config_db create-user.')
    with st.form("login"):
        login = st.text_input("Логин")
        password = st.text_input("Пароль", type="password")
        submitted = st.form_submit_button("Войти", type="primary")
    if submitted:
        if time.time() < st.session_state.get("login_retry_after", 0):
            st.error("Подождите несколько секунд перед следующей попыткой.")
        else:
            try:
                operator = (bool(encoded) and hmac.compare_digest(login.encode(), username.encode())
                            and verify_password(password, encoded))
                if operator:
                    # Only the explicitly configured legacy operator inherits its old catalog.
                    create_account(username, encoded, legacy=True)
                stored = account_hash(login)
                authenticated = operator or verify_password(password, stored or password_hash('dummy-password'))
                if authenticated:
                    st.session_state.clear()
                    st.session_state['identity'] = login
                    st.session_state['rebuild_after_login'] = True
                    st.rerun()
                else:
                    st.session_state['login_retry_after'] = time.time() + 3
                    st.error('Неверный логин или пароль.')
            except ConfigurationError:
                st.error('Не удалось открыть учётные записи. Выполните make service-up.')


if __name__ == "__main__":
    password = getpass.getpass("New UI password: ")
    if len(password) < 12:
        raise SystemExit("Use at least 12 characters.")
    print(password_hash(password))
