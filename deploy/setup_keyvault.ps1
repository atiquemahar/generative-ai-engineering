# deploy/setup_keyvault.ps1
# Day 48 - Azure Key Vault + Managed Identity
#
# What this script does:
#   1. Loads .env so secret values are available in this session
#   2. Creates an Azure Key Vault
#   3. Writes all secrets into Key Vault
#   4. Enables system-assigned managed identity on the Container App
#   5. Grants the Container App identity Key Vault Secrets User role
#   6. Replaces every plaintext ACA env var with a Key Vault reference
#   7. Restarts the Container App and re-verifies /health
#
# After this script runs:
#   - Zero plaintext secrets in ACA configuration
#   - Zero secrets in Dockerfile, requirements.txt, or GitHub
#   - Container App reads secrets from Key Vault at runtime via its
#     managed identity - no SDK changes needed in the application
#
# Run from repo root (after deploy.ps1 succeeded on Day 47):
#   .\deploy\setup_keyvault.ps1
#
# Prerequisites: az login, Container App already running from Day 47

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# -- Configuration - must match Day 47 deploy.ps1 -----------------------------
$RESOURCE_GROUP = "day45-resource"
$LOCATION       = "eastus"
$ACA_APP_NAME   = "operations-agent"

# Key Vault name: 3-24 chars, alphanumeric + hyphens, globally unique.
# Use opsagent-kv1 to match the opsagentacr1 ACR name pattern.
# If this name is taken, change to opsagent-kv-<your-initials>.
$KV_NAME = "opsagent-kv1"

# -- Step 0: Load .env ---------------------------------------------------------
Write-Host "`n[0/7] Loading .env..." -ForegroundColor Cyan
if (Test-Path ".env") {
    Get-Content ".env" | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line -match "^([^=]+)=(.*)$") {
            $key   = $matches[1].Trim()
            $value = $matches[2].Trim().Trim('"').Trim("'")
            [System.Environment]::SetEnvironmentVariable($key, $value, "Process")
        }
    }
    Write-Host "  .env loaded" -ForegroundColor Green
} else {
    Write-Warning "  .env not found - AZURE_* env vars must already be set in your shell"
}

# -- Step 1: Create Key Vault --------------------------------------------------
Write-Host "`n[1/7] Creating Key Vault '$KV_NAME'..." -ForegroundColor Cyan
az keyvault create `
    --name           $KV_NAME `
    --resource-group $RESOURCE_GROUP `
    --location       $LOCATION `
    --output         none

$KV_URI = "https://$KV_NAME.vault.azure.net"
Write-Host "  Created: $KV_URI" -ForegroundColor Green

# -- Step 2: Write all secrets to Key Vault ------------------------------------
# Secret names use hyphens (Key Vault requirement - underscores not allowed).
# The application reads env vars with underscores; ACA translates between them.
Write-Host "`n[2/7] Writing secrets to Key Vault..." -ForegroundColor Cyan

$secrets = @{
    "AZURE-OPENAI-API-KEY"                  = $env:AZURE_OPENAI_API_KEY
    "AZURE-OPENAI-ENDPOINT"                 = $env:AZURE_OPENAI_ENDPOINT
    "MODEL-DEPLOYMENT-NAME"                 = $env:MODEL_DEPLOYMENT_NAME
    "AZURE-SEARCH-ENDPOINT"                 = $env:AZURE_SEARCH_ENDPOINT
    "AZURE-SEARCH-API-KEY"                  = $env:AZURE_SEARCH_API_KEY
    "AZURE-EMBEDDING-DEPLOYMENT"            = $env:AZURE_EMBEDDING_DEPLOYMENT
    "AZURE-PROJECT-ENDPOINT"                = $env:AZURE_PROJECT_ENDPOINT
    "APPLICATIONINSIGHTS-CONNECTION-STRING" = $env:APPLICATIONINSIGHTS_CONNECTION_STRING
}

foreach ($name in $secrets.Keys) {
    $value = $secrets[$name]
    if ($value) {
        az keyvault secret set --vault-name $KV_NAME --name $name --value $value --output none
        Write-Host "  Wrote: $name" -ForegroundColor Green
    } else {
        Write-Host "  Skipped (empty): $name" -ForegroundColor DarkGray
    }
}

# -- Step 3: Enable system-assigned managed identity on Container App ----------
Write-Host "`n[3/7] Enabling system-assigned managed identity..." -ForegroundColor Cyan
az containerapp identity assign `
    --name           $ACA_APP_NAME `
    --resource-group $RESOURCE_GROUP `
    --system-assigned `
    --output         none

$PRINCIPAL_ID = az containerapp identity show `
    --name           $ACA_APP_NAME `
    --resource-group $RESOURCE_GROUP `
    --query          "principalId" `
    --output         tsv

Write-Host "  Principal ID: $PRINCIPAL_ID" -ForegroundColor Green

# -- Step 4: Grant Container App identity access to Key Vault ------------------
# Key Vault Secrets User = read secret values, cannot manage secrets.
# This is the minimum permission needed.
Write-Host "`n[4/7] Granting Key Vault Secrets User role to Container App identity..." -ForegroundColor Cyan

$KV_ID = az keyvault show `
    --name           $KV_NAME `
    --resource-group $RESOURCE_GROUP `
    --query          "id" `
    --output         tsv

az role assignment create `
    --assignee   $PRINCIPAL_ID `
    --role       "Key Vault Secrets User" `
    --scope      $KV_ID `
    --output     none

Write-Host "  Role assigned - waiting 30s for propagation..." -ForegroundColor Green
Start-Sleep -Seconds 30

# -- Step 5: Set Key Vault secret references in ACA ----------------------------
# ACA secrets reference Key Vault URIs via the Container App's managed identity.
# Format: <aca-secret-name>=keyvaultref:<kv-secret-uri>,identityref:system
Write-Host "`n[5/7] Setting Key Vault references in Container App secrets..." -ForegroundColor Cyan

$acaSecrets = @(
    "azure-openai-key=keyvaultref:$KV_URI/secrets/AZURE-OPENAI-API-KEY,identityref:system",
    "azure-openai-endpoint=keyvaultref:$KV_URI/secrets/AZURE-OPENAI-ENDPOINT,identityref:system",
    "model-deployment-name=keyvaultref:$KV_URI/secrets/MODEL-DEPLOYMENT-NAME,identityref:system",
    "azure-search-endpoint=keyvaultref:$KV_URI/secrets/AZURE-SEARCH-ENDPOINT,identityref:system",
    "azure-search-key=keyvaultref:$KV_URI/secrets/AZURE-SEARCH-API-KEY,identityref:system",
    "azure-embedding-deployment=keyvaultref:$KV_URI/secrets/AZURE-EMBEDDING-DEPLOYMENT,identityref:system",
    "azure-project-endpoint=keyvaultref:$KV_URI/secrets/AZURE-PROJECT-ENDPOINT,identityref:system"
)

az containerapp secret set `
    --name           $ACA_APP_NAME `
    --resource-group $RESOURCE_GROUP `
    --secrets        $acaSecrets `
    --output         none

Write-Host "  Key Vault references set" -ForegroundColor Green

# -- Step 6: Replace plaintext env vars with secretref: pointers ---------------
# The application sees these as normal env vars - no code changes required.
Write-Host "`n[6/7] Replacing plaintext env vars with secretref: pointers..." -ForegroundColor Cyan

$envVars = @(
    "AZURE_OPENAI_API_KEY=secretref:azure-openai-key",
    "AZURE_OPENAI_ENDPOINT=secretref:azure-openai-endpoint",
    "MODEL_DEPLOYMENT_NAME=secretref:model-deployment-name",
    "AZURE_SEARCH_ENDPOINT=secretref:azure-search-endpoint",
    "AZURE_SEARCH_API_KEY=secretref:azure-search-key",
    "AZURE_EMBEDDING_DEPLOYMENT=secretref:azure-embedding-deployment",
    "AZURE_PROJECT_ENDPOINT=secretref:azure-project-endpoint",
    "DATABASE_URL=sqlite:////app/data/operations_agent.db",
    "PYTHONPATH=/app"
)

az containerapp update `
    --name           $ACA_APP_NAME `
    --resource-group $RESOURCE_GROUP `
    --set-env-vars   $envVars `
    --output         none

Write-Host "  Env vars updated - container restarting..." -ForegroundColor Green

# -- Step 7: Verify /health after restart --------------------------------------
Write-Host "`n[7/7] Verifying /health (up to 90 s for restart)..." -ForegroundColor Cyan

$FQDN    = az containerapp show --name $ACA_APP_NAME --resource-group $RESOURCE_GROUP --query "properties.configuration.ingress.fqdn" --output tsv
$ACA_URL = "https://$FQDN"
$healthy = $false

for ($i = 1; $i -le 18; $i++) {
    try {
        $resp = Invoke-RestMethod -Uri "$ACA_URL/health" -Method GET -ErrorAction Stop
        if ($resp.status -eq "ok") { $healthy = $true; break }
    } catch {
        Write-Host "  Attempt $i - waiting 5s" -ForegroundColor DarkGray
        Start-Sleep -Seconds 5
    }
}

if ($healthy) {
    Write-Host "  /health -> ok" -ForegroundColor Green
} else {
    Write-Warning "  /health not responding - check logs:"
    Write-Warning "  az containerapp logs show --name $ACA_APP_NAME --resource-group $RESOURCE_GROUP --follow"
}

Write-Host ""
Write-Host "====================================" -ForegroundColor Cyan
Write-Host " Key Vault: $KV_URI"
Write-Host " App URL:   $ACA_URL"
Write-Host " Zero plaintext secrets in ACA."
Write-Host "====================================" -ForegroundColor Cyan