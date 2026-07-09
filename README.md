# Tradebot on GCP

This repo is wired for two Cloud Run services:

- `dashboard`: Streamlit UI for monitoring balance and trades.
- `bot`: background worker that scans the market and sends Telegram alerts.

## Runtime model

- Secrets come from Google Secret Manager and are injected into Cloud Run as environment variables.
- Trade history is stored in a Cloud Storage bucket, because Cloud Run filesystems are ephemeral and not shared between services.
- The bot exposes a small `/healthz` endpoint so it can run as a Cloud Run service.
- For the bot, use `--min-instances 1` and `--no-cpu-throttling` so the worker keeps running outside request traffic.

## Required environment variables

Set these on both services unless noted otherwise:

- `KRAKEN_API_KEY`
- `KRAKEN_SECRET`
- `TRADE_HISTORY_BUCKET`
- `TRADE_HISTORY_OBJECT` or use the default `storico_trade.csv`

Set these only on the bot service:

- `TELEGRAM_TOKEN`
- `TELEGRAM_CHAT_ID`
- `KRAKEN_WITHDRAWAL_ACCOUNT` if you use the withdrawal flow

Optional dashboard auth:

- `STREAMLIT_AUTH_USERNAME`
- `STREAMLIT_AUTH_PASSWORD_HASH`

Optional:

- `MODALITA_PROVA=true` to simulate trades
- `SOGLIA_PRELIEVO_EUR=200`
- `TRADE_HISTORY_LOCAL_PATH=storico_trade.csv` for local runs

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
- deploys `dashboard` and `bot` to Cloud Run.

It reads the secret values from environment variables first, then prompts if they are not set:

- `KRAKEN_API_KEY`
- `KRAKEN_SECRET`
- `TELEGRAM_TOKEN`
- `TELEGRAM_CHAT_ID`
- `KRAKEN_WITHDRAWAL_ACCOUNT` is optional.
- `STREAMLIT_AUTH_USERNAME` and `STREAMLIT_AUTH_PASSWORD_HASH` are optional for dashboard login.

To generate a password hash for Streamlit auth:

```powershell
py -3 -c "import hashlib; print(hashlib.sha256(b'YOUR_PASSWORD').hexdigest())"
```

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
gcloud run deploy tradebot-bot \
  --image REGION-docker.pkg.dev/PROJECT/REPO/tradebot-bot:latest \
  --region REGION \
  --min-instances 1 \
  --no-cpu-throttling \
  --set-env-vars TRADE_HISTORY_BUCKET=YOUR_BUCKET \
  --set-secrets KRAKEN_API_KEY=kraken-api-key:latest,KRAKEN_SECRET=kraken-secret:latest,TELEGRAM_TOKEN=telegram-token:latest,TELEGRAM_CHAT_ID=telegram-chat-id:latest
```

If you want, I can also add a `gcloud` bootstrap script or Terraform for the bucket, secrets, IAM and deployments.
