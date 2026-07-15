# Tradebot Kraken

Bot di trading automatico su **Kraken** con dashboard Streamlit, alert Telegram e deploy su **Google Cloud Run**.

Il sistema analizza il mercato crypto in coppia **EUR**, entra solo quando convergono segnali tecnici, sentiment e trend, e gestisce automaticamente stop loss e take profit in base alla volatilità di ogni asset.

---

## Cosa fa

| Componente | Ruolo |
|---|---|
| **Bot** (`bot.py`) | Worker schedulato che scansiona il mercato, apre posizioni e invia notifiche Telegram |
| **Dashboard** (`dashboard.py`) | UI web read-only: legge snapshot e storico da GCS, nessuna chiamata live |
| **Dashboard snapshot** (`dashboard_snapshot.py`) | Snapshot JSON scritto dal bot a ogni run |
| **Market** (`market.py`) | Dati OHLCV, indicatori tecnici, discovery asset dinamico, filtro trend |
| **News monitor** (`news_monitor.py`) | RSS crypto, sentiment FinBERT e cache persistente su GCS |
| **Storage** (`storage.py`) | Storico trade su file locale e/o Cloud Storage, sync con API Kraken |

---

## Strategia di trading

### Timeframe e segnale di ingresso

- **Timeframe**: 15 minuti
- **Segnale BUY** quando tutte le condizioni sono vere:
  1. **Trend positivo** (vedi sotto)
  2. **RSI < 40** (ipervenduto)
  3. **Prezzo sopra EMA 9**, oppure prezzo in linea con EMA 9 e **sentiment news positivo**
  4. **Nessun blocco news**: sentiment non fortemente negativo (se `NEWS_BLOCK_BUYS=true`)

### Come il sentiment influenza le decisioni

Il sentiment **non è decorativo**: entra direttamente nella logica del bot.

| Esito sentiment | Effetto sul trading |
|---|---|
| `POSITIVE` | Può consentire l'ingresso anche con prezzo **in linea** con EMA 9 (non solo sopra) |
| `NEGATIVE` (sotto soglia) | **Blocca** nuovi buy se `NEWS_BLOCK_BUYS=true` (default) |
| `NEUTRAL` / `NO_DATA` | Nessun effetto diretto su ingresso/uscita |

Il blocco negativo scatta quando `label == NEGATIVE` e `score <= NEWS_NEGATIVE_THRESHOLD` (default `-0.35`).

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
Lista core
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
| Budget per trade | 22 EUR |
| Leva | 4x (1x su DOGE/EUR) |
| Max posizioni contemporanee | 4 |
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

Il modulo news legge feed RSS crypto (Cointelegraph, CryptoNews, o custom) e calcola un punteggio di sentiment per ogni asset con **FinBERT** (`ProsusAI/finbert`).

**Pipeline a livelli (dalla più veloce alla più costosa):**

```
1. Cache GCS (news_sentiment.json)     → 24h, condivisa tra bot e dashboard
2. Cache RSS in RAM                    → 10 min, feed scaricati una volta per run
3. Calcolo sentiment (FinBERT)         → al massimo 1 volta/giorno per asset
4. Fallback lessicale                  → se ML non disponibile
```

**Comportamento default:**

- `NEWS_ML_SENTIMENT_ENABLED=true` — usa FinBERT (PyTorch CPU)
- `NEWS_BLOCK_BUYS=true` — blocca buy su sentiment fortemente negativo
- Cache sentiment persistita su **GCS** nello stesso bucket dello storico trade

**Log tipici:**

```
[News] Cache sentiment caricata da locale + GCS: 12 symbol.
[News] Modello ML sentiment caricato: ProsusAI/finbert
[News] Sentiment BTC/EUR da cache persisted (6h fa): POSITIVE (0.87)
[News] Sentiment SOL/EUR calcolato: NEUTRAL (0.00)
[News] Cache sentiment salvata su GCS (12 symbol).
Filtro news attivo: XRP/EUR bloccato da sentiment negativo (-0.42).
```

Per forzare un ricalcolo immediato: elimina `news_sentiment.json` dal bucket GCS.

I fallimenti del modulo news non fermano il bot: riducono solo il contesto disponibile.

### Ottimizzazioni computazionali

Il bot è progettato per ridurre chiamate API e calcoli ripetuti:

| Area | Ottimizzazione |
|---|---|
| Feed RSS | Scaricati una volta, riusati per tutti gli asset (cache 10 min) |
| Sentiment | Calcolato al massimo 1 volta/giorno per asset, persistito su GCS |
| Posizioni / saldo | Una sola chiamata Kraken per run del bot |
| FinBERT | Caricato solo quando serve un ricalcolo (non ogni 15 min) |
| CoinGecko trending | Cache 1 ora |
| Immagine bot | `requirements-bot.txt` separato dalla dashboard |

### Prelievo automatico profitti

Ogni giorno alle 23:00, se il saldo totale supera `SOGLIA_PRELIEVO_EUR` (default 200 EUR), il bot preleva l'eccedenza verso il conto configurato (`KRAKEN_WITHDRAWAL_ACCOUNT`).

---

## Architettura dati: bot scrive, dashboard legge

Il bot è l'**unico componente** che interroga Kraken, RSS e FinBERT. A ogni run salva su GCS:

| File | Contenuto | Chi scrive | Chi legge |
|---|---|---|---|
| `storico_trade.csv` | Storico operazioni | Bot | Dashboard |
| `news_sentiment.json` | Cache sentiment per asset | Bot | Bot (run successivi) |
| `dashboard_snapshot.json` | Saldo, posizioni, segnali, news | Bot | Dashboard |

La dashboard **non effettua fetch** verso Kraken, CoinGecko o feed RSS: carica solo file dal bucket (o copia locale in dev). Risultato: avvio immediato, nessun cold start ML, nessun rate limit API sulla UI.

```
Bot (ogni 15 min)
  ├─ fetch Kraken / RSS / FinBERT
  ├─ salva storico_trade.csv
  ├─ salva news_sentiment.json
  └─ salva dashboard_snapshot.json
           ↓
Dashboard (on demand)
  ├─ legge dashboard_snapshot.json
  └─ legge storico_trade.csv
```

---

## Architettura su GCP

Il repo è pensato per due servizi Cloud Run:

- **`dashboard`**: Streamlit UI per monitoraggio saldo e trade
- **`bot job`**: Cloud Run Job schedulato, nessuna porta in ascolto

```
┌─────────────┐     ┌──────────────┐     ┌──────────────────────────────┐
│ Cloud Run   │     │ Cloud Run    │     │ Cloud Storage (GCS)          │
│ Dashboard   │────▶│ Job (Bot)    │────▶│ storico_trade.csv            │
│ (read-only) │     │ (bot.py)     │     │ news_sentiment.json          │
└─────────────┘     └──────────────┘     │ dashboard_snapshot.json      │
       │                    │            └──────────────────────────────┘
       │                    ▼
       │              Kraken API
       │              Telegram API
       └─ legge solo GCS     CoinGecko API
                             Feed RSS / FinBERT
```

- I **secret** del bot arrivano da Google Secret Manager; la dashboard richiede solo auth Streamlit
- **Storico trade**, **cache sentiment** e **snapshot dashboard** vivono su Cloud Storage
- La dashboard ha bisogno solo di **lettura** sul bucket (`storage.objectViewer` o `objectAdmin`)
- Le **posizioni aperte** nel bot sono lette live da Kraken; la dashboard mostra l'ultimo snapshot salvato dal bot

---

## Variabili d'ambiente

### Obbligatorie

| Variabile | Dove | Descrizione |
|---|---|---|
| `KRAKEN_API_KEY` | bot | API key Kraken |
| `KRAKEN_SECRET` | bot | Secret Kraken |
| `TELEGRAM_TOKEN` | bot | Token bot Telegram |
| `TELEGRAM_CHAT_ID` | bot | Chat ID per gli alert |
| `TRADE_HISTORY_BUCKET` | bot + dashboard | Bucket GCS per storico, sentiment e snapshot |

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
| `NEWS_ML_SENTIMENT_ENABLED` | `true` | Usa FinBERT per il sentiment |
| `NEWS_BLOCK_BUYS` | `true` | Blocca buy su sentiment fortemente negativo |
| `NEWS_NEGATIVE_THRESHOLD` | `-0.35` | Soglia di blocco (score ≤ valore) |
| `NEWS_FEED_CACHE_SECONDS` | `600` | Cache feed RSS in RAM (secondi) |
| `NEWS_SENTIMENT_CACHE_SECONDS` | `86400` | Durata cache sentiment per asset (24h) |
| `NEWS_SENTIMENT_OBJECT` | `news_sentiment.json` | Nome file cache sentiment su GCS |
| `NEWS_SENTIMENT_LOCAL_PATH` | `news_sentiment.json` | Path locale cache sentiment (dev) |
| `DASHBOARD_SNAPSHOT_OBJECT` | `dashboard_snapshot.json` | Snapshot dashboard su GCS |
| `DASHBOARD_SNAPSHOT_LOCAL_PATH` | `dashboard_snapshot.json` | Path locale snapshot (dev) |
| `NEWS_FEEDS` | Cointelegraph + CryptoNews | Feed RSS custom (comma-separated) |
| `NEWS_SENTIMENT_MODEL` | `ProsusAI/finbert` | Modello Hugging Face |

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
# Per il solo bot (senza Streamlit):
# pip install torch --index-url https://download.pytorch.org/whl/cpu
# pip install -r requirements-bot.txt

cp .env.example .env   # poi compila i valori

# Test connessione Kraken
python main.py

# Avvia il bot (una scansione)
python bot.py

# Avvia la dashboard (legge solo file locali/GCS)
streamlit run dashboard.py
```

La dashboard non richiede credenziali Kraken: servono solo `TRADE_HISTORY_BUCKET` (o file locali) e auth Streamlit.

Per FinBERT sul **bot** servono `torch` (CPU) e `transformers` (`requirements-bot.txt`).

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
4. Builda entrambe le immagini con Cloud Build (PyTorch CPU + FinBERT)
5. Deploya `dashboard` su Cloud Run (read-only, senza credenziali Kraken) e il bot come Cloud Run Job
6. Imposta `NEWS_ML_SENTIMENT_ENABLED=true` e `NEWS_BLOCK_BUYS=true` sul bot

Legge i secret da variabili d'ambiente, altrimenti chiede input interattivo.

### Deploy manuale

```bash
gcloud run deploy tradebot-dashboard \
  --image REGION-docker.pkg.dev/PROJECT/REPO/tradebot-dashboard:latest \
  --region REGION \
  --allow-unauthenticated \
  --set-env-vars TRADE_HISTORY_BUCKET=YOUR_BUCKET,DASHBOARD_SNAPSHOT_OBJECT=dashboard_snapshot.json \
  --set-secrets STREAMLIT_AUTH_USERNAME=streamlit-auth-username:latest,STREAMLIT_AUTH_PASSWORD_HASH=streamlit-auth-password-hash:latest,STREAMLIT_COOKIE_SECRET=streamlit-cookie-secret:latest
```

```bash
gcloud run jobs deploy tradebot-bot-job \
  --image REGION-docker.pkg.dev/PROJECT/REPO/tradebot-bot:latest \
  --region REGION \
  --set-env-vars TRADE_HISTORY_BUCKET=YOUR_BUCKET,NEWS_ML_SENTIMENT_ENABLED=true,NEWS_BLOCK_BUYS=true \
  --set-secrets KRAKEN_API_KEY=kraken-api-key:latest,KRAKEN_SECRET=kraken-secret:latest,TELEGRAM_TOKEN=telegram-token:latest,TELEGRAM_CHAT_ID=telegram-chat-id:latest
```

### Permessi IAM richiesti

Al service account Cloud Run:

- `roles/secretmanager.secretAccessor`
- `roles/storage.objectAdmin` (bot — scrive storico, sentiment e snapshot)
- `roles/storage.objectViewer` (dashboard — legge file dal bucket)

### Scheduling consigliato

Allinea il Cloud Run Job al timeframe della strategia: **ogni 15 minuti**.

---

## Struttura del progetto

```
tradebot/
├── bot.py                  # Loop di trading e gestione ordini
├── dashboard.py            # UI Streamlit read-only (GCS)
├── dashboard_snapshot.py     # Build/save/load snapshot dashboard
├── market.py               # Dati mercato, indicatori, discovery asset
├── news_monitor.py         # RSS + FinBERT + cache GCS
├── storage.py              # Storico trade (locale + GCS + API)
├── notifications.py        # Alert Telegram
├── app_config.py           # Lettura variabili d'ambiente
├── cookie_auth.py          # Autenticazione dashboard
├── Dockerfile.bot          # Immagine Cloud Run Job (leggera)
├── Dockerfile.dashboard    # Immagine dashboard
├── deploy-gcp.ps1          # Script bootstrap GCP
├── requirements.txt            # Dipendenze complete (dev locale)
├── requirements-bot.txt        # Dipendenze bot (Kraken + ML)
└── requirements-dashboard.txt  # Dipendenze dashboard (solo lettura GCS)
```
