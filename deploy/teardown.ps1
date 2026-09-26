# deploy/teardown.ps1
# Day 47 — Remove all Azure resources created by deploy.ps1
#
# Deletes the entire Resource Group which removes:
#   - Container App
#   - Container Apps Environment
#   - Azure Container Registry (and all images)
#   - All associated networking / logs
#
# Run from repo root:
#   .\deploy\teardown.ps1
#
# WARNING: This is irreversible. The SQLite DB in ACA storage is also deleted.
# Back up audit_logs data before running if you need it.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RESOURCE_GROUP = "rg-operations-agent"

Write-Host "`nThis will permanently delete:" -ForegroundColor Yellow
Write-Host "  Resource group: $RESOURCE_GROUP" -ForegroundColor Yellow
Write-Host "  All resources inside it (ACR, ACA, logs)" -ForegroundColor Yellow
$confirm = Read-Host "`nType 'yes' to confirm"

if ($confirm -ne "yes") {
    Write-Host "Cancelled." -ForegroundColor Green
    exit 0
}

Write-Host "`nDeleting resource group '$RESOURCE_GROUP'..." -ForegroundColor Cyan
az group delete --name $RESOURCE_GROUP --yes --no-wait
Write-Host "  Deletion queued (runs in background, takes ~2 min)." -ForegroundColor Green
Write-Host "  Check: az group show --name $RESOURCE_GROUP"