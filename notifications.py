import requests

from app_config import get_telegram_credentials


def send_telegram_message(message: str) -> None:
    token, chat_id = get_telegram_credentials()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
    except Exception as exc:
        print(f"Errore invio Telegram: {exc}")
