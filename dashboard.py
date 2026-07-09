import streamlit as st

from auth import require_streamlit_auth
from market import CRYPTO_TARGETS, get_account_balance
from storage import empty_trade_history, load_trade_history

st.set_page_config(page_title="Kraken Trading Bot Dashboard", layout="wide", page_icon="chart")

require_streamlit_auth()

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
    capitale_attuale = get_account_balance()
except Exception as exc:
    capitale_attuale = 0.0
    st.warning(f"Impossibile leggere il saldo Kraken: {exc}")

trade_attivi = len(df_trades[df_trades["action"] == "BUY"]) - len(df_trades[df_trades["action"] == "SELL"])
trade_attivi = max(0, trade_attivi)

col1, col2, col3, col4 = st.columns(4)
col1.metric("Capitale Attuale", f"{round(capitale_attuale, 2)} EUR", f"{round(profitto_totale, 2)} EUR totale")
col2.metric("Target Obiettivo (x2)", f"{capitale_iniziale * 2} EUR")
col3.metric("Posizioni Aperte", f"{trade_attivi}")
col4.metric(
    "Win Rate",
    f"{round((trade_vincenti / (trade_vincenti + trade_perdenti) * 100), 1) if (trade_vincenti + trade_perdenti) > 0 else 0.0} %",
)

st.markdown("---")

col_left, col_right = st.columns([2, 1])

with col_left:
    st.subheader("Registro Completo Operazioni")
    st.dataframe(df_trades.sort_values(by="timestamp", ascending=False), use_container_width=True)

with col_right:
    st.subheader("Stato e Configurazione Bot")
    st.success("Bot online e connesso a Kraken API")
    st.info("Strategia: RSI (<35) + incrocio EMA 9\nTimeframe: 15 minuti (15m)")

    st.markdown("**Asset Monitorati:**")
    for crypto in CRYPTO_TARGETS:
        st.markdown(f"- `{crypto}`")

    st.markdown(
        "**Gestione Rischio:**\n"
        "- Budget per Trade: 20 EUR\n"
        "- Leva Finanziaria: 3x\n"
        "- Stop Loss fisso: -2%\n"
        "- Take Profit fisso: +4%"
    )
