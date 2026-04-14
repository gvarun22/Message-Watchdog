# =============================================================================
# Message Watchdog — Dockerfile
# Intended for Azure Container Instances (ACI)
# =============================================================================
#
# Build:
#   docker build -t message-watchdog .
#
# Run locally (with .env file):
#   docker run --env-file .env -v $(pwd)/watchdog_session.session:/app/watchdog_session.session message-watchdog
#
# Azure deployment notes:
#   - Store .env values as ACI secure environment variables (--secure-environment-variables)
#   - Mount an Azure File Share at /app/ to persist the .session file across restarts:
#       az container create ... \
#         --azure-file-volume-account-name <storage_account> \
#         --azure-file-volume-account-key  <storage_key> \
#         --azure-file-volume-share-name   <share_name> \
#         --azure-file-volume-mount-path   /app/session_store
#     Then set sources.telegram.session_name in config.yaml to:
#       session_name: "session_store/watchdog_session"
# =============================================================================

FROM python:3.11-slim

# Keeps Python from buffering stdout (important for live log streaming in ACI)
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (cached layer — only rebuilds when requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# The .session file must be mounted from persistent storage (Azure File Share).
# The container will run setup interactively the first time if no session exists,
# but for automated deployment, run setup locally first and upload the .session file.

CMD ["python", "main.py"]
