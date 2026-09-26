Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RESOURCE_GROUP  = "rg-operations-agent"
$LOCATION        = "eastus"
$ACR_NAME        = "opsagentacr"
$ACA_ENV_NAME    = "aca-env-operations-agent"
$ACA_APP_NAME    = "operations-agent"
$IMAGE_TAG       = "operations-agent:latest"
$TARGET_PORT     = 8000

Write-Host "[0/7] Loading .env..." -ForegroundColor Cyan
if (Test-Path ".env") {
    Get-Content ".env" | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line -match "^([^=]+)=(.*)`$") {
            $key   = $matches[1].Trim()
            $value = $matches[2].Trim().Trim('"').Trim("'")
            [System.Environment]::SetEnvironmentVariable($key, $value, "Process")
        }
    }
    Write-Host "  .env loaded" -ForegroundColor Green
}

Write-Host "[1/7] Creating Resource Group..." -ForegroundColor Cyan
az group create --name $RESOURCE_GROUP --location $LOCATION --output none
Write-Host "  Done" -ForegroundColor Green

Write-Host "[2/7] Creating ACR..." -ForegroundColor Cyan
az acr create --name $ACR_NAME --resource-group $RESOURCE_GROUP --sku Basic --admin-enabled true --output none
$ACR_SERVER = "$ACR_NAME.azurecr.io"
Write-Host "  ACR: $ACR_SERVER" -ForegroundColor Green

Write-Host "[3/7] Building image in Azure..." -ForegroundColor Cyan
az acr build --registry $ACR_NAME --image $IMAGE_TAG .
Write-Host "  Pushed" -ForegroundColor Green

Write-Host "[4/7] Creating ACA Environment..." -ForegroundColor Cyan
az containerapp env create --name $ACA_ENV_NAME --resource-group $RESOURCE_GROUP --location $LOCATION --output none
Write-Host "  Done" -ForegroundColor Green

Write-Host "[5/7] Deploying Container App..." -ForegroundColor Cyan
$envVars = @(
    "AZURE_OPENAI_API_KEY=$env:AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT=$env:AZURE_OPENAI_ENDPOINT",
    "MODEL_DEPLOYMENT_NAME=$env:MODEL_DEPLOYMENT_NAME",
    "AZURE_SEARCH_ENDPOINT=$env:AZURE_SEARCH_ENDPOINT",
    "AZURE_SEARCH_API_KEY=$env:AZURE_SEARCH_API_KEY",
    "AZURE_EMBEDDING_DEPLOYMENT=$env:AZURE_EMBEDDING_DEPLOYMENT",
    "DATABASE_URL=sqlite:////app/data/operations_agent.db",
    "PYTHONPATH=/app"
)
az containerapp create --name $ACA_APP_NAME --resource-group $RESOURCE_GROUP --environment $ACA_ENV_NAME --image "$ACR_SERVER/$IMAGE_TAG" --registry-server $ACR_SERVER --registry-identity system --target-port $TARGET_PORT --ingress external --min-replicas 1 --max-replicas 3 --cpu 0.5 --memory "1.0Gi" --env-vars $envVars --output none
Write-Host "  Deployed" -ForegroundColor Green

Write-Host "[6/7] Getting URL..." -ForegroundColor Cyan
$FQDN    = az containerapp show --name $ACA_APP_NAME --resource-group $RESOURCE_GROUP --query "properties.configuration.ingress.fqdn" --output tsv
$ACA_URL = "https://$FQDN"
Write-Host "  URL: $ACA_URL" -ForegroundColor Green

Write-Host "[7/7] Waiting for /health..." -ForegroundColor Cyan
$healthy = $false
for ($i = 1; $i -le 12; $i++) {
    try {
        $resp = Invoke-RestMethod -Uri "$ACA_URL/health" -Method GET -ErrorAction Stop
        if ($resp.status -eq "ok") { $healthy = $true; break }
    } catch {
        Write-Host "  Attempt $i — waiting 10s" -ForegroundColor DarkGray
        Start-Sleep -Seconds 10
    }
}

if ($healthy) {
    Write-Host "  /health ok" -ForegroundColor Green
} else {
    Write-Warning "  /health not responding — run: az containerapp logs show --name $ACA_APP_NAME --resource-group $RESOURCE_GROUP --follow"
}

Write-Host ""
Write-Host "URL:     $ACA_URL"
Write-Host "Health:  $ACA_URL/health"
Write-Host "Docs:    $ACA_URL/docs"
Write-Host "Metrics: $ACA_URL/metrics/dashboard"
Write-Host ""
Write-Host "Set before tests: $env:ACA_URL = '$ACA_URL'"
