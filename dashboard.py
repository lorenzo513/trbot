import pandas as pd
import streamlit as st

from dashboard_snapshot import load_dashboard_snapshot
from storage import empty_trade_history, load_persisted_trade_history

try:
    from cookie_auth import require_cookie_auth
except ImportError:
    from auth import require_streamlit_auth as require_cookie_auth

st.set_page_config(page_title="Kraken Trading Bot Dashboard", layout="wide", page_icon="chart")

require_cookie_auth()


@st.cache_data(ttl=15)
def fetch_trade_history() -> pd.DataFrame:
    return load_persisted_trade_history()


@st.cache_data(ttl=15)
def fetch_dashboard_snapshot() -> dict[str, object]:
    return load_dashboard_snapshot()


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
st.subheader("Monitoraggio del capitale e delle operazioni")

snapshot = fetch_dashboard_snapshot()
config = snapshot.get("config", {}) if isinstance(snapshot.get("config"), dict) else {}
balance_snapshot = snapshot.get("balance", {}) if isinstance(snapshot.get("balance"), dict) else {}
positions_snapshot = snapshot.get("positions", {}) if isinstance(snapshot.get("positions"), dict) else {}
symbols_snapshot = snapshot.get("symbols", {}) if isinstance(snapshot.get("symbols"), dict) else {}

df_trades = fetch_trade_history()
if df_trades.empty:
    df_trades = empty_trade_history()

api_open_positions = positions_snapshot.get("open_positions", [])
api_open_position_counts = positions_snapshot.get("open_position_counts", {})
if not isinstance(api_open_positions, list):
    api_open_positions = []
if not isinstance(api_open_position_counts, dict):
    api_open_position_counts = {}

updated_at = snapshot.get("updated_at")
if updated_at:
    st.caption(f"Dati aggiornati dal bot: {updated_at}")
else:
    st.warning("Snapshot dashboard non ancora disponibile. Attendi il prossimo run del bot.")

TIMEFRAME = config.get("timeframe", "15m")
TRADE_AMOUNT_EUR = float(config.get("trade_amount_eur", 22.0))
LEVERAGE = config.get("leverage", 4)


def build_monitored_crypto_table() -> pd.DataFrame:
    rows = []
    candidate_symbols = snapshot.get("candidate_symbols", [])
    if not isinstance(candidate_symbols, list):
        candidate_symbols = list(symbols_snapshot.keys())

    for symbol in candidate_symbols:
        symbol_data = symbols_snapshot.get(symbol, {})
        if not isinstance(symbol_data, dict):
            symbol_data = {}
        rows.append(
            {
                "Symbol": symbol,
                "Core": "Yes" if symbol_data.get("core") else "Trending",
                "Last Price": symbol_data.get("last_price", "N/A"),
                "RSI": symbol_data.get("rsi", "N/A"),
                "EMA 9": symbol_data.get("ema_9", "N/A"),
                "Trend": symbol_data.get("trend", "N/A"),
                "Signal": symbol_data.get("signal", "N/A"),
                "Sentiment": symbol_data.get("sentiment", "N/A"),
                "Score": symbol_data.get("score", "N/A"),
                "Sentiment Age": symbol_data.get("sentiment_age", "N/A"),
                "Open Trades": symbol_data.get("open_trades", api_open_position_counts.get(symbol, 0)),
            }
        )
    return pd.DataFrame(rows)


def build_news_table() -> pd.DataFrame:
    rows = []
    candidate_symbols = snapshot.get("candidate_symbols", [])
    if not isinstance(candidate_symbols, list):
        candidate_symbols = list(symbols_snapshot.keys())

    for symbol in candidate_symbols:
        symbol_data = symbols_snapshot.get(symbol, {})
        if not isinstance(symbol_data, dict):
            symbol_data = {}
        rows.append(
            {
                "Symbol": symbol,
                "Sentiment": symbol_data.get("sentiment", "N/A"),
                "Score": symbol_data.get("score", "N/A"),
                "Sentiment Age": symbol_data.get("sentiment_age", "N/A"),
                "News Items": symbol_data.get("news_items", 0),
                "Top Headline": symbol_data.get("top_headline", "N/A"),
            }
        )
    return pd.DataFrame(rows)


def build_positions_table() -> pd.DataFrame:
    rows = []
    for position in api_open_positions:
        if not isinstance(position, dict):
            continue
        rows.append(
            {
                "Symbol": position.get("symbol", "N/A"),
                "Side": position.get("side", "N/A"),
                "Contracts": round(float(position.get("contracts") or 0), 8),
                "Leverage": position.get("leverage", "N/A"),
                "Unrealized PnL": round(float(position.get("unrealizedPnl") or 0), 4)
                if position.get("unrealizedPnl") is not None
                else "N/A",
                "Entry Price": round(float(position.get("entryPrice") or 0), 4)
                if position.get("entryPrice") is not None
                else "N/A",
            }
        )
    return pd.DataFrame(rows)


def build_currency_table(balance: dict[str, object]) -> pd.DataFrame:
    raw_balance = balance.get("raw", {}) if isinstance(balance, dict) else {}
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

capitale_attuale = float(balance_snapshot.get("total_eur", 0.0) or 0.0)
trade_attivi = sum(int(value or 0) for value in api_open_position_counts.values())
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
saldo_col1.metric("EUR Free", f"{round(float(balance_snapshot.get('free_eur', 0.0) or 0.0), 2)} EUR")
saldo_col2.metric("EUR Locked", f"{round(float(balance_snapshot.get('used_eur', 0.0) or 0.0), 2)} EUR")
saldo_col3.metric("EUR Total", f"{round(float(balance_snapshot.get('total_eur', 0.0) or 0.0), 2)} EUR")

st.markdown("**Saldo per valuta:**")
currency_df = build_currency_table(balance_snapshot)
if currency_df.empty:
    st.info("Nessuna valuta aggiuntiva rilevata nel wallet.")
else:
    st.dataframe(currency_df, use_container_width=True, hide_index=True)

st.markdown("**Posizioni Aperte:**")
positions_df = build_positions_table()
if positions_df.empty:
    st.info("Nessuna posizione aperta rilevata.")
else:
    st.dataframe(positions_df, use_container_width=True, hide_index=True)

st.markdown("---")

col_left, col_right = st.columns([2, 1])

with col_left:
    st.subheader("Registro Completo Operazioni")
    st.dataframe(df_trades.sort_values(by="timestamp", ascending=False), use_container_width=True)

with col_right:
    st.subheader("Stato e Configurazione Bot")
    if updated_at:
        st.success("Dati sincronizzati dallo snapshot del bot")
    else:
        st.warning("In attesa del primo snapshot dal bot")
    st.info(f"Strategia: RSI (<40) + incrocio EMA 9 + trend positivo\nTimeframe: {TIMEFRAME}")

    dynamic_status = "attivo" if config.get("dynamic_crypto_enabled") else "disattivo"
    st.markdown("**Asset Monitorati:**")
    st.caption(f"Lista core + trending CoinGecko (discovery dinamico {dynamic_status}).")
    monitored_df = build_monitored_crypto_table()
    st.dataframe(monitored_df, use_container_width=True, hide_index=True)

    st.markdown("**News & Sentiment:**")
    if config.get("news_monitor_enabled", True):
        st.caption(
            "Il modulo news è attivo"
            + (" e può bloccare i buy negativi." if config.get("news_block_buys") else " in sola modalità monitoraggio.")
        )
        st.dataframe(build_news_table(), use_container_width=True, hide_index=True)
    else:
        st.warning("News monitor disattivato.")

    st.markdown(
        "**Gestione Rischio:**\n"
        f"- Budget per Trade: {TRADE_AMOUNT_EUR} EUR\n"
        f"- Leva Finanziaria: {LEVERAGE}x\n"
        "- Stop Loss / Take Profit adattivi in base alla volatilita (ATR)\n"
        "- Asset poco volatili: SL ~-1% / TP ~+2.5%\n"
        "- Asset molto volatili: SL/TP piu ampi"
    )
