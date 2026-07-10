import streamlit as st
import pandas as pd

from cookie_auth import require_cookie_auth
from market import (
    CRYPTO_TARGETS,
    TIMEFRAME,
    get_account_balance,
    get_balance_snapshot,
    get_market_data,
    get_open_positions,
    get_open_position_counts,
)
from news_monitor import analyze_symbol_news, get_news_block_buys_enabled, get_news_monitor_enabled
from storage import empty_trade_history, load_trade_history

st.set_page_config(page_title="Kraken Trading Bot Dashboard", layout="wide", page_icon="chart")

require_cookie_auth()

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

df_trades = load_trade_history()
if df_trades.empty:
    df_trades = empty_trade_history()

try:
    api_open_position_counts = get_open_position_counts()
    api_open_positions = get_open_positions()
except Exception as exc:
    api_open_position_counts = {symbol: 0 for symbol in CRYPTO_TARGETS}
    api_open_positions = []
    st.warning(f"Impossibile leggere le posizioni aperte da Kraken: {exc}")


@st.cache_data(ttl=60)
def fetch_market_snapshot(symbol: str) -> dict[str, object]:
    market_df = get_market_data(symbol)
    last_row = market_df.iloc[-1]
    current_price = last_row["close"]
    news_snapshot = analyze_symbol_news(symbol)
    news_label = news_snapshot["label"]
    signal = (
        "BUY"
        if last_row["RSI"] < 40 and (current_price > last_row["EMA_9"] or (current_price + 0.05 >= last_row["EMA_9"] and news_label == 'POSITIVE'))
        else "WAIT"
    )
    return {
        "Last Price": round(float(last_row["close"]), 4),
        "RSI": round(float(last_row["RSI"]), 2),
        "EMA 9": round(float(last_row["EMA_9"]), 4),
        "Signal": signal,
    }


def build_monitored_crypto_table() -> pd.DataFrame:
    rows = []

    for symbol in CRYPTO_TARGETS:
        try:
            snapshot = fetch_market_snapshot(symbol)
            rows.append(
                {
                    "Symbol": symbol,
                    **snapshot,
                    "Open Trades": api_open_position_counts.get(symbol, 0),
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "Symbol": symbol,
                    "Last Price": "N/A",
                    "RSI": "N/A",
                    "EMA 9": "N/A",
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
    for symbol in CRYPTO_TARGETS:
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

capitale_iniziale = 100.0
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
    balance_snapshot = get_balance_snapshot()
    capitale_attuale = balance_snapshot["free_eur"]
except Exception as exc:
    balance_snapshot = {"free_eur": 0.0, "used_eur": 0.0, "total_eur": 0.0}
    capitale_attuale = 0.0
    st.warning(f"Impossibile leggere il saldo Kraken: {exc}")

trade_attivi = sum(api_open_position_counts.values())

col1, col2, col3, col4 = st.columns(4)
col1.metric("Capitale Attuale", f"{round(capitale_attuale, 2)} EUR", f"{round(profitto_totale, 2)} EUR totale")
col2.metric("Target Obiettivo (x2)", f"{capitale_iniziale * 2} EUR")
col3.metric("Posizioni Aperte", f"{trade_attivi}")
col4.metric(
    "Win Rate",
    f"{round((trade_vincenti / (trade_vincenti + trade_perdenti) * 100), 1) if (trade_vincenti + trade_perdenti) > 0 else 0.0} %",
)

st.markdown("---")

saldo_col1, saldo_col2, saldo_col3 = st.columns(3)
saldo_col1.metric("EUR Free", f"{round(balance_snapshot['free_eur'], 2)} EUR")
saldo_col2.metric("EUR Locked", f"{round(balance_snapshot['used_eur'], 2)} EUR")
saldo_col3.metric("EUR Total", f"{round(balance_snapshot['total_eur'], 2)} EUR")

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
    st.info(f"Strategia: RSI (<40) + incrocio EMA 9\nTimeframe: {TIMEFRAME}")

    st.markdown("**Asset Monitorati:**")
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
        "- Budget per Trade: 20 EUR\n"
        "- Leva Finanziaria: 3x\n"
        "- Stop Loss fisso: -2%\n"
        "- Take Profit fisso: +4%"
    )
