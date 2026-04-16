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
#   jq                  (command-line JSON processor)
#
# Usage:
#   SUBSCRIPTION_ID="xxx" GITHUB_REPO="user/Message-Watchdog" ./scripts/azure-setup.sh
#
# All other variables have defaults matching the project conventions but can
# be overridden:
#   ACR_NAME="myacr" KEY_VAULT_NAME="my-kv" ./scripts/azure-setup.sh ...
#
# What this script creates:
#   Resource group
#   Azure Container Registry (ACR)              — stores Docker images
#   Azure Key Vault                             — stores all app secrets
#   Storage account + file share                — persists source session files
#   User-assigned managed identity              — used by the ACI container at runtime
#   RBAC role assignments (least-privilege)
#   GitHub repository secrets
# =============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration — override via environment variables
# ---------------------------------------------------------------------------
: "${SUBSCRIPTION_ID:?Set SUBSCRIPTION_ID. Run: az account show --query id -o tsv}"
: "${GITHUB_REPO:?Set GITHUB_REPO in owner/name format, e.g. jsmith/Message-Watchdog}"

RESOURCE_GROUP="${RESOURCE_GROUP:-Message-Watchdog}"
LOCATION="${LOCATION:-eastus}"
ACR_NAME="${ACR_NAME:-messagewatchdog}"              # globally unique, alphanumeric only
KEY_VAULT_NAME="${KEY_VAULT_NAME:-watchdog-kv}"      # globally unique, 3-24 chars
FILE_SHARE="${FILE_SHARE:-watchdog-session}"
IDENTITY_NAME="${IDENTITY_NAME:-message-watchdog-id}"
ACI_NAME="${ACI_NAME:-message-watchdog}"
# STORAGE_ACCOUNT: auto-generated from subscription ID if not set
# ---------------------------------------------------------------------------

RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; NC='\033[0m'
info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
die()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# Accept full GitHub URL or owner/repo — normalise to owner/repo
if [[ "$GITHUB_REPO" =~ ^https?://github\.com/(.+?)(\.git)?$ ]]; then
    GITHUB_REPO="${BASH_REMATCH[1]}"
fi

# ---------------------------------------------------------------------------
# Validate prerequisites
# ---------------------------------------------------------------------------
command -v az  &>/dev/null || die "Azure CLI not found. Install from https://aka.ms/install-azure-cli"
command -v gh  &>/dev/null || die "GitHub CLI not found. Install from https://cli.github.com"
command -v jq  &>/dev/null || die "jq not found. Install from https://stedolan.github.io/jq"

info "Verifying GitHub CLI authentication..."
gh auth status &>/dev/null || die "GitHub CLI not authenticated. Run: gh auth login"

info "Verifying Azure CLI authentication..."
az account show --query id -o tsv &>/dev/null || \
    die "Azure CLI token expired or not logged in. Run: az logout && az login"

az account set --subscription "$SUBSCRIPTION_ID"
CURRENT_SUB=$(az account show --query id -o tsv)
info "Using subscription: $CURRENT_SUB"

# Auto-generate storage account name from subscription ID if not set
if [[ -z "${STORAGE_ACCOUNT:-}" ]]; then
    STORAGE_ACCOUNT="watchdog$(echo "$SUBSCRIPTION_ID" | tr -d '-' | cut -c1-10)"
    info "Storage account name auto-generated: '$STORAGE_ACCOUNT' (override with STORAGE_ACCOUNT=<name>)"
fi

# ---------------------------------------------------------------------------
# Register required resource providers (idempotent)
# ---------------------------------------------------------------------------
info "Registering required resource providers (this may take ~60s on first run)..."
for ns in \
    "Microsoft.ContainerRegistry" \
    "Microsoft.KeyVault" \
    "Microsoft.ContainerInstance" \
    "Microsoft.ManagedIdentity" \
    "Microsoft.Storage"
do
    state=$(az provider show --namespace "$ns" --query registrationState -o tsv 2>/dev/null || true)
    if [[ "$state" == "Registered" ]]; then
        info "  $ns already registered."
    else
        info "  Registering $ns..."
        az provider register --namespace "$ns" --wait --output none
    fi
done

# ---------------------------------------------------------------------------
# Resource group — detect existing location to avoid InvalidResourceGroupLocation
# ---------------------------------------------------------------------------
existing_location=$(az group show --name "$RESOURCE_GROUP" --query location -o tsv 2>/dev/null || true)
if [[ -n "$existing_location" ]]; then
    LOCATION="$existing_location"
    info "Resource group '$RESOURCE_GROUP' already exists in '$LOCATION' — using that location."
else
    info "Creating resource group '$RESOURCE_GROUP' in $LOCATION..."
    az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none
fi

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

CURRENT_USER_ID=$(az ad signed-in-user show --query id -o tsv)
info "Granting Key Vault Secrets Officer to current user..."
az role assignment create \
  --assignee "$CURRENT_USER_ID" \
  --role "Key Vault Secrets Officer" \
  --scope "$KV_ID" \
  --output none

# ---------------------------------------------------------------------------
# Storage account + file share (source session file persistence)
# ---------------------------------------------------------------------------
storage_exists=$(az storage account show \
  --name "$STORAGE_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" \
  --query id -o tsv 2>/dev/null || true)

if [[ -n "$storage_exists" ]]; then
    info "Storage account '$STORAGE_ACCOUNT' already exists — skipping creation."
else
    info "Creating storage account '$STORAGE_ACCOUNT'..."
    az storage account create \
      --name "$STORAGE_ACCOUNT" \
      --resource-group "$RESOURCE_GROUP" \
      --location "$LOCATION" \
      --sku Standard_LRS \
      --allow-blob-public-access false \
      --output none || \
      die "Failed to create storage account '$STORAGE_ACCOUNT'. If the name is taken, re-run with STORAGE_ACCOUNT=<unique-name>"
fi

STORAGE_KEY=$(az storage account keys list \
  --account-name "$STORAGE_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" \
  --query "[0].value" -o tsv)
[[ -z "$STORAGE_KEY" ]] && die "Could not retrieve key for storage account '$STORAGE_ACCOUNT'."

share_exists=$(az storage share exists \
  --name "$FILE_SHARE" \
  --account-name "$STORAGE_ACCOUNT" \
  --account-key "$STORAGE_KEY" \
  --query exists -o tsv 2>/dev/null || true)

if [[ "$share_exists" == "true" ]]; then
    info "File share '$FILE_SHARE' already exists — skipping creation."
else
    info "Creating file share '$FILE_SHARE'..."
    az storage share create \
      --name "$FILE_SHARE" \
      --account-name "$STORAGE_ACCOUNT" \
      --account-key "$STORAGE_KEY" \
      --output none
fi

info "Storing storage account key in Key Vault..."
az keyvault secret set \
  --vault-name "$KEY_VAULT_NAME" \
  --name "storage-account-key" \
  --value "$STORAGE_KEY" \
  --output none

# ---------------------------------------------------------------------------
# User-assigned managed identity
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

info "Waiting 30s for identity to propagate in AAD..."
sleep 30

info "Assigning AcrPull to container identity..."
az role assignment create \
  --assignee-object-id "$IDENTITY_PRINCIPAL" \
  --assignee-principal-type ServicePrincipal \
  --role "AcrPull" \
  --scope "$ACR_ID" \
  --output none

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

info "Assigning AcrPush to GitHub Actions SP..."
az role assignment create \
  --assignee-object-id "$SP_OBJECT_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "AcrPush" \
  --scope "$ACR_ID" \
  --output none

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
echo "--- LLM provider (at least one required — match llm.provider in config.yaml) ---"
prompt_secret "anthropic-api-key"     "ANTHROPIC_API_KEY (skip if using Azure OpenAI)"   optional
prompt_secret "azure-openai-endpoint" "AZURE_OPENAI_ENDPOINT (skip if using Anthropic)"  optional
prompt_secret "azure-openai-api-key"  "AZURE_OPENAI_API_KEY (skip if using Anthropic)"   optional

echo ""
echo "--- Twilio phone call alerts (optional) ---"
prompt_secret "twilio-account-sid"  "TWILIO_ACCOUNT_SID"                                         optional
prompt_secret "twilio-auth-token"   "TWILIO_AUTH_TOKEN"                                          optional
prompt_secret "twilio-from-number"  "TWILIO_FROM_NUMBER (your Twilio number, e.g. +12025551234)" optional
prompt_secret "twilio-to-number"    "TWILIO_TO_NUMBER (your mobile, e.g. +19175551234)"          optional

echo ""
echo "--- Gmail alerts (optional) ---"
prompt_secret "gmail-app-password"  "GMAIL_APP_PASSWORD (16-char app password from myaccount.google.com/apppasswords)"  optional
prompt_secret "gmail-sender"        "GMAIL_SENDER (Gmail address the app password belongs to)"                           optional
prompt_secret "gmail-recipient"     "GMAIL_RECIPIENT (address to send alerts to, can be the same)"                       optional

# ---------------------------------------------------------------------------
# Set GitHub repository secrets
# ---------------------------------------------------------------------------
info "Setting GitHub repository secrets for $GITHUB_REPO..."

echo "$SP_JSON" | gh secret set AZURE_CREDENTIALS     --repo "$GITHUB_REPO"
gh secret set ACR_REGISTRY            --repo "$GITHUB_REPO" --body "$ACR_REGISTRY"
gh secret set RESOURCE_GROUP          --repo "$GITHUB_REPO" --body "$RESOURCE_GROUP"
gh secret set KEY_VAULT_URL           --repo "$GITHUB_REPO" --body "$KEY_VAULT_URL"
gh secret set STORAGE_ACCOUNT_NAME    --repo "$GITHUB_REPO" --body "$STORAGE_ACCOUNT"
gh secret set FILE_SHARE_NAME         --repo "$GITHUB_REPO" --body "$FILE_SHARE"
gh secret set ACI_MANAGED_IDENTITY_ID --repo "$GITHUB_REPO" --body "$IDENTITY_ID"

# ---------------------------------------------------------------------------
# Upload source session file (if it already exists locally)
# ---------------------------------------------------------------------------
SESSION_FILE="watchdog_session.session"
if [[ -f "$SESSION_FILE" ]]; then
    info "Uploading existing session file to Azure File Share..."
    az storage file upload \
      --share-name "$FILE_SHARE" \
      --account-name "$STORAGE_ACCOUNT" \
      --account-key "$STORAGE_KEY" \
      --source "./$SESSION_FILE" \
      --path "$SESSION_FILE"
    info "Session file uploaded."
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
echo "  ACR               : $ACR_REGISTRY"
echo "  Key Vault         : $KEY_VAULT_URL"
echo "  Storage account   : $STORAGE_ACCOUNT"
echo "  File share        : $FILE_SHARE"
echo "  Container identity: $IDENTITY_ID"
echo "  GitHub secrets    : 7 secrets set on $GITHUB_REPO"
echo ""
echo "Next steps:"
echo "  1. If you haven't run setup.py yet:"
echo "       python setup.py"
echo "     Then upload the generated .session file (command above)."
echo "  2. Update config.yaml -> sources.telegram.session_name:"
echo "       session_name: \"/session-store/watchdog_session\""
echo "  3. Push to main to trigger the first deploy:"
echo "       git push origin main"
