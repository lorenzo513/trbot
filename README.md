# Tradebot on GCP

This repo is wired for one Cloud Run service and one Cloud Run Job:

- `dashboard`: Streamlit UI for monitoring balance and trades.
- `bot job`: background worker that scans the market and sends Telegram alerts.

## Runtime model

- Secrets come from Google Secret Manager and are injected into Cloud Run as environment variables.
- Trade history is stored in a Cloud Storage bucket, because Cloud Run filesystems are ephemeral and not shared between services.
- The bot runs as a Cloud Run Job, so it does not need a listening port.

## Required environment variables

Set these on both services unless noted otherwise:

- `KRAKEN_API_KEY`
- `KRAKEN_SECRET`
- `TRADE_HISTORY_BUCKET`
- `TRADE_HISTORY_OBJECT` or use the default `storico_trade.csv`

Set these only on the bot job:

- `TELEGRAM_TOKEN`
- `TELEGRAM_CHAT_ID`
- `KRAKEN_WITHDRAWAL_ACCOUNT` if you use the withdrawal flow

Optional dashboard auth:

- `STREAMLIT_AUTH_USERNAME`
- `STREAMLIT_AUTH_PASSWORD`
- `STREAMLIT_COOKIE_SECRET`

Optional:

- `MODALITA_PROVA=true` to simulate trades
- `SOGLIA_PRELIEVO_EUR=200`
- `TRADE_HISTORY_LOCAL_PATH=storico_trade.csv` for local runs
- `TRADE_HISTORY_SOURCE=hybrid` to read from Kraken API and mirror to CSV
- `TRADE_HISTORY_LOOKBACK_DAYS=365` to widen the API history window
- `TRADE_HISTORY_LIMIT=500` to cap how many fills are requested per symbol
- `NEWS_MONITOR_ENABLED=true` to enable RSS-based news collection
- `NEWS_BLOCK_BUYS=true` to use negative sentiment as an entry filter
- `NEWS_NEGATIVE_THRESHOLD=-0.35` to tune the block sensitivity
- `NEWS_FEEDS` to override the default RSS sources, comma-separated
- `NEWS_SENTIMENT_MODEL=ProsusAI/finbert` to request a Hugging Face model if available

## GCP setup

1. Create a bucket for trade history.
2. Create the required secrets in Secret Manager.
3. Grant the Cloud Run service account:
   - `roles/secretmanager.secretAccessor`
   - `roles/storage.objectAdmin` for the bot
   - `roles/storage.objectViewer` is enough for the dashboard if you split identities
4. Build and push two images, one from `Dockerfile.dashboard` and one from `Dockerfile.bot`.

## Bootstrap script

Run the end-to-end bootstrap from PowerShell:

```powershell
.\deploy-gcp.ps1 -ProjectId YOUR_PROJECT -Region europe-west1
```

The script:

- creates the storage bucket if missing,
- creates and updates the secrets in Secret Manager,
- creates the Artifact Registry repo if needed,
- builds both images with Cloud Build,
- deploys `dashboard` to Cloud Run and the bot as a Cloud Run Job.

It reads the secret values from environment variables first, then prompts if they are not set:

- `KRAKEN_API_KEY`
- `KRAKEN_SECRET`
- `TELEGRAM_TOKEN`
- `TELEGRAM_CHAT_ID`
- `KRAKEN_WITHDRAWAL_ACCOUNT` is optional.
- `STREAMLIT_AUTH_USERNAME` and `STREAMLIT_AUTH_PASSWORD` are required for dashboard login.

The deploy script hashes `STREAMLIT_AUTH_PASSWORD` before storing it in Secret Manager and uses `STREAMLIT_COOKIE_SECRET` to sign the persistent login cookie.

## News & Sentiment Monitor

The bot includes an RSS-based news monitor that can score headlines for each tracked crypto pair.

- By default it uses a lightweight local fallback if `transformers` or a model backend is not available.
- If you want FinBERT-style scoring, install the NLP stack in the runtime image and keep `NEWS_SENTIMENT_MODEL` set.
- The dashboard shows the latest sentiment snapshot alongside the monitored symbols.
- If `NEWS_BLOCK_BUYS=true`, strongly negative sentiment can block new entries for that symbol.

This keeps the trading loop resilient: news failures do not stop the bot, they only reduce the amount of context available.

## Kraken trade history

When `TRADE_HISTORY_SOURCE=hybrid`, the dashboard and bot read fills from Kraken using `fetch_my_trades` and keep the CSV mirror updated automatically.

- This shows real executed trades, not just a local log file.
- The local CSV becomes a mirror and backup, not the source of truth.
- The default window is the last `365` days, adjustable with `TRADE_HISTORY_LOOKBACK_DAYS`.
- If the API is unavailable, the code falls back to the local history file so the UI stays usable.

Open positions are read separately from Kraken using `fetch_positions()`, so the dashboard and bot always use the exchange as the source of truth for active exposure.

## Example deploy

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

If you want, I can also add a `gcloud` bootstrap script or Terraform for the bucket, secrets, IAM and deployments.
