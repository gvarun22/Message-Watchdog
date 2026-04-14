#!/usr/bin/env bash
# =============================================================================
# Message Watchdog — One-time Azure infrastructure setup
# =============================================================================
# Run this ONCE from your local machine before the first deployment.
# Safe to re-run — all Azure create operations are idempotent.
#
# Prerequisites:
#   az login            (Azure CLI, logged in to the correct subscription)
#   gh auth login       (GitHub CLI, authenticated to your account)
#
# What this script creates:
#   Resource group
#   Azure Container Registry (ACR)              — stores Docker images
#   Azure Key Vault                             — stores all app secrets
#   Storage account + file share                — persists Telegram .session file
#   User-assigned managed identity              — used by the ACI container at runtime
#   RBAC role assignments (least-privilege):
#     Container identity   AcrPull on ACR
#     Container identity   Key Vault Secrets User on Key Vault
#     GitHub Actions SP    AcrPush on ACR
#     GitHub Actions SP    Contributor on resource group (for ACI create/delete)
#     GitHub Actions SP    Key Vault Secrets User on Key Vault (reads storage key during deploy)
#   GitHub repository secrets
#
# Permissions model summary:
#   The ACI container receives ONLY AZURE_KEY_VAULT_URL as an env var.
#   Everything else (Telegram, Twilio, Anthropic, Gmail credentials) is read
#   from Key Vault at runtime via managed identity — no credentials in GitHub.
# =============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# EDIT THESE before running
# ---------------------------------------------------------------------------
SUBSCRIPTION_ID=""          # fill in: az account show --query id -o tsv
RESOURCE_GROUP="Message-Watchdog"
LOCATION="eastus"
ACR_NAME="messagewatchdog"          # globally unique, 5-50 chars, alphanumeric only
KEY_VAULT_NAME="watchdog-kv"        # globally unique, 3-24 chars
STORAGE_ACCOUNT="watchdogstorage"   # globally unique, 3-24 chars, lowercase alphanumeric
FILE_SHARE="watchdog-session"
IDENTITY_NAME="message-watchdog-id"
ACI_NAME="message-watchdog"
GITHUB_REPO=""              # fill in: e.g. "jsmith/Message-Watchdog"
# ---------------------------------------------------------------------------

RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; NC='\033[0m'
info()    { echo -e "${GREEN}[+]${NC} $*"; }
warn()    { echo -e "${YELLOW}[!]${NC} $*"; }
die()     { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ---------------------------------------------------------------------------
# Validate prerequisites and configuration
# ---------------------------------------------------------------------------
[[ -z "$SUBSCRIPTION_ID" ]] && die "Set SUBSCRIPTION_ID at the top of this script."
[[ -z "$GITHUB_REPO" ]]     && die "Set GITHUB_REPO at the top of this script."

command -v az  &>/dev/null || die "Azure CLI not found. Install from https://aka.ms/install-azure-cli"
command -v gh  &>/dev/null || die "GitHub CLI not found. Install from https://cli.github.com"
command -v jq  &>/dev/null || die "jq not found. Install from https://stedolan.github.io/jq"

az account set --subscription "$SUBSCRIPTION_ID"
CURRENT_SUB=$(az account show --query id -o tsv)
info "Using subscription: $CURRENT_SUB"

# ---------------------------------------------------------------------------
# Resource group
# ---------------------------------------------------------------------------
info "Creating resource group '$RESOURCE_GROUP' in $LOCATION..."
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

# ---------------------------------------------------------------------------
# Azure Container Registry
# ---------------------------------------------------------------------------
info "Creating ACR '$ACR_NAME'..."
az acr create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$ACR_NAME" \
  --sku Basic \
  --admin-enabled false \
  --output none

ACR_ID=$(az acr show --name "$ACR_NAME" --query id -o tsv)
ACR_REGISTRY="${ACR_NAME}.azurecr.io"
info "ACR: $ACR_REGISTRY"

# ---------------------------------------------------------------------------
# Azure Key Vault
# ---------------------------------------------------------------------------
info "Creating Key Vault '$KEY_VAULT_NAME'..."
az keyvault create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$KEY_VAULT_NAME" \
  --location "$LOCATION" \
  --enable-rbac-authorization true \
  --output none

KV_ID=$(az keyvault show --name "$KEY_VAULT_NAME" --query id -o tsv)
KEY_VAULT_URL="https://${KEY_VAULT_NAME}.vault.azure.net/"
info "Key Vault: $KEY_VAULT_URL"

# Give the current user Secrets Officer so we can upload secrets below
CURRENT_USER_ID=$(az ad signed-in-user show --query id -o tsv)
info "Granting Key Vault Secrets Officer to current user..."
az role assignment create \
  --assignee "$CURRENT_USER_ID" \
  --role "Key Vault Secrets Officer" \
  --scope "$KV_ID" \
  --output none

# ---------------------------------------------------------------------------
# Storage account + file share (for Telegram .session file persistence)
# ---------------------------------------------------------------------------
info "Creating storage account '$STORAGE_ACCOUNT'..."
az storage account create \
  --name "$STORAGE_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --sku Standard_LRS \
  --allow-blob-public-access false \
  --output none

info "Creating file share '$FILE_SHARE'..."
az storage share create \
  --name "$FILE_SHARE" \
  --account-name "$STORAGE_ACCOUNT" \
  --output none

STORAGE_ACCOUNT_ID=$(az storage account show \
  --name "$STORAGE_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" \
  --query id -o tsv)

# Store the storage key in Key Vault — the deploy workflow reads it from here
info "Storing storage account key in Key Vault..."
STORAGE_KEY=$(az storage account keys list \
  --account-name "$STORAGE_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" \
  --query "[0].value" -o tsv)
az keyvault secret set \
  --vault-name "$KEY_VAULT_NAME" \
  --name "storage-account-key" \
  --value "$STORAGE_KEY" \
  --output none

# ---------------------------------------------------------------------------
# User-assigned managed identity (used by ACI container at runtime)
# ---------------------------------------------------------------------------
info "Creating managed identity '$IDENTITY_NAME'..."
az identity create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$IDENTITY_NAME" \
  --output none

IDENTITY_ID=$(az identity show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$IDENTITY_NAME" \
  --query id -o tsv)
IDENTITY_PRINCIPAL=$(az identity show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$IDENTITY_NAME" \
  --query principalId -o tsv)

# Wait for the service principal to propagate (AAD replication delay)
info "Waiting 30 s for identity to propagate..."
sleep 30

# AcrPull — container pulls its own image
info "Assigning AcrPull to container identity..."
az role assignment create \
  --assignee-object-id "$IDENTITY_PRINCIPAL" \
  --assignee-principal-type ServicePrincipal \
  --role "AcrPull" \
  --scope "$ACR_ID" \
  --output none

# Key Vault Secrets User — container reads app secrets at runtime
info "Assigning Key Vault Secrets User to container identity..."
az role assignment create \
  --assignee-object-id "$IDENTITY_PRINCIPAL" \
  --assignee-principal-type ServicePrincipal \
  --role "Key Vault Secrets User" \
  --scope "$KV_ID" \
  --output none

# ---------------------------------------------------------------------------
# GitHub Actions service principal
# ---------------------------------------------------------------------------
info "Creating GitHub Actions service principal..."
SP_JSON=$(az ad sp create-for-rbac \
  --name "message-watchdog-gh" \
  --role Contributor \
  --scopes "/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RESOURCE_GROUP}" \
  --sdk-auth \
  2>/dev/null)

SP_APP_ID=$(echo "$SP_JSON" | jq -r '.clientId')
SP_OBJECT_ID=$(az ad sp show --id "$SP_APP_ID" --query id -o tsv)

# AcrPush — workflow pushes new images
info "Assigning AcrPush to GitHub Actions SP..."
az role assignment create \
  --assignee-object-id "$SP_OBJECT_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "AcrPush" \
  --scope "$ACR_ID" \
  --output none

# Key Vault Secrets User — workflow reads storage key from KV during deploy
info "Assigning Key Vault Secrets User to GitHub Actions SP..."
az role assignment create \
  --assignee-object-id "$SP_OBJECT_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Key Vault Secrets User" \
  --scope "$KV_ID" \
  --output none

# ---------------------------------------------------------------------------
# Upload app secrets to Key Vault
# ---------------------------------------------------------------------------
warn "You will now be prompted for each app secret. Press Enter to skip optional ones."
echo ""

prompt_secret() {
  local name="$1" label="$2" required="$3"
  read -r -s -p "  $label: " val
  echo ""
  if [[ -z "$val" ]]; then
    if [[ "$required" == "required" ]]; then
      die "$label is required."
    fi
    warn "  Skipping $name (not set)"
    return
  fi
  az keyvault secret set \
    --vault-name "$KEY_VAULT_NAME" \
    --name "$name" \
    --value "$val" \
    --output none
  info "  Stored $name"
}

echo "--- Telegram credentials (required) ---"
prompt_secret "telegram-api-id"   "TELEGRAM_API_ID (from my.telegram.org/apps)"      required
prompt_secret "telegram-api-hash" "TELEGRAM_API_HASH (from my.telegram.org/apps)"     required
prompt_secret "telegram-phone"    "TELEGRAM_PHONE (E.164 format, e.g. +19175551234)"  required

echo ""
echo "--- LLM provider (at least one required) ---"
prompt_secret "anthropic-api-key"     "ANTHROPIC_API_KEY (skip if using Azure OpenAI)"  optional
prompt_secret "azure-openai-endpoint" "AZURE_OPENAI_ENDPOINT (skip if using Anthropic)" optional
prompt_secret "azure-openai-api-key"  "AZURE_OPENAI_API_KEY (skip if using Anthropic)"  optional

echo ""
echo "--- Twilio phone call alerts (required for phone_call channel) ---"
prompt_secret "twilio-account-sid"  "TWILIO_ACCOUNT_SID"  optional
prompt_secret "twilio-auth-token"   "TWILIO_AUTH_TOKEN"   optional
prompt_secret "twilio-from-number"  "TWILIO_FROM_NUMBER"  optional
prompt_secret "twilio-to-number"    "TWILIO_TO_NUMBER"    optional

echo ""
echo "--- Gmail alerts (required for email channel) ---"
prompt_secret "gmail-app-password"  "GMAIL_APP_PASSWORD (16-char app password)"  optional

# ---------------------------------------------------------------------------
# Set GitHub repository secrets
# ---------------------------------------------------------------------------
info "Setting GitHub repository secrets for $GITHUB_REPO..."

gh secret set AZURE_CREDENTIALS      --repo "$GITHUB_REPO" --body "$SP_JSON"
gh secret set ACR_REGISTRY           --repo "$GITHUB_REPO" --body "$ACR_REGISTRY"
gh secret set RESOURCE_GROUP         --repo "$GITHUB_REPO" --body "$RESOURCE_GROUP"
gh secret set KEY_VAULT_URL          --repo "$GITHUB_REPO" --body "$KEY_VAULT_URL"
gh secret set STORAGE_ACCOUNT_NAME   --repo "$GITHUB_REPO" --body "$STORAGE_ACCOUNT"
gh secret set FILE_SHARE_NAME        --repo "$GITHUB_REPO" --body "$FILE_SHARE"
gh secret set ACI_MANAGED_IDENTITY_ID --repo "$GITHUB_REPO" --body "$IDENTITY_ID"

# ---------------------------------------------------------------------------
# Upload Telegram session file (if it already exists locally)
# ---------------------------------------------------------------------------
SESSION_FILE="watchdog_session.session"
if [[ -f "$SESSION_FILE" ]]; then
  info "Uploading existing Telegram session file to Azure File Share..."
  az storage file upload \
    --share-name "$FILE_SHARE" \
    --account-name "$STORAGE_ACCOUNT" \
    --account-key "$STORAGE_KEY" \
    --source "./$SESSION_FILE" \
    --path "$SESSION_FILE"
else
  warn "No local session file found. Run 'python setup.py' first, then upload:"
  warn "  az storage file upload --share-name $FILE_SHARE \\"
  warn "    --account-name $STORAGE_ACCOUNT --account-key '<key>' \\"
  warn "    --source ./watchdog_session.session --path watchdog_session.session"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
info "Setup complete."
echo ""
echo "  ACR              : $ACR_REGISTRY"
echo "  Key Vault        : $KEY_VAULT_URL"
echo "  Storage account  : $STORAGE_ACCOUNT"
echo "  File share       : $FILE_SHARE"
echo "  Container identity: $IDENTITY_ID"
echo "  GitHub secrets   : 7 secrets set on $GITHUB_REPO"
echo ""
echo "Next steps:"
echo "  1. If you haven't run setup.py yet:"
echo "       python setup.py"
echo "     Then upload the generated .session file (command above)."
echo "  2. Update config.yaml → sources.telegram.session_name:"
echo "       session_name: \"/session-store/watchdog_session\""
echo "  3. Push to main to trigger the first deploy:"
echo "       git push origin main"
