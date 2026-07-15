# Tradebot Kraken

Bot di trading automatico su **Kraken** con dashboard Streamlit, alert Telegram e deploy su **Google Cloud Run**.

Il sistema analizza il mercato crypto in coppia **EUR**, entra solo quando convergono segnali tecnici, sentiment e trend, e gestisce automaticamente stop loss e take profit in base alla volatilità di ogni asset.

---

## Cosa fa

| Componente | Ruolo |
|---|---|
| **Bot** (`bot.py`) | Worker schedulato che scansiona il mercato, apre posizioni e invia notifiche Telegram |
| **Dashboard** (`dashboard.py`) | Interfaccia web per monitorare saldo, posizioni aperte, segnali e storico trade |
| **Market** (`market.py`) | Dati OHLCV, indicatori tecnici, discovery asset dinamico, filtro trend |
| **News monitor** (`news_monitor.py`) | RSS crypto + sentiment analysis per filtrare o arricchire le decisioni |
| **Storage** (`storage.py`) | Storico trade su file locale e/o Cloud Storage, sync con API Kraken |

---

## Strategia di trading

### Timeframe e segnale di ingresso

- **Timeframe**: 10 minuti
- **Segnale BUY** quando tutte le condizioni sono vere:
  1. **Trend positivo** (vedi sotto)
  2. **RSI < 40** (ipervenduto)
  3. **Prezzo sopra EMA 9**, oppure prezzo in linea con EMA 9 e **sentiment news positivo**

### Filtro trend positivo

Prima di analizzare un asset, il bot scarta quelli fuori trend. Un asset passa il filtro solo se:

- prezzo > EMA 9
- EMA 9 in salita rispetto a 3 candele fa
- prezzo in crescita nelle ultime 5 candele

In questo modo si opera solo su asset con momentum rialzista, non su una lista statica indiscriminata.

### Asset monitorati

Il bot combina due fonti:

1. **Lista core** (sempre attiva): BTC, SOL, XRP, LINK, ADA, DOGE, EDGE — tutte in coppia `/EUR`
2. **Discovery dinamico** (opzionale): le crypto più trending su [CoinGecko](https://www.coingecko.com/en/trending-cryptocurrencies), mappate solo se disponibili su Kraken in `/EUR`

```
Lista core + BTC
       ↓
CoinGecko Trending (cache 1h)
       ↓
Filtro: esiste su Kraken /EUR?
       ↓
Filtro: trend positivo?
       ↓
Analisi RSI + EMA + News
       ↓
Ingresso con SL/TP adattivi
```

Per tornare alla sola lista fissa: `DYNAMIC_CRYPTO_ENABLED=false`.

### Gestione del rischio

| Parametro | Valore default |
|---|---|
| Budget per trade | 20 EUR |
| Leva | 3x (1x su DOGE/EUR) |
| Max posizioni contemporanee | 3 |
| Max 1 posizione per symbol | sì |

**Stop Loss / Take Profit adattivi (ATR)**

I livelli di protezione si adattano alla volatilità misurata con l'ATR (Average True Range):

| Volatilità ATR | Stop Loss | Take Profit |
|---|---|---|
| Bassa (< 1%) | ~ −1% | ~ +2.5% |
| Media (1–2.5%) | −2% | +4% |
| Alta (> 2.5%) | più ampi, scalati su ATR | più ampi, scalati su ATR |

Asset stabili come **BTC** usano SL/TP più stretti; asset volatili come **XRP** possono avere override manuali (`SYMBOL_RISK_MULTIPLIERS` in `bot.py`).

Dopo ogni ingresso, il bot piazza automaticamente ordini di protezione su Kraken. Se Kraken rifiuta SL/TP, l'evento viene loggato e il symbol viene bloccato per 24 ore per evitare retry loop.

### News & sentiment

Il modulo news legge feed RSS crypto (Cointelegraph, CryptoNews, o custom) e calcola un punteggio di sentiment per ogni asset.

- **Monitoraggio passivo** (default): arricchisce i messaggi Telegram e la dashboard
- **Blocco ingressi** (`NEWS_BLOCK_BUYS=true`): blocca nuovi buy se il sentiment è fortemente negativo
- **Fallback lexicon**: se FinBERT/transformers non è disponibile, usa un dizionario locale di parole positive/negative

I fallimenti del modulo news non fermano il bot: riducono solo il contesto disponibile.

### Prelievo automatico profitti

Ogni giorno alle 23:00, se il saldo totale supera `SOGLIA_PRELIEVO_EUR` (default 200 EUR), il bot preleva l'eccedenza verso il conto configurato (`KRAKEN_WITHDRAWAL_ACCOUNT`).

---

## Architettura su GCP

Il repo è pensato per due servizi Cloud Run:

- **`dashboard`**: Streamlit UI per monitoraggio saldo e trade
- **`bot job`**: Cloud Run Job schedulato, nessuna porta in ascolto

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│ Cloud Run   │     │ Cloud Run    │     │ Cloud       │
│ Dashboard   │────▶│ Job (Bot)    │────▶│ Storage     │
│ (Streamlit) │     │ (bot.py)     │     │ (storico)   │
└─────────────┘     └──────────────┘     └─────────────┘
       │                    │
       ▼                    ▼
  Kraken API           Telegram API
  CoinGecko API        Secret Manager
```

- I **secret** arrivano da Google Secret Manager
- Lo **storico trade** vive su Cloud Storage (il filesystem Cloud Run è effimero)
- Le **posizioni aperte** sono sempre lette live da Kraken (`fetch_positions`)

---

## Variabili d'ambiente

### Obbligatorie

| Variabile | Dove | Descrizione |
|---|---|---|
| `KRAKEN_API_KEY` | bot + dashboard | API key Kraken |
| `KRAKEN_SECRET` | bot + dashboard | Secret Kraken |
| `TELEGRAM_TOKEN` | bot | Token bot Telegram |
| `TELEGRAM_CHAT_ID` | bot | Chat ID per gli alert |
| `TRADE_HISTORY_BUCKET` | bot + dashboard | Bucket GCS per lo storico |

### Dashboard auth

| Variabile | Descrizione |
|---|---|
| `STREAMLIT_AUTH_USERNAME` | Username login |
| `STREAMLIT_AUTH_PASSWORD` | Password in chiaro (hashata al deploy) |
| `STREAMLIT_COOKIE_SECRET` | Secret per firmare il cookie di sessione |

### Trading

| Variabile | Default | Descrizione |
|---|---|---|
| `MODALITA_PROVA` | `false` | Simula i trade senza ordini reali |
| `SOGLIA_PRELIEVO_EUR` | `200` | Soglia per prelievo automatico |
| `KRAKEN_WITHDRAWAL_ACCOUNT` | `revolut` | Destinazione prelievo su Kraken |

### Discovery crypto dinamico

| Variabile | Default | Descrizione |
|---|---|---|
| `DYNAMIC_CRYPTO_ENABLED` | `true` | Aggiunge asset trending da CoinGecko |
| `TRENDING_CRYPTO_LIMIT` | `5` | Quante trending includere (max 15) |
| `TRENDING_CRYPTO_CACHE_SECONDS` | `3600` | Durata cache trending (secondi) |

### News & sentiment

| Variabile | Default | Descrizione |
|---|---|---|
| `NEWS_MONITOR_ENABLED` | `true` | Attiva raccolta RSS |
| `NEWS_BLOCK_BUYS` | `false` | Blocca buy su sentiment negativo |
| `NEWS_NEGATIVE_THRESHOLD` | `-0.35` | Soglia di blocco |
| `NEWS_FEEDS` | Cointelegraph + CryptoNews | Feed RSS custom (comma-separated) |
| `NEWS_SENTIMENT_MODEL` | `ProsusAI/finbert` | Modello Hugging Face (se disponibile) |

### Storico trade

| Variabile | Default | Descrizione |
|---|---|---|
| `TRADE_HISTORY_OBJECT` | `storico_trade.csv` | Nome file nel bucket |
| `TRADE_HISTORY_LOCAL_PATH` | `storico_trade.csv` | Path locale per dev |
| `TRADE_HISTORY_SOURCE` | `hybrid` | `hybrid` = API Kraken + mirror CSV |
| `TRADE_HISTORY_LOOKBACK_DAYS` | `365` | Finestra storico API |
| `TRADE_HISTORY_LIMIT` | `500` | Max fill per symbol dall'API |

Copia `.env.example` come punto di partenza per lo sviluppo locale.

---

## Sviluppo locale

```bash
pip install -r requirements.txt
cp .env.example .env   # poi compila i valori

# Test connessione Kraken
python main.py

# Avvia il bot (una scansione)
python bot.py

# Avvia la dashboard
streamlit run dashboard.py
```

---

## Deploy su GCP

### Bootstrap automatico

```powershell
.\deploy-gcp.ps1 -ProjectId YOUR_PROJECT -Region europe-west1
```

Lo script:

1. Crea il bucket GCS se mancante
2. Crea/aggiorna i secret in Secret Manager
3. Crea il repo Artifact Registry se necessario
4. Builda entrambe le immagini con Cloud Build
5. Deploya `dashboard` su Cloud Run e il bot come Cloud Run Job

Legge i secret da variabili d'ambiente, altrimenti chiede input interattivo.

### Deploy manuale

```bash
gcloud run deploy tradebot-dashboard \
  --image REGION-docker.pkg.dev/PROJECT/REPO/tradebot-dashboard:latest \
  --region REGION \
  --allow-unauthenticated \
  --set-env-vars TRADE_HISTORY_BUCKET=YOUR_BUCKET \
  --set-secrets KRAKEN_API_KEY=kraken-api-key:latest,KRAKEN_SECRET=kraken-secret:latest
```

```bash
gcloud run jobs deploy tradebot-bot-job \
  --image REGION-docker.pkg.dev/PROJECT/REPO/tradebot-bot:latest \
  --region REGION \
  --set-env-vars TRADE_HISTORY_BUCKET=YOUR_BUCKET \
  --set-secrets KRAKEN_API_KEY=kraken-api-key:latest,KRAKEN_SECRET=kraken-secret:latest,TELEGRAM_TOKEN=telegram-token:latest,TELEGRAM_CHAT_ID=telegram-chat-id:latest
```

### Permessi IAM richiesti

Al service account Cloud Run:

- `roles/secretmanager.secretAccessor`
- `roles/storage.objectAdmin` (bot)
- `roles/storage.objectViewer` (dashboard, se identità separate)

---

## Struttura del progetto

```
tradebot/
├── bot.py              # Loop di trading e gestione ordini
├── dashboard.py        # UI Streamlit
├── market.py           # Dati mercato, indicatori, discovery asset
├── news_monitor.py     # RSS + sentiment analysis
├── storage.py          # Storico trade (locale + GCS + API)
├── notifications.py    # Alert Telegram
├── app_config.py       # Lettura variabili d'ambiente
├── cookie_auth.py      # Autenticazione dashboard
├── Dockerfile.bot      # Immagine Cloud Run Job
├── Dockerfile.dashboard
├── deploy-gcp.ps1      # Script bootstrap GCP
└── requirements.txt
```
