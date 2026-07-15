import pandas as pd
import streamlit as st

from market import (
    CORE_CRYPTO_TARGETS,
    TIMEFRAME,
    TRADE_AMOUNT_EUR,
    get_all_candidate_symbols,
    get_balance_snapshot,
    get_market_data,
    get_open_positions,
    is_positive_trend,
    is_dynamic_crypto_enabled,
)
from news_monitor import analyze_symbol_news, get_news_block_buys_enabled, get_news_monitor_enabled
from storage import empty_trade_history, load_trade_history

try:
    from cookie_auth import require_cookie_auth
except ImportError:
    # Older deployments may still expose the session-based auth helper instead.
    from auth import require_streamlit_auth as require_cookie_auth

st.set_page_config(page_title="Kraken Trading Bot Dashboard", layout="wide", page_icon="chart")

require_cookie_auth()


@st.cache_data(ttl=30)
def fetch_trade_history() -> pd.DataFrame:
    return load_trade_history()


@st.cache_data(ttl=30)
def fetch_balance_snapshot() -> dict[str, object]:
    return get_balance_snapshot()


@st.cache_data(ttl=30)
def fetch_positions_snapshot() -> dict[str, object]:
    open_positions = get_open_positions()
    open_position_counts = {symbol: 0 for symbol in get_all_candidate_symbols()}

    for position in open_positions:
        symbol = position.get("symbol")
        if symbol in open_position_counts:
            open_position_counts[symbol] += 1

    return {
        "open_positions": open_positions,
        "open_position_counts": open_position_counts,
    }

st.markdown(
    """
<style>
.main {
    background-color: #0d1117;
    color: #ffffff;
}
.stMetric {
    background-color: #161b22;
    padding: 15px;
    border-radius: 10px;
    border: 1px solid #30363d;
}
h1, h2, h3 {
    color: #58a6ff !important;
}
</style>
""",
    unsafe_allow_html=True,
)

st.title("Kraken Trading Bot Dashboard")
st.subheader("Monitoraggio in tempo reale del capitale e delle operazioni")

df_trades = fetch_trade_history()
if df_trades.empty:
    df_trades = empty_trade_history()

try:
    positions_snapshot = fetch_positions_snapshot()
    api_open_position_counts = positions_snapshot["open_position_counts"]
    api_open_positions = positions_snapshot["open_positions"]
except Exception as exc:
    api_open_position_counts = {symbol: 0 for symbol in get_all_candidate_symbols()}
    api_open_positions = []
    st.warning(f"Impossibile leggere le posizioni aperte da Kraken: {exc}")


@st.cache_data(ttl=60)
def fetch_market_snapshot(symbol: str) -> dict[str, object]:
    market_df = get_market_data(symbol)
    last_row = market_df.iloc[-1]
    current_price = last_row["close"]
    news_snapshot = analyze_symbol_news(symbol)
    news_label = news_snapshot["label"]
    trend_positive = is_positive_trend(market_df)
    signal = (
        "BUY"
        if trend_positive
        and last_row["RSI"] < 40
        and (current_price > last_row["EMA_9"] or (current_price >= last_row["EMA_9"] and news_label == "POSITIVE"))
        else "WAIT"
    )
    return {
        "Last Price": round(float(last_row["close"]), 4),
        "RSI": round(float(last_row["RSI"]), 2),
        "EMA 9": round(float(last_row["EMA_9"]), 4),
        "Trend": "UP" if trend_positive else "DOWN",
        "Signal": signal,
    }


def build_monitored_crypto_table() -> pd.DataFrame:
    rows = []

    for symbol in get_all_candidate_symbols():
        try:
            snapshot = fetch_market_snapshot(symbol)
            rows.append(
                {
                    "Symbol": symbol,
                    "Core": "Yes" if symbol in CORE_CRYPTO_TARGETS else "Trending",
                    **snapshot,
                    "Open Trades": api_open_position_counts.get(symbol, 0),
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "Symbol": symbol,
                    "Core": "Yes" if symbol in CORE_CRYPTO_TARGETS else "Trending",
                    "Last Price": "N/A",
                    "RSI": "N/A",
                    "EMA 9": "N/A",
                    "Trend": "N/A",
                    "Signal": f"Error: {exc}",
                    "Open Trades": api_open_position_counts.get(symbol, 0),
                }
            )

    return pd.DataFrame(rows)


@st.cache_data(ttl=120)
def fetch_news_snapshot(symbol: str) -> dict[str, object]:
    snapshot = analyze_symbol_news(symbol)
    items = snapshot.get("items", [])
    first_item = items[0] if items else None
    return {
        "Sentiment": snapshot["label"],
        "Score": round(float(snapshot["score"]), 2),
        "News Items": len(items),
        "Top Headline": getattr(first_item, "title", "N/A") if first_item else "N/A",
    }


def build_news_table() -> pd.DataFrame:
    rows = []
    for symbol in get_all_candidate_symbols():
        try:
            rows.append({"Symbol": symbol, **fetch_news_snapshot(symbol)})
        except Exception as exc:
            rows.append(
                {
                    "Symbol": symbol,
                    "Sentiment": "ERROR",
                    "Score": 0.0,
                    "News Items": 0,
                    "Top Headline": str(exc),
                }
            )
    return pd.DataFrame(rows)


def build_positions_table() -> pd.DataFrame:
    rows = []
    for position in api_open_positions:
        rows.append(
            {
                "Symbol": position.get("symbol", "N/A"),
                "Side": position.get("side", "N/A"),
                "Contracts": round(float(position.get("contracts") or 0), 8),
                "Leverage": position.get("leverage", "N/A"),
                "Unrealized PnL": round(float(position.get("unrealizedPnl") or 0), 4) if position.get("unrealizedPnl") is not None else "N/A",
                "Entry Price": round(float(position.get("entryPrice") or 0), 4) if position.get("entryPrice") is not None else "N/A",
            }
        )
    return pd.DataFrame(rows)


def build_currency_table(snapshot: dict[str, object]) -> pd.DataFrame:
    raw_balance = snapshot.get("raw", {}) if isinstance(snapshot, dict) else {}
    free = raw_balance.get("free", {}) if isinstance(raw_balance, dict) else {}
    used = raw_balance.get("used", {}) if isinstance(raw_balance, dict) else {}
    total = raw_balance.get("total", {}) if isinstance(raw_balance, dict) else {}

    rows = []
    currencies = sorted(set(free) | set(used) | set(total))
    for currency in currencies:
        free_amount = float(free.get(currency, 0) or 0)
        used_amount = float(used.get(currency, 0) or 0)
        total_amount = float(total.get(currency, 0) or 0)
        if free_amount == 0 and used_amount == 0 and total_amount == 0:
            continue
        rows.append(
            {
                "Currency": currency,
                "Free": round(free_amount, 8),
                "Locked": round(used_amount, 8),
                "Total": round(total_amount, 8),
            }
        )

    return pd.DataFrame(rows)


trade_vincenti = 0
trade_perdenti = 0
profitto_totale = 0.0

if not df_trades.empty:
    buys = df_trades[df_trades["action"] == "BUY"]
    sells = df_trades[df_trades["action"] == "SELL"]
    for _, row in sells.iterrows():
        matching_buy = buys[buys["symbol"] == row["symbol"]].last_valid_index()
        if matching_buy is not None:
            buy_price = buys.loc[matching_buy, "price"]
            sell_price = row["price"]
            lev = row["leverage"]
            pnl_perc = ((sell_price - buy_price) / buy_price) * lev
            pnl_eur = (buy_price * row["amount"]) * pnl_perc
            profitto_totale += pnl_eur
            if pnl_eur > 0:
                trade_vincenti += 1
            else:
                trade_perdenti += 1

try:
    balance_snapshot = fetch_balance_snapshot()
    capitale_attuale = balance_snapshot["total_eur"]
except Exception as exc:
    balance_snapshot = {"free_eur": 0.0, "used_eur": 0.0, "total_eur": 0.0}
    capitale_attuale = 0.0
    st.warning(f"Impossibile leggere il portafoglio Kraken: {exc}")

trade_attivi = sum(api_open_position_counts.values())
budget_totale_allocato = TRADE_AMOUNT_EUR * trade_attivi

col1, col2, col3, col4 = st.columns(4)
col1.metric("Valore Portafoglio", f"{round(capitale_attuale, 2)} EUR", f"{round(profitto_totale, 2)} EUR totale")
col2.metric("Budget per Posizione", f"{TRADE_AMOUNT_EUR} EUR")
col3.metric("Guadagni Stimati", f"{round(profitto_totale, 2)} EUR")
col4.metric("Posizioni Aperte", f"{trade_attivi}", f"{round(budget_totale_allocato, 2)} EUR allocati")

win_rate = round((trade_vincenti / (trade_vincenti + trade_perdenti) * 100), 1) if (trade_vincenti + trade_perdenti) > 0 else 0.0
st.caption(f"Win Rate: {win_rate} %")

st.markdown("---")

saldo_col1, saldo_col2, saldo_col3 = st.columns(3)
saldo_col1.metric("EUR Free", f"{round(balance_snapshot['free_eur'], 2)} EUR")
saldo_col2.metric("EUR Locked", f"{round(balance_snapshot['used_eur'], 2)} EUR")
saldo_col3.metric("EUR Total", f"{round(balance_snapshot['total_eur'], 2)} EUR")

st.markdown("**Saldo per valuta:**")
currency_df = build_currency_table(balance_snapshot)
if currency_df.empty:
    st.info("Nessuna valuta aggiuntiva rilevata nel wallet.")
else:
    st.dataframe(currency_df, use_container_width=True, hide_index=True)

st.markdown("**Posizioni Aperte da API:**")
positions_df = build_positions_table()
if positions_df.empty:
    st.info("Nessuna posizione aperta rilevata da Kraken.")
else:
    st.dataframe(positions_df, use_container_width=True, hide_index=True)

st.markdown("---")

col_left, col_right = st.columns([2, 1])

with col_left:
    st.subheader("Registro Completo Operazioni")
    st.dataframe(df_trades.sort_values(by="timestamp", ascending=False), use_container_width=True)

with col_right:
    st.subheader("Stato e Configurazione Bot")
    st.success("Bot online e connesso a Kraken API")
    st.info(f"Strategia: RSI (<40) + incrocio EMA 9 + trend positivo\nTimeframe: {TIMEFRAME}")

    dynamic_status = "attivo" if is_dynamic_crypto_enabled() else "disattivo"
    st.markdown("**Asset Monitorati:**")
    st.caption(f"Lista core + trending CoinGecko (discovery dinamico {dynamic_status}).")
    monitored_df = build_monitored_crypto_table()
    st.dataframe(monitored_df, use_container_width=True, hide_index=True)

    st.markdown("**News & Sentiment:**")
    if get_news_monitor_enabled():
        st.caption(
            "Il modulo news è attivo"
            + (" e può bloccare i buy negativi." if get_news_block_buys_enabled() else " in sola modalità monitoraggio.")
        )
        st.dataframe(build_news_table(), use_container_width=True, hide_index=True)
    else:
        st.warning("News monitor disattivato.")

    st.markdown(
        "**Gestione Rischio:**\n"
        f"- Budget per Trade: {TRADE_AMOUNT_EUR} EUR\n"
        "- Leva Finanziaria: 3x\n"
        "- Stop Loss / Take Profit adattivi in base alla volatilita (ATR)\n"
        "- Asset poco volatili: SL ~-1% / TP ~+2.5%\n"
        "- Asset molto volatili: SL/TP piu ampi"
    )
