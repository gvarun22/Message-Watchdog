#Requires -Version 5.1
# =============================================================================
# Message Watchdog — One-time Azure infrastructure setup
# =============================================================================
# Run this ONCE from your local machine before the first deployment.
# Safe to re-run — all Azure create operations are idempotent.
#
# Prerequisites:
#   az login       (Azure CLI, logged in to the correct subscription)
#   gh auth login  (GitHub CLI, authenticated to your account)
#
# Usage:
#   .\scripts\azure-setup.ps1 -SubscriptionId "xxx" -GitHubRepo "user/Message-Watchdog"
#
# All other parameters have defaults matching the project conventions but can
# be overridden if your environment uses different names:
#   .\scripts\azure-setup.ps1 -SubscriptionId "xxx" -GitHubRepo "user/repo" `
#       -AcrName "myacr" -KeyVaultName "my-kv"
# =============================================================================
param(
    [Parameter(Mandatory = $true, HelpMessage = "Azure subscription ID (az account show --query id -o tsv)")]
    [string]$SubscriptionId,

    [Parameter(Mandatory = $true, HelpMessage = "GitHub repo in owner/name format, e.g. jsmith/Message-Watchdog")]
    [string]$GitHubRepo,

    [string]$ResourceGroup   = "Message-Watchdog",
    [string]$Location        = "eastus",
    [string]$AcrName         = "messagewatchdog",    # globally unique, alphanumeric only
    [string]$KeyVaultName    = "watchdog-kv",        # globally unique, 3-24 chars
    [string]$StorageAccount  = "",                    # auto-generated from subscription ID if not specified
    [string]$FileShare       = "watchdog-session",
    [string]$IdentityName    = "message-watchdog-id",
    [string]$AciName         = "message-watchdog"
)

$ErrorActionPreference = "Stop"

function Write-Info  { param($msg) Write-Host "[+] $msg" -ForegroundColor Green }
function Write-Warn  { param($msg) Write-Host "[!] $msg" -ForegroundColor Yellow }
function Write-Fatal { param($msg) Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }

# Accept full GitHub URL or owner/repo — normalise to owner/repo
if ($GitHubRepo -match "^https?://github\.com/(.+?)(?:\.git)?$") {
    $GitHubRepo = $Matches[1]
}

# ---------------------------------------------------------------------------
# Validate prerequisites
# ---------------------------------------------------------------------------
if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    Write-Fatal "Azure CLI not found. Install from https://aka.ms/install-azure-cli"
}
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Fatal "GitHub CLI not found. Install from https://cli.github.com"
}

# Verify GitHub CLI is authenticated
Write-Info "Verifying GitHub CLI authentication..."
gh auth status 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Fatal "GitHub CLI not authenticated. Run: gh auth login"
}

# Verify the CLI token is valid before doing anything else
Write-Info "Verifying Azure CLI authentication..."
az account show --query id -o tsv 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Fatal "Azure CLI token expired or not logged in. Run:`n  az logout`n  az login`nthen re-run this script."
}

az account set --subscription $SubscriptionId
if ($LASTEXITCODE -ne 0) {
    Write-Fatal "Could not set subscription '$SubscriptionId'. Run 'az account list' to see available subscriptions."
}
$currentSub = az account show --query id -o tsv
Write-Info "Using subscription: $currentSub"

# Auto-generate storage account name from subscription ID if not provided
if (-not $StorageAccount) {
    $StorageAccount = "watchdog" + ($SubscriptionId -replace "-","").Substring(0,10)
    Write-Info "Storage account name auto-generated: '$StorageAccount' (override with -StorageAccount)"
}

# ---------------------------------------------------------------------------
# Register required resource providers (idempotent, skipped if already registered)
# ---------------------------------------------------------------------------
Write-Info "Registering required resource providers (this may take ~60s on first run)..."
foreach ($ns in @(
    "Microsoft.ContainerRegistry",
    "Microsoft.KeyVault",
    "Microsoft.ContainerInstance",
    "Microsoft.ManagedIdentity",
    "Microsoft.Storage"
)) {
    $state = az provider show --namespace $ns --query registrationState -o tsv 2>$null
    if ($state -ne "Registered") {
        Write-Info "  Registering $ns..."
        az provider register --namespace $ns --wait --output none
    } else {
        Write-Info "  $ns already registered."
    }
}

# ---------------------------------------------------------------------------
# Resource group — detect existing location to avoid InvalidResourceGroupLocation
# ---------------------------------------------------------------------------
$existingLocation = az group show --name $ResourceGroup --query location -o tsv 2>$null
if ($LASTEXITCODE -eq 0 -and $existingLocation) {
    $Location = $existingLocation
    Write-Info "Resource group '$ResourceGroup' already exists in '$Location' — using that location."
} else {
    Write-Info "Creating resource group '$ResourceGroup' in $Location..."
    az group create --name $ResourceGroup --location $Location --output none
    if ($LASTEXITCODE -ne 0) { Write-Fatal "Failed to create resource group." }
}

# ---------------------------------------------------------------------------
# Azure Container Registry
# ---------------------------------------------------------------------------
Write-Info "Creating ACR '$AcrName'..."
az acr create `
    --resource-group $ResourceGroup `
    --name $AcrName `
    --sku Basic `
    --admin-enabled false `
    --output none

$acrId       = az acr show --name $AcrName --query id -o tsv
$acrRegistry = "$AcrName.azurecr.io"
Write-Info "ACR: $acrRegistry"

# ---------------------------------------------------------------------------
# Azure Key Vault
# ---------------------------------------------------------------------------
Write-Info "Creating Key Vault '$KeyVaultName'..."
az keyvault create `
    --resource-group $ResourceGroup `
    --name $KeyVaultName `
    --location $Location `
    --enable-rbac-authorization true `
    --output none

$kvId        = az keyvault show --name $KeyVaultName --query id -o tsv
$keyVaultUrl = "https://$KeyVaultName.vault.azure.net/"
Write-Info "Key Vault: $keyVaultUrl"

# Grant the current user Secrets Officer so we can upload secrets below
$currentUserId = az ad signed-in-user show --query id -o tsv
Write-Info "Granting Key Vault Secrets Officer to current user..."
az role assignment create `
    --assignee $currentUserId `
    --role "Key Vault Secrets Officer" `
    --scope $kvId `
    --output none

# ---------------------------------------------------------------------------
# Storage account + file share (source session file persistence)
# ---------------------------------------------------------------------------
$storageExists = az storage account show `
    --name $StorageAccount `
    --resource-group $ResourceGroup `
    --query id -o tsv 2>$null
if ($storageExists) {
    Write-Info "Storage account '$StorageAccount' already exists — skipping creation."
} else {
    Write-Info "Creating storage account '$StorageAccount'..."
    az storage account create `
        --name $StorageAccount `
        --resource-group $ResourceGroup `
        --location $Location `
        --sku Standard_LRS `
        --allow-blob-public-access false `
        --output none
    if ($LASTEXITCODE -ne 0) {
        Write-Fatal "Failed to create storage account '$StorageAccount'. If the name is globally taken by another account, re-run with: -StorageAccount <unique-name>"
    }
}

$storageKey = az storage account keys list `
    --account-name $StorageAccount `
    --resource-group $ResourceGroup `
    --query "[0].value" -o tsv
if (-not $storageKey) { Write-Fatal "Could not retrieve key for storage account '$StorageAccount'." }

$shareExists = az storage share exists `
    --name $FileShare `
    --account-name $StorageAccount `
    --account-key $storageKey `
    --query exists -o tsv 2>$null
if ($shareExists -eq "true") {
    Write-Info "File share '$FileShare' already exists — skipping creation."
} else {
    Write-Info "Creating file share '$FileShare'..."
    az storage share create `
        --name $FileShare `
        --account-name $StorageAccount `
        --account-key $storageKey `
        --output none
    if ($LASTEXITCODE -ne 0) { Write-Fatal "Failed to create file share '$FileShare'." }
}

Write-Info "Storing storage account key in Key Vault..."
az keyvault secret set `
    --vault-name $KeyVaultName `
    --name "storage-account-key" `
    --value $storageKey `
    --output none

# ---------------------------------------------------------------------------
# User-assigned managed identity (for the ACI container)
# ---------------------------------------------------------------------------
Write-Info "Creating managed identity '$IdentityName'..."
az identity create `
    --resource-group $ResourceGroup `
    --name $IdentityName `
    --output none

$identityId        = az identity show --resource-group $ResourceGroup --name $IdentityName --query id -o tsv
$identityPrincipal = az identity show --resource-group $ResourceGroup --name $IdentityName --query principalId -o tsv

Write-Info "Waiting 30s for identity to propagate in AAD..."
Start-Sleep -Seconds 30

Write-Info "Assigning AcrPull to container identity..."
az role assignment create `
    --assignee-object-id $identityPrincipal `
    --assignee-principal-type ServicePrincipal `
    --role "AcrPull" `
    --scope $acrId `
    --output none

Write-Info "Assigning Key Vault Secrets User to container identity..."
az role assignment create `
    --assignee-object-id $identityPrincipal `
    --assignee-principal-type ServicePrincipal `
    --role "Key Vault Secrets User" `
    --scope $kvId `
    --output none

# ---------------------------------------------------------------------------
# GitHub Actions service principal
# ---------------------------------------------------------------------------
Write-Info "Creating GitHub Actions service principal..."
$spJson = az ad sp create-for-rbac `
    --name "message-watchdog-gh" `
    --role Contributor `
    --scopes "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup" `
    --sdk-auth 2>$null

$spObj       = $spJson | ConvertFrom-Json
$spAppId     = $spObj.clientId
$spObjectId  = az ad sp show --id $spAppId --query id -o tsv

Write-Info "Assigning AcrPush to GitHub Actions SP..."
az role assignment create `
    --assignee-object-id $spObjectId `
    --assignee-principal-type ServicePrincipal `
    --role "AcrPush" `
    --scope $acrId `
    --output none

Write-Info "Assigning Key Vault Secrets User to GitHub Actions SP..."
az role assignment create `
    --assignee-object-id $spObjectId `
    --assignee-principal-type ServicePrincipal `
    --role "Key Vault Secrets User" `
    --scope $kvId `
    --output none

# ---------------------------------------------------------------------------
# Upload app secrets to Key Vault
# ---------------------------------------------------------------------------
function Set-KVSecret {
    param(
        [string]$Name,
        [string]$Label,
        [bool]$Required
    )
    $secureVal = Read-Host "  $Label" -AsSecureString
    $val = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureVal)
    )
    if (-not $val) {
        if ($Required) { Write-Fatal "$Label is required." }
        Write-Warn "  Skipping $Name (not set)"
        return
    }
    az keyvault secret set --vault-name $KeyVaultName --name $Name --value $val --output none | Out-Null
    Write-Info "  Stored $Name"
}

Write-Warn "You will now be prompted for each app secret. Press Enter to skip optional ones."
Write-Host ""

Write-Host "--- Telegram credentials (required) ---"
Set-KVSecret "telegram-api-id"   "TELEGRAM_API_ID (from my.telegram.org/apps)"      $true
Set-KVSecret "telegram-api-hash" "TELEGRAM_API_HASH (from my.telegram.org/apps)"     $true
Set-KVSecret "telegram-phone"    "TELEGRAM_PHONE (E.164 format, e.g. +919175551234)" $true

Write-Host ""
Write-Host "--- LLM provider (at least one required — match llm.provider in config.yaml) ---"
Set-KVSecret "anthropic-api-key"     "ANTHROPIC_API_KEY (skip if using Azure OpenAI)"   $false
Set-KVSecret "azure-openai-endpoint" "AZURE_OPENAI_ENDPOINT (skip if using Anthropic)"  $false
Set-KVSecret "azure-openai-api-key"  "AZURE_OPENAI_API_KEY (skip if using Anthropic)"   $false

Write-Host ""
Write-Host "--- Twilio phone call alerts ---"
Set-KVSecret "twilio-account-sid"  "TWILIO_ACCOUNT_SID"                                          $false
Set-KVSecret "twilio-auth-token"   "TWILIO_AUTH_TOKEN"                                            $false
Set-KVSecret "twilio-from-number"  "TWILIO_FROM_NUMBER (your Twilio number, e.g. +12025551234)"  $false
Set-KVSecret "twilio-to-number"    "TWILIO_TO_NUMBER (your mobile, e.g. +19175551234)"            $false

Write-Host ""
Write-Host "--- Gmail alerts (optional) ---"
Set-KVSecret "gmail-app-password"  "GMAIL_APP_PASSWORD (16-char app password from myaccount.google.com/apppasswords)"  $false
Set-KVSecret "gmail-sender"        "GMAIL_SENDER (Gmail address the app password belongs to)"                           $false
Set-KVSecret "gmail-recipient"     "GMAIL_RECIPIENT (address to send alerts to, can be the same)"                       $false

# ---------------------------------------------------------------------------
# Set GitHub repository secrets
# ---------------------------------------------------------------------------
Write-Info "Setting GitHub repository secrets for $GitHubRepo..."

$spJson | gh secret set AZURE_CREDENTIALS --repo $GitHubRepo
gh secret set ACR_REGISTRY            --repo $GitHubRepo --body $acrRegistry
gh secret set RESOURCE_GROUP          --repo $GitHubRepo --body $ResourceGroup
gh secret set KEY_VAULT_URL           --repo $GitHubRepo --body $keyVaultUrl
gh secret set STORAGE_ACCOUNT_NAME    --repo $GitHubRepo --body $StorageAccount
gh secret set FILE_SHARE_NAME         --repo $GitHubRepo --body $FileShare
gh secret set ACI_MANAGED_IDENTITY_ID --repo $GitHubRepo --body $identityId

# ---------------------------------------------------------------------------
# Upload the Telegram session file to the file share
# ---------------------------------------------------------------------------
$sessionFile = "watchdog_session.session"
if (Test-Path $sessionFile) {
    Write-Info "Uploading Telegram session file to Azure File Share..."
    az storage file upload `
        --share-name $FileShare `
        --account-name $StorageAccount `
        --account-key $storageKey `
        --source "./$sessionFile" `
        --path $sessionFile
    Write-Info "Session file uploaded."
} else {
    Write-Warn "Session file '$sessionFile' not found in current directory."
    Write-Warn "Upload it manually after running python setup.py:"
    Write-Warn "  az storage file upload --share-name $FileShare --account-name $StorageAccount --account-key '<key>' --source ./watchdog_session.session --path watchdog_session.session"
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
Write-Host ""
Write-Info "Setup complete."
Write-Host ""
Write-Host "  ACR               : $acrRegistry"
Write-Host "  Key Vault         : $keyVaultUrl"
Write-Host "  Storage account   : $StorageAccount"
Write-Host "  File share        : $FileShare"
Write-Host "  Container identity: $identityId"
Write-Host "  GitHub secrets    : 7 secrets set on $GitHubRepo"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Update config.yaml -> sources.telegram.session_name:"
Write-Host "       session_name: `"/session-store/watchdog_session`""
Write-Host "  2. Push to main to trigger the first deploy:"
Write-Host "       git push origin main"
