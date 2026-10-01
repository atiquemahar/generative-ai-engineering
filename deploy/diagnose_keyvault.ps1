# deploy/diagnose_keyvault.ps1
# Run this to see exactly what went wrong and what to fix.
# Usage: .\deploy\diagnose_keyvault.ps1

$KV_NAME        = "kv-name"
$RESOURCE_GROUP = "resource-group"
$ACA_APP_NAME   = "aca-app-name"
$SUBSCRIPTION   = "subscription-id"

Write-Host "`n=== DIAGNOSIS: Key Vault + Managed Identity ===" -ForegroundColor Cyan

# ── 1. Who am I logged in as? ─────────────────────────────────────────────────
Write-Host "`n[1] Current az login identity:" -ForegroundColor Yellow
az account show --query "{user:user.name, type:user.type, subscription:name}" -o table

# ── 2. What secrets actually made it into Key Vault? ──────────────────────────
Write-Host "`n[2] Secrets currently in Key Vault '$KV_NAME':" -ForegroundColor Yellow
az keyvault secret list --vault-name $KV_NAME --query "[].name" -o table 2>&1

# ── 3. Does MY identity have permission to write secrets? ─────────────────────
Write-Host "`n[3] My role assignments on Key Vault:" -ForegroundColor Yellow
$KV_ID = az keyvault show --name $KV_NAME --resource-group $RESOURCE_GROUP --query "id" -o tsv 2>$null
if ($KV_ID) {
    az role assignment list --scope $KV_ID --query "[].{Role:roleDefinitionName, Principal:principalName}" -o table 2>&1
} else {
    Write-Warning "Could not find Key Vault ID - vault may not exist yet"
}

# ── 4. What is the Container App's managed identity principal? ────────────────
Write-Host "`n[4] Container App managed identity:" -ForegroundColor Yellow
az containerapp identity show `
    --name $ACA_APP_NAME `
    --resource-group $RESOURCE_GROUP `
    --query "{type:type, principalId:principalId}" `
    -o table 2>&1

# ── 5. Does the managed identity have the right role on Key Vault? ────────────
Write-Host "`n[5] Managed identity role assignments on Key Vault:" -ForegroundColor Yellow
$PRINCIPAL_ID = az containerapp identity show `
    --name $ACA_APP_NAME `
    --resource-group $RESOURCE_GROUP `
    --query "principalId" -o tsv 2>$null
if ($PRINCIPAL_ID) {
    az role assignment list `
        --assignee $PRINCIPAL_ID `
        --scope $KV_ID `
        --query "[].{Role:roleDefinitionName, Scope:scope}" `
        -o table 2>&1
    Write-Host "  Principal ID: $PRINCIPAL_ID"
} else {
    Write-Warning "Could not get managed identity principal ID"
}

# ── 6. Key Vault access policy mode (RBAC vs legacy policy)? ─────────────────
Write-Host "`n[6] Key Vault authorization mode:" -ForegroundColor Yellow
az keyvault show `
    --name $KV_NAME `
    --resource-group $RESOURCE_GROUP `
    --query "{enableRbacAuthorization:properties.enableRbacAuthorization, softDelete:properties.enableSoftDelete}" `
    -o table 2>&1

# ── 7. Current ACA secret configuration ───────────────────────────────────────
Write-Host "`n[7] Current Container App secrets (names only, no values):" -ForegroundColor Yellow
az containerapp secret list `
    --name $ACA_APP_NAME `
    --resource-group $RESOURCE_GROUP `
    --query "[].{Name:name, Source:keyVaultUrl}" `
    -o table 2>&1

Write-Host "`n=== END DIAGNOSIS ===" -ForegroundColor Cyan
Write-Host "Paste this output back to identify the exact fix needed." -ForegroundColor DarkGray