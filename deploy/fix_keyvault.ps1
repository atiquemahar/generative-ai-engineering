# deploy/fix_keyvault.ps1
# Fixes the Day 48 Key Vault setup after the permission gap in setup_keyvault.ps1.
#
# What went wrong:
#   - Your login identity (abdumahar28@gmail.com) only had "Key Vault Secrets User"
#     (read-only). Writing secrets requires "Key Vault Secrets Officer".
#   - So all 7 az keyvault secret set calls silently failed -> zero secrets in vault.
#   - ACA steps 5+6 then failed because there was nothing to reference.
#
# What this script does:
#   Step A: Grant YOUR identity "Key Vault Secrets Officer" on the vault
#   Step B: Write all 6 required secrets (reads from your .env)
#   Step C: Set Key Vault references on the Container App
#   Step D: Replace plaintext env vars with secretref: pointers
#   Step E: Verify /health after restart
#
# Run from repo root:
#   .\deploy\fix_keyvault.ps1

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RESOURCE_GROUP = "resource-group"
$ACA_APP_NAME   = "aca-app-name"
$KV_NAME        = "kv-name"
$KV_URI         = "https://$KV_NAME.vault.azure.net"
$SUBSCRIPTION   = "subscription-id"

# ── Load .env ─────────────────────────────────────────────────────────────────
Write-Host "`n[0/5] Loading .env..." -ForegroundColor Cyan
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
    Write-Warning "  .env not found - env vars must already be set in your shell"
}

# ── Step A: Grant YOUR identity Key Vault Secrets Officer ─────────────────────
# Your user OID from the diagnosis output (oid= in the Caller line)
Write-Host "`n[1/5] Granting YOUR identity 'Key Vault Secrets Officer' on vault..." -ForegroundColor Cyan

$KV_ID   = az keyvault show --name $KV_NAME --resource-group $RESOURCE_GROUP --query "id" -o tsv
$MY_OID  = az ad signed-in-user show --query "id" -o tsv

Write-Host "  Your OID:  $MY_OID"
Write-Host "  Vault ID:  $KV_ID"

# Check if the role is already assigned (idempotent)
$existing = az role assignment list `
    --assignee $MY_OID `
    --scope    $KV_ID `
    --query    "[?roleDefinitionName=='Key Vault Secrets Officer'].id" `
    -o tsv 2>$null

if ($existing) {
    Write-Host "  Already has Key Vault Secrets Officer - skipping assignment" -ForegroundColor DarkGray
} else {
    az role assignment create `
        --assignee $MY_OID `
        --role     "Key Vault Secrets Officer" `
        --scope    $KV_ID `
        --output   none
    Write-Host "  Role assigned - waiting 60s for AAD propagation..." -ForegroundColor Green
    Start-Sleep -Seconds 60
}

# ── Step B: Write all required secrets ────────────────────────────────────────
Write-Host "`n[2/5] Writing secrets to Key Vault..." -ForegroundColor Cyan

# Secrets that MUST exist (required by config.py REQUIRED_VARS)
$required = [ordered]@{
    "AZURE-OPENAI-API-KEY"       = $env:AZURE_OPENAI_API_KEY
    "AZURE-OPENAI-ENDPOINT"      = $env:AZURE_OPENAI_ENDPOINT
    "MODEL-DEPLOYMENT-NAME"      = $env:MODEL_DEPLOYMENT_NAME
    "AZURE-SEARCH-ENDPOINT"      = $env:AZURE_SEARCH_ENDPOINT
    "AZURE-SEARCH-API-KEY"       = $env:AZURE_SEARCH_API_KEY
    "AZURE-EMBEDDING-DEPLOYMENT" = $env:AZURE_EMBEDDING_DEPLOYMENT
}

# Optional secrets (app works without them)
$optional = [ordered]@{
    "AZURE-PROJECT-ENDPOINT"                = $env:AZURE_PROJECT_ENDPOINT
    "APPLICATIONINSIGHTS-CONNECTION-STRING" = $env:APPLICATIONINSIGHTS_CONNECTION_STRING
}

$writeErrors = @()

foreach ($name in $required.Keys) {
    $value = $required[$name]
    if (-not $value) {
        $writeErrors += "  MISSING in .env: $name (check your .env file)"
        continue
    }
    try {
        az keyvault secret set --vault-name $KV_NAME --name $name --value $value --output none
        Write-Host "  Wrote (required): $name" -ForegroundColor Green
    } catch {
        $writeErrors += "  FAILED to write: $name -> $_"
    }
}

foreach ($name in $optional.Keys) {
    $value = $optional[$name]
    if ($value) {
        try {
            az keyvault secret set --vault-name $KV_NAME --name $name --value $value --output none
            Write-Host "  Wrote (optional): $name" -ForegroundColor Green
        } catch {
            Write-Host "  Skipped (optional, write failed): $name" -ForegroundColor DarkGray
        }
    } else {
        Write-Host "  Skipped (optional, empty): $name" -ForegroundColor DarkGray
    }
}

if ($writeErrors) {
    Write-Host "`n  ERRORS in Step B:" -ForegroundColor Red
    $writeErrors | ForEach-Object { Write-Host $_ -ForegroundColor Red }
    Write-Host "`n  Fix your .env and re-run. Aborting." -ForegroundColor Red
    exit 1
}

# Verify all required secrets are in vault before proceeding
Write-Host "`n  Verifying secrets in vault..." -ForegroundColor Cyan
$inVault = az keyvault secret list --vault-name $KV_NAME --query "[].name" -o tsv
foreach ($name in $required.Keys) {
    $lower = $name.ToLower()
    if ($inVault -notcontains $lower) {
        Write-Host "  MISSING from vault: $name" -ForegroundColor Red
        exit 1
    }
    Write-Host "  Confirmed in vault: $lower" -ForegroundColor Green
}

# ── Step C: Set Key Vault references on the Container App ─────────────────────
# Format: <aca-secret-name>=keyvaultref:<kv-secret-uri>,identityref:system
# Only reference secrets that actually exist in the vault.
Write-Host "`n[3/5] Setting Key Vault references on Container App..." -ForegroundColor Cyan

$acaSecrets = [System.Collections.Generic.List[string]]::new()
$acaSecrets.Add("azure-openai-key=keyvaultref:$KV_URI/secrets/AZURE-OPENAI-API-KEY,identityref:system")
$acaSecrets.Add("azure-openai-endpoint=keyvaultref:$KV_URI/secrets/AZURE-OPENAI-ENDPOINT,identityref:system")
$acaSecrets.Add("model-deployment-name=keyvaultref:$KV_URI/secrets/MODEL-DEPLOYMENT-NAME,identityref:system")
$acaSecrets.Add("azure-search-endpoint=keyvaultref:$KV_URI/secrets/AZURE-SEARCH-ENDPOINT,identityref:system")
$acaSecrets.Add("azure-search-key=keyvaultref:$KV_URI/secrets/AZURE-SEARCH-API-KEY,identityref:system")
$acaSecrets.Add("azure-embedding-deployment=keyvaultref:$KV_URI/secrets/AZURE-EMBEDDING-DEPLOYMENT,identityref:system")

# Only add optional secrets if they exist in vault
if ($inVault -contains "azure-project-endpoint") {
    $acaSecrets.Add("azure-project-endpoint=keyvaultref:$KV_URI/secrets/AZURE-PROJECT-ENDPOINT,identityref:system")
}
if ($inVault -contains "applicationinsights-connection-string") {
    $acaSecrets.Add("appinsights-conn=keyvaultref:$KV_URI/secrets/APPLICATIONINSIGHTS-CONNECTION-STRING,identityref:system")
}

az containerapp secret set `
    --name           $ACA_APP_NAME `
    --resource-group $RESOURCE_GROUP `
    --secrets        ($acaSecrets.ToArray()) `
    --output         none

Write-Host "  Key Vault references set ($($acaSecrets.Count) secrets)" -ForegroundColor Green

# ── Step D: Replace plaintext env vars with secretref: pointers ───────────────
Write-Host "`n[4/5] Replacing plaintext env vars with secretref: pointers..." -ForegroundColor Cyan

$envVars = @(
    "AZURE_OPENAI_API_KEY=secretref:azure-openai-key",
    "AZURE_OPENAI_ENDPOINT=secretref:azure-openai-endpoint",
    "MODEL_DEPLOYMENT_NAME=secretref:model-deployment-name",
    "AZURE_SEARCH_ENDPOINT=secretref:azure-search-endpoint",
    "AZURE_SEARCH_API_KEY=secretref:azure-search-key",
    "AZURE_EMBEDDING_DEPLOYMENT=secretref:azure-embedding-deployment",
    "DATABASE_URL=sqlite:////app/data/operations_agent.db",
    "PYTHONPATH=/app"
)

az containerapp update `
    --name           $ACA_APP_NAME `
    --resource-group $RESOURCE_GROUP `
    --set-env-vars   $envVars `
    --output         none

Write-Host "  Env vars updated - container restarting..." -ForegroundColor Green

# ── Step E: Verify /health ─────────────────────────────────────────────────────
Write-Host "`n[5/5] Verifying /health (up to 90s for restart)..." -ForegroundColor Cyan

$FQDN    = az containerapp show --name $ACA_APP_NAME --resource-group $RESOURCE_GROUP `
               --query "properties.configuration.ingress.fqdn" -o tsv
$ACA_URL = "https://$FQDN"
$healthy = $false

for ($i = 1; $i -le 18; $i++) {
    try {
        $resp = Invoke-RestMethod -Uri "$ACA_URL/health" -Method GET -ErrorAction Stop
        if ($resp.status -eq "ok") { $healthy = $true; break }
    } catch {
        Write-Host "  Attempt $i/18 - waiting 5s..." -ForegroundColor DarkGray
        Start-Sleep -Seconds 5
    }
}

Write-Host ""
if ($healthy) {
    Write-Host "SUCCESS" -ForegroundColor Green
    Write-Host "  /health returned ok"
    Write-Host "  All Key Vault references resolving correctly"
    Write-Host ""
    Write-Host "Run to confirm Day 48 test 1 passes:"
    Write-Host "  `$env:ACA_URL = '$ACA_URL'"
    Write-Host "  pytest experiments/day48_keyvault.py -v"
} else {
    Write-Host "HEALTH CHECK FAILED" -ForegroundColor Red
    Write-Host "  Check logs:"
    Write-Host "  az containerapp logs show --name $ACA_APP_NAME --resource-group $RESOURCE_GROUP --follow"
}

Write-Host ""
Write-Host "====================================" -ForegroundColor Cyan
Write-Host " Key Vault: $KV_URI"
Write-Host " App URL:   $ACA_URL"
Write-Host " Secrets written:  $($required.Count) required"
Write-Host "====================================" -ForegroundColor Cyan