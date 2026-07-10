import datetime
from threading import Event

import pandas as pd

from app_config import get_bool_env, get_env
from market import (
    CRYPTO_TARGETS,
    LEVERAGE,
    MAX_CONCURRENT_TRADES,
    TRADE_AMOUNT_EUR,
    get_account_balance,
    get_exchange,
    get_market_data,
)
from notifications import send_telegram_message
from news_monitor import analyze_symbol_news, get_news_block_buys_enabled, get_news_negative_threshold
from storage import append_trade_row, ensure_trade_history, load_trade_history

MODALITA_PROVA = get_bool_env("MODALITA_PROVA", False)
SOGLIA_PRELIEVO_EUR = float(get_env("SOGLIA_PRELIEVO_EUR", default="200"))
NOME_CONTO_KRAKEN = get_env("KRAKEN_WITHDRAWAL_ACCOUNT", default="revolut")


def controlla_e_preleva_profitti() -> None:
    try:
        exchange = get_exchange()
        balance = exchange.fetch_balance()
        eur_totali = float(balance["total"].get("EUR", 0))

        print(f"[Controllo Giornaliero] Saldo totale: {eur_totali} EUR")

        if eur_totali > SOGLIA_PRELIEVO_EUR:
            cifra_da_prelevare = eur_totali - SOGLIA_PRELIEVO_EUR

            if cifra_da_prelevare < 5.0:
                print("Il surplus e inferiore a 5 EUR, rinvio il prelievo a domani.")
                return

            msg_info = f"Soglia superata. Avvio prelievo automatico di: {round(cifra_da_prelevare, 2)} EUR"
            print(msg_info)
            send_telegram_message(msg_info)

            response = exchange.withdraw(
                code="EUR",
                amount=cifra_da_prelevare,
                address=NOME_CONTO_KRAKEN,
                params={},
            )

            log_trade_to_csv("EUR", "WITHDRAW", cifra_da_prelevare, current_price=1.0)

            send_telegram_message(
                f"Prelievo inviato con successo. {round(cifra_da_prelevare, 2)} EUR sono in viaggio verso il tuo conto. ID transazione: {response['id']}"
            )
        else:
            print("Saldo inferiore alla soglia di prelievo. Nessun profitto da prelevare oggi.")

    except Exception as exc:
        error_msg = f"Errore durante il prelievo automatico: {exc}"
        print(error_msg)
        send_telegram_message(error_msg)


def log_trade_to_csv(symbol: str, action: str, amount: float, price: float, stop_loss: float = 0, take_profit: float = 0) -> None:
    new_row = {
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": symbol,
        "action": action,
        "amount": amount,
        "price": price,
        "leverage": LEVERAGE,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
    }
    append_trade_row(new_row)


def get_active_trades_count() -> int:
    df = load_trade_history()
    buys = df[df["action"] == "BUY"]["symbol"].tolist()
    sells = df[df["action"] == "SELL"]["symbol"].tolist()
    for symbol in sells:
        if symbol in buys:
            buys.remove(symbol)
    return len(buys)


def get_trade_amount() -> float:
    try:
        eur_disponibili = get_account_balance()
        trade_attivi = get_active_trades_count()
        slot_disponibili = MAX_CONCURRENT_TRADES - trade_attivi

        if slot_disponibili <= 0:
            print("Nessuno slot disponibile per fare trading.")
            return 0

        trade_amount_dinamico = eur_disponibili / slot_disponibili

        if trade_amount_dinamico < 5:
            print(f"Saldo insufficiente per calcolare un trade valido ({trade_amount_dinamico} EUR).")
            return 0

        print(
            f"Saldo EUR libero: {round(eur_disponibili, 2)} EUR | Budget calcolato per questo trade: {round(trade_amount_dinamico, 2)} EUR"
        )
        return trade_amount_dinamico

    except Exception as exc:
        print(f"Errore nel recupero del saldo dinamico da Kraken: {exc}")
        return TRADE_AMOUNT_EUR


def _place_protection_order(
    exchange,
    symbol: str,
    amount: float,
    order_type: str,
    trigger_price: float,
    limit_price: float,
) -> None:
    trigger_price = float(exchange.price_to_precision(symbol, trigger_price))
    limit_price = float(exchange.price_to_precision(symbol, limit_price))

    exchange.create_order(
        symbol=symbol,
        type="limit",
        side="sell",
        amount=amount,
        price=limit_price,
        params={
            "leverage": str(LEVERAGE),
            "stopLossPrice" if order_type == "stop-loss-limit" else "takeProfitPrice": trigger_price,
        },
    )


def place_stop_loss_and_take_profit(exchange, symbol: str, amount: float, stop_loss: float, take_profit: float) -> None:
    # Kraken is stricter with trigger orders than the generic CCXT interface.
    # We use limit-based protection orders with an explicit trigger price and a
    # slightly more aggressive limit price to improve fill reliability.
    sl_limit = stop_loss * 0.999
    tp_limit = take_profit * 0.999

    _place_protection_order(exchange, symbol, amount, "stop-loss-limit", stop_loss, sl_limit)
    _place_protection_order(exchange, symbol, amount, "take-profit-limit", take_profit, tp_limit)


def check_signals() -> None:
    print(f"[{pd.Timestamp.now().strftime('%H:%M:%S')}] Analisi di mercato in corso...")

    active_count = get_active_trades_count()
    if active_count >= MAX_CONCURRENT_TRADES:
        print(f"Raggiunto il limite massimo di trade contemporanei ({active_count}/{MAX_CONCURRENT_TRADES}). Salto l'analisi.")
        return

    for symbol in CRYPTO_TARGETS:
        try:
            df_csv = load_trade_history()
            news_snapshot = analyze_symbol_news(symbol)
            news_label = news_snapshot["label"]
            news_score = float(news_snapshot["score"])

            if get_news_block_buys_enabled() and news_label == "NEGATIVE" and news_score <= get_news_negative_threshold():
                print(f"Filtro news attivo: {symbol} bloccato da sentiment negativo ({news_score:.2f}).")
                continue

            if symbol == "DOGE/EUR" and not df_csv.empty:
                doge_buys = len(df_csv[(df_csv["symbol"] == "DOGE/EUR") & (df_csv["action"] == "BUY")])
                doge_sells = len(df_csv[(df_csv["symbol"] == "DOGE/EUR") & (df_csv["action"] == "SELL")])
                doge_attivi = doge_buys - doge_sells
                if doge_attivi >= 1:
                    print("DOGE/EUR ha gia un trade attivo. Salto ulteriori segnali per contenere il rischio.")
                    continue

            df = get_market_data(symbol)
            last_row = df.iloc[-1]
            current_price = last_row["close"]

            if last_row["RSI"] < 40 and (current_price > last_row["EMA_9"] or (current_price + 0.05 >= last_row["EMA_9"] and news_label == 'POSITIVE')):
                amount_to_buy = get_trade_amount() / current_price
                if amount_to_buy <= 0:
                    continue

                exchange = get_exchange()
                amount_to_buy = float(exchange.amount_to_precision(symbol, amount_to_buy))

                stop_loss = current_price * 0.98
                take_profit = current_price * 1.04
                if MODALITA_PROVA:
                    entry_price = current_price
                    log_trade_to_csv(symbol, "BUY", amount_to_buy, entry_price, stop_loss, take_profit)

                    msg = (
                        f"[SIMULAZIONE] ORDINE COMPRA\n"
                        f"Asset: `{symbol}`\n"
                        f"Quantita: {amount_to_buy}\n"
                        f"Prezzo ingresso: {round(entry_price, 4)} EUR\n"
                        f"SL: {round(stop_loss, 4)} EUR | TP: {round(take_profit, 4)} EUR"
                    )
                    if news_label in {"POSITIVE", "NEGATIVE"}:
                        msg += f"\nSentiment news: {news_label} ({round(news_score, 2)})"
                    send_telegram_message(msg)
                else:
                    order = exchange.create_market_buy_order(
                        symbol=symbol,
                        amount=amount_to_buy,
                        params={"leverage": str(LEVERAGE) if symbol != "DOGE/EUR" else "1"},
                    )
                    entry_price = order.get("price", current_price) if order.get("price") else current_price

                    log_trade_to_csv(symbol, "BUY", amount_to_buy, entry_price, stop_loss, take_profit)

                    msg = (
                        f"ORDINE COMPRA ESEGUITO\n"
                        f"Asset: `{symbol}`\n"
                        f"Quantita: {amount_to_buy}\n"
                        f"Prezzo ingresso: {round(entry_price, 4)} EUR\n"
                        f"Stop Loss: {round(stop_loss, 4)} EUR\n"
                        f"Take Profit: {round(take_profit, 4)} EUR"
                    )
                    if news_label in {"POSITIVE", "NEGATIVE"}:
                        msg += f"\nSentiment news: {news_label} ({round(news_score, 2)})"
                    send_telegram_message(msg)

                    try:
                        filled_amount = float(order.get("filled") or amount_to_buy)
                        place_stop_loss_and_take_profit(exchange, symbol, filled_amount, stop_loss, take_profit)
                        send_telegram_message("Protezioni attivate. Stop Loss e Take Profit impostati correttamente su Kraken.")
                    except Exception as exc:
                        send_telegram_message(
                            f"Ordine eseguito ma errore nel piazzare SL/TP automatici: {exc}"
                        )

        except Exception as exc:
            print(f"Errore durante l'analisi o l'ordine su {symbol}: {exc}")


def run_bot(stop_event: Event | None = None, poll_seconds: int = 300) -> None:
    ensure_trade_history()

    ultimo_controllo_prelievo = None
    check_signals()

    ora_attuale = datetime.datetime.now()
    if ora_attuale.hour == 23 and ultimo_controllo_prelievo != ora_attuale.date():
        controlla_e_preleva_profitti()


if __name__ == "__main__":
    run_bot()
