import datetime
from threading import Event

import pandas as pd

from app_config import get_bool_env, get_env
from market import (
    DEFAULT_STOP_LOSS_MULTIPLIER,
    DEFAULT_TAKE_PROFIT_MULTIPLIER,
    LEVERAGE,
    MAX_CONCURRENT_TRADES,
    TRADE_AMOUNT_EUR,
    get_account_balance,
    get_all_candidate_symbols,
    get_exchange,
    get_risk_multipliers,
    get_volatility_pct,
    has_open_position,
    get_open_position_counts,
    get_open_positions_count,
    get_market_data,
    is_positive_trend,
)
from dashboard_snapshot import publish_dashboard_snapshot
from notifications import send_telegram_message
from news_monitor import analyze_symbol_news, get_news_block_buys_enabled, get_news_negative_threshold
from storage import append_trade_row, ensure_trade_history, has_recent_event, log_protection_rejection

MODALITA_PROVA = get_bool_env("MODALITA_PROVA", False)
SOGLIA_PRELIEVO_EUR = float(get_env("SOGLIA_PRELIEVO_EUR", default="200"))
NOME_CONTO_KRAKEN = get_env("KRAKEN_WITHDRAWAL_ACCOUNT", default="revolut")

SYMBOL_RISK_MULTIPLIERS = {
    "XRP/EUR": {
        "stop_loss": 0.85,
        "take_profit": 1.15,
    }
}


def get_risk_levels(symbol: str, current_price: float, volatility_pct: float | None = None) -> tuple[float, float]:
    multipliers = SYMBOL_RISK_MULTIPLIERS.get(symbol, {})
    if multipliers:
        stop_loss_multiplier = multipliers.get("stop_loss", DEFAULT_STOP_LOSS_MULTIPLIER)
        take_profit_multiplier = multipliers.get("take_profit", DEFAULT_TAKE_PROFIT_MULTIPLIER)
    elif volatility_pct is not None:
        stop_loss_multiplier, take_profit_multiplier = get_risk_multipliers(volatility_pct)
    else:
        stop_loss_multiplier = DEFAULT_STOP_LOSS_MULTIPLIER
        take_profit_multiplier = DEFAULT_TAKE_PROFIT_MULTIPLIER
    return current_price * stop_loss_multiplier, current_price * take_profit_multiplier


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


def log_trade_to_csv(
    symbol: str,
    action: str,
    amount: float,
    price: float,
    stop_loss: float = 0,
    take_profit: float = 0,
    order_id: str | None = None,
    trade_id: str | None = None,
) -> None:
    new_row = {
        "source": "LOCAL",
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": symbol,
        "action": action,
        "amount": amount,
        "price": price,
        "leverage": LEVERAGE,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "order_id": order_id,
        "trade_id": trade_id,
    }
    append_trade_row(new_row)


def get_trade_amount(
    eur_disponibili: float | None = None,
    trade_attivi: int | None = None,
) -> float:
    try:
        if MODALITA_PROVA:
            if trade_attivi is not None and trade_attivi >= MAX_CONCURRENT_TRADES:
                print("Nessuno slot disponibile per fare trading.")
                return 0
            print(f"Budget fisso in modalita prova: {TRADE_AMOUNT_EUR} EUR per posizione.")
            return TRADE_AMOUNT_EUR

        if eur_disponibili is None:
            eur_disponibili = get_account_balance()
        if trade_attivi is None:
            trade_attivi = get_open_positions_count()
        slot_disponibili = MAX_CONCURRENT_TRADES - trade_attivi

        if slot_disponibili <= 0:
            print("Nessuno slot disponibile per fare trading.")
            return 0

        if eur_disponibili < TRADE_AMOUNT_EUR:
            print(
                f"Saldo EUR libero: {round(eur_disponibili, 2)} EUR | Budget fisso richiesto: {TRADE_AMOUNT_EUR} EUR"
            )
            return 0

        print(
            f"Saldo EUR libero: {round(eur_disponibili, 2)} EUR | Budget fisso per questo trade: {TRADE_AMOUNT_EUR} EUR"
        )
        return TRADE_AMOUNT_EUR

    except Exception as exc:
        print(f"Errore nel recupero del saldo dinamico da Kraken: {exc}")
        return 0


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


def _is_protection_order(order: dict) -> bool:
    if str(order.get("side", "")).lower() != "sell":
        return False

    if order.get("stopLossPrice") or order.get("takeProfitPrice"):
        return True

    info = order.get("info") or {}
    descr = info.get("descr") if isinstance(info.get("descr"), dict) else {}
    ordertype = str(descr.get("ordertype") or info.get("ordertype") or order.get("type") or "").lower()
    return "stop" in ordertype or "take-profit" in ordertype or "take_profit" in ordertype


def cancel_existing_protection_orders(exchange, symbol: str) -> int:
    cancelled = 0

    try:
        open_orders = exchange.fetch_open_orders(symbol)
    except Exception as exc:
        print(f"Impossibile leggere gli ordini aperti su {symbol}: {exc}")
        return 0

    for order in open_orders:
        if not _is_protection_order(order):
            continue
        try:
            exchange.cancel_order(order["id"], symbol)
            cancelled += 1
        except Exception as exc:
            print(f"Impossibile cancellare l'ordine di protezione {order.get('id')} su {symbol}: {exc}")

    return cancelled


def place_stop_loss_and_take_profit(exchange, symbol: str, amount: float, stop_loss: float, take_profit: float) -> None:
    # Kraken is stricter with trigger orders than the generic CCXT interface.
    # We use limit-based protection orders with an explicit trigger price and a
    # slightly more aggressive limit price to improve fill reliability.
    removed = cancel_existing_protection_orders(exchange, symbol)
    if removed:
        print(f"Rimossi {removed} ordini di protezione duplicati su {symbol}.")

    sl_limit = stop_loss * 0.999
    tp_limit = take_profit * 0.999

    _place_protection_order(exchange, symbol, amount, "stop-loss-limit", stop_loss, sl_limit)
    _place_protection_order(exchange, symbol, amount, "take-profit-limit", take_profit, tp_limit)


def check_signals() -> None:
    print(f"[{pd.Timestamp.now().strftime('%H:%M:%S')}] Analisi di mercato in corso...")

    try:
        open_position_counts = get_open_position_counts()
        active_count = sum(open_position_counts.values())
    except Exception as exc:
        print(f"Errore nel recupero delle posizioni aperte da Kraken: {exc}")
        return

    if active_count >= MAX_CONCURRENT_TRADES:
        print(f"Raggiunto il limite massimo di trade contemporanei ({active_count}/{MAX_CONCURRENT_TRADES}). Salto l'analisi.")
        return

    eur_disponibili = TRADE_AMOUNT_EUR if MODALITA_PROVA else get_account_balance()
    slots_used = active_count

    trading_targets = get_all_candidate_symbols()
    print(f"Asset candidati: {', '.join(trading_targets)}")

    for symbol in trading_targets:
        if slots_used >= MAX_CONCURRENT_TRADES:
            print(f"Raggiunto il limite massimo di trade contemporanei ({slots_used}/{MAX_CONCURRENT_TRADES}). Interrompo la scansione.")
            break

        try:
            df = get_market_data(symbol)
            if not is_positive_trend(df):
                print(f"{symbol} escluso: trend non positivo.")
                continue

            news_snapshot = analyze_symbol_news(symbol)
            news_label = news_snapshot["label"]
            news_score = float(news_snapshot["score"])

            if get_news_block_buys_enabled() and news_label == "NEGATIVE" and news_score <= get_news_negative_threshold():
                print(f"Filtro news attivo: {symbol} bloccato da sentiment negativo ({news_score:.2f}).")
                continue

            if has_open_position(symbol, counts=open_position_counts):
                print(f"{symbol} ha gia una posizione aperta. Salto ulteriori segnali per evitare sovrapposizioni.")
                continue

            if has_recent_event(symbol, "PROTECTION_REJECTED", hours=24):
                print(f"Protezione rifiutata nelle ultime 24 ore per {symbol}. Salto il trade per evitare retry continui.")
                continue

            last_row = df.iloc[-1]
            current_price = last_row["close"]

            if last_row["RSI"] < 40 and (current_price > last_row["EMA_9"] or (current_price >= last_row["EMA_9"] and news_label == 'POSITIVE')):
                amount_to_buy = get_trade_amount(eur_disponibili=eur_disponibili, trade_attivi=slots_used) / current_price
                if amount_to_buy <= 0:
                    continue

                exchange = get_exchange()
                amount_to_buy = float(exchange.amount_to_precision(symbol, amount_to_buy))

                volatility_pct = get_volatility_pct(df)
                stop_loss, take_profit = get_risk_levels(symbol, current_price, volatility_pct)
                sl_pct = round((stop_loss / current_price - 1) * 100, 2)
                tp_pct = round((take_profit / current_price - 1) * 100, 2)
                if MODALITA_PROVA:
                    entry_price = current_price
                    log_trade_to_csv(symbol, "BUY", amount_to_buy, entry_price, stop_loss, take_profit)
                    slots_used += 1
                    open_position_counts[symbol] = open_position_counts.get(symbol, 0) + 1

                    msg = (
                        f"[SIMULAZIONE] ORDINE COMPRA\n"
                        f"Asset: `{symbol}`\n"
                        f"Quantita: {amount_to_buy}\n"
                        f"Prezzo ingresso: {round(entry_price, 4)} EUR\n"
                        f"SL: {round(stop_loss, 4)} EUR ({sl_pct}%) | TP: {round(take_profit, 4)} EUR ({tp_pct}%)\n"
                        f"Volatilita ATR: {round(volatility_pct, 2)}%"
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
                    order_id = order.get("id")

                    log_trade_to_csv(symbol, "BUY", amount_to_buy, entry_price, stop_loss, take_profit, order_id=order_id)

                    msg = (
                        f"ORDINE COMPRA ESEGUITO\n"
                        f"Asset: `{symbol}`\n"
                        f"Quantita: {amount_to_buy}\n"
                        f"Prezzo ingresso: {round(entry_price, 4)} EUR\n"
                        f"Stop Loss: {round(stop_loss, 4)} EUR ({sl_pct}%)\n"
                        f"Take Profit: {round(take_profit, 4)} EUR ({tp_pct}%)\n"
                        f"Volatilita ATR: {round(volatility_pct, 2)}%"
                    )
                    if news_label in {"POSITIVE", "NEGATIVE"}:
                        msg += f"\nSentiment news: {news_label} ({round(news_score, 2)})"
                    send_telegram_message(msg)

                    slots_used += 1
                    open_position_counts[symbol] = open_position_counts.get(symbol, 0) + 1
                    if eur_disponibili is not None:
                        eur_disponibili = max(0.0, eur_disponibili - TRADE_AMOUNT_EUR)

                    try:
                        filled_amount = float(order.get("filled") or amount_to_buy)
                        place_stop_loss_and_take_profit(exchange, symbol, filled_amount, stop_loss, take_profit)
                        send_telegram_message("Protezioni attivate. Stop Loss e Take Profit impostati correttamente su Kraken.")
                    except Exception as exc:
                        log_protection_rejection(symbol, str(exc), amount_to_buy, entry_price)
                        send_telegram_message(
                            f"Ordine eseguito ma errore nel piazzare SL/TP automatici: {exc}"
                        )

        except Exception as exc:
            print(f"Errore durante l'analisi o l'ordine su {symbol}: {exc}")


def run_bot() -> None:
    ensure_trade_history()

    ultimo_controllo_prelievo = None
    check_signals()

    try:
        publish_dashboard_snapshot()
    except Exception as exc:
        print(f"Errore durante la pubblicazione dello snapshot dashboard: {exc}")

    ora_attuale = datetime.datetime.now()
    if ora_attuale.hour == 23 and ultimo_controllo_prelievo != ora_attuale.date():
        controlla_e_preleva_profitti()


if __name__ == "__main__":
    run_bot()
