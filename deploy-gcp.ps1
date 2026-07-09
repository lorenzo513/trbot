param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectId,

    [Parameter(Mandatory = $true)]
    [string]$Region,

    [string]$Repository = "tradebot",
    [string]$BucketName = "",
    [string]$RuntimeServiceAccount = "tradebot-runner",
    [string]$DashboardServiceName = "tradebot-dashboard",
    [string]$BotServiceName = "tradebot-bot",
    [string]$TradeHistoryObject = "storico_trade.csv",
    [string]$DashboardImageName = "tradebot-dashboard",
    [string]$BotImageName = "tradebot-bot",
    [string]$StreamlitAuthUsername = "",
    [string]$StreamlitAuthPasswordHash = ""
)

$ErrorActionPreference = "Stop"

function Assert-CommandExists {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command not found: $Name"
    }
}

function Get-PlainTextSecret {
    param(
        [string]$EnvName,
        [string]$Prompt
    )

    $value = [System.Environment]::GetEnvironmentVariable($EnvName)
    if ([string]::IsNullOrWhiteSpace($value)) {
        $value = Read-Host -Prompt $Prompt
    }

    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "Missing value for $EnvName"
    }

    return $value
}

function Ensure-Secret {
    param(
        [string]$Name,
        [string]$Value
    )

    $exists = $true
    & gcloud secrets describe $Name --project $ProjectId | Out-Null
    if ($LASTEXITCODE -ne 0) {
        $exists = $false
    }

    if (-not $exists) {
        & gcloud secrets create $Name --project $ProjectId --replication-policy="automatic" | Out-Null
    }

    $tempFile = New-TemporaryFile
    try {
        Set-Content -Path $tempFile.FullName -Value $Value -NoNewline
        & gcloud secrets versions add $Name --project $ProjectId --data-file=$tempFile.FullName | Out-Null
    }
    finally {
        Remove-Item $tempFile.FullName -Force -ErrorAction SilentlyContinue
    }
}

function Ensure-Repo {
    & gcloud artifacts repositories describe $Repository --project $ProjectId --location $Region | Out-Null
    if ($LASTEXITCODE -ne 0) {
        & gcloud artifacts repositories create $Repository `
            --project $ProjectId `
            --location $Region `
            --repository-format docker | Out-Null
    }
}

function Build-Image {
    param(
        [string]$Dockerfile,
        [string]$ImageName
    )

    if ([string]::IsNullOrWhiteSpace($ImageName)) {
        throw "ImageName cannot be empty."
    }

    $image = "${Region}-docker.pkg.dev/${ProjectId}/${Repository}/${ImageName}:latest"
    $buildConfig = New-TemporaryFile
    $buildConfigPath = $buildConfig.FullName

    @"
steps:
  - name: gcr.io/cloud-builders/docker
    args:
      - build
      - -f
      - $Dockerfile
      - -t
      - $image
      - .
images:
  - $image
"@ | Set-Content -Path $buildConfigPath

    try {
        & gcloud builds submit --project $ProjectId --quiet --config $buildConfigPath . | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Cloud Build failed for image $image."
        }
    }
    finally {
        Remove-Item $buildConfigPath -Force -ErrorAction SilentlyContinue
    }

    Write-Output $image
}

Assert-CommandExists -Name "gcloud"

& gcloud config set project $ProjectId | Out-Null
& gcloud services enable run.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com cloudbuild.googleapis.com storage.googleapis.com | Out-Null

if ([string]::IsNullOrWhiteSpace($BucketName)) {
    $BucketName = "$ProjectId-tradebot-history"
}

$withdrawalAccount = [System.Environment]::GetEnvironmentVariable("KRAKEN_WITHDRAWAL_ACCOUNT")

if (-not [string]::IsNullOrWhiteSpace($withdrawalAccount)) {
    Ensure-Secret -Name "kraken-withdrawal-account" -Value $withdrawalAccount
}

& gcloud storage buckets describe gs://$BucketName --project $ProjectId | Out-Null
if ($LASTEXITCODE -ne 0) {
    & gcloud storage buckets create gs://$BucketName --project $ProjectId --location $Region | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create bucket gs://$BucketName"
    }
}

Ensure-Repo

$runtimeServiceAccountEmail = "$RuntimeServiceAccount@$ProjectId.iam.gserviceaccount.com"
& gcloud iam service-accounts describe $runtimeServiceAccountEmail --project $ProjectId | Out-Null
if ($LASTEXITCODE -ne 0) {
    & gcloud iam service-accounts create $RuntimeServiceAccount --project $ProjectId --display-name "Tradebot runtime service account" | Out-Null
}

& gcloud projects add-iam-policy-binding $ProjectId `
    --member "serviceAccount:$runtimeServiceAccountEmail" `
    --role "roles/secretmanager.secretAccessor" | Out-Null
& gcloud projects add-iam-policy-binding $ProjectId `
    --member "serviceAccount:$runtimeServiceAccountEmail" `
    --role "roles/storage.objectAdmin" | Out-Null
& gcloud projects add-iam-policy-binding $ProjectId `
    --member "serviceAccount:$runtimeServiceAccountEmail" `
    --role "roles/artifactregistry.reader" | Out-Null

$dashboardImage = Build-Image -Dockerfile "Dockerfile.dashboard" -ImageName $DashboardImageName
$botImage = Build-Image -Dockerfile "Dockerfile.bot" -ImageName $BotImageName

$dashboardSecrets = "KRAKEN_API_KEY=kraken-api-key:latest,KRAKEN_SECRET=kraken-secret:latest"

& gcloud run deploy $DashboardServiceName `
    --project $ProjectId `
    --region $Region `
    --image=$dashboardImage `
    --service-account $runtimeServiceAccountEmail `
    --allow-unauthenticated `
    --set-env-vars "TRADE_HISTORY_BUCKET=$BucketName,TRADE_HISTORY_OBJECT=$TradeHistoryObject" `
    --set-secrets $dashboardSecrets | Out-Null

$botSecrets = "KRAKEN_API_KEY=kraken-api-key:latest,KRAKEN_SECRET=kraken-secret:latest,TELEGRAM_TOKEN=telegram-token:latest,TELEGRAM_CHAT_ID=telegram-chat-id:latest"
if (-not [string]::IsNullOrWhiteSpace($withdrawalAccount)) {
    $botSecrets += ",KRAKEN_WITHDRAWAL_ACCOUNT=kraken-withdrawal-account:latest"
}

& gcloud run deploy $BotServiceName `
    --project $ProjectId `
    --region $Region `
    --image=$botImage `
    --service-account $runtimeServiceAccountEmail `
    --min-instances 1 `
    --no-cpu-throttling `
    --set-env-vars "TRADE_HISTORY_BUCKET=$BucketName,TRADE_HISTORY_OBJECT=$TradeHistoryObject" `
    --set-secrets $botSecrets | Out-Null

Write-Host "Deploy completed."
Write-Host "Dashboard service: $DashboardServiceName"
Write-Host "Bot service: $BotServiceName"
