#!/usr/bin/env bash
# Provisions everything the site needs on Azure and wires it together:
#   - a resource group
#   - a storage account + "videos" blob container (video files)
#   - a Linux App Service plan + Python web app (the Flask site)
#
# Prerequisites: Azure CLI (az) installed and logged in (az login).
# Usage:
#   ./deploy/azure-setup.sh <app-name> [region]
# <app-name> must be globally unique (it becomes <app-name>.azurewebsites.net).
set -euo pipefail

APP_NAME="${1:?Usage: $0 <app-name> [region]}"
LOCATION="${2:-eastus}"
RESOURCE_GROUP="${APP_NAME}-rg"
PLAN_NAME="${APP_NAME}-plan"
# Storage account names: 3-24 chars, lowercase letters + digits only.
STORAGE_ACCOUNT="$(echo "${APP_NAME}store" | tr -cd 'a-z0-9' | cut -c1-24)"

echo "==> Creating resource group ${RESOURCE_GROUP} in ${LOCATION}"
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

echo "==> Creating storage account ${STORAGE_ACCOUNT}"
az storage account create \
  --name "$STORAGE_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --sku Standard_LRS \
  --kind StorageV2 \
  --output none

CONNECTION_STRING=$(az storage account show-connection-string \
  --name "$STORAGE_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" \
  --query connectionString --output tsv)

echo "==> Creating blob container 'videos'"
az storage container create \
  --name videos \
  --connection-string "$CONNECTION_STRING" \
  --output none

echo "==> Creating App Service plan ${PLAN_NAME} (Linux, B1)"
az appservice plan create \
  --name "$PLAN_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --is-linux \
  --sku B1 \
  --output none

echo "==> Creating web app ${APP_NAME} (Python 3.12)"
az webapp create \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --plan "$PLAN_NAME" \
  --runtime "PYTHON:3.12" \
  --output none

echo "==> Configuring app settings"
az webapp config appsettings set \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --settings \
    AZURE_STORAGE_CONNECTION_STRING="$CONNECTION_STRING" \
    SECRET_KEY="$(openssl rand -hex 32)" \
    SCM_DO_BUILD_DURING_DEPLOYMENT=true \
  --output none

echo "==> Setting startup command (gunicorn)"
az webapp config set \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --startup-file "gunicorn --bind=0.0.0.0:8000 --timeout 600 --workers 2 app:app" \
  --output none

echo
echo "Done. Deploy the code with either:"
echo "  1. az webapp up --name $APP_NAME --resource-group $RESOURCE_GROUP   (from the repo root)"
echo "  2. The GitHub Actions workflow in .github/workflows/azure-deploy.yml"
echo "     (download the publish profile and save it as the AZURE_WEBAPP_PUBLISH_PROFILE secret):"
echo "     az webapp deployment list-publishing-profiles --name $APP_NAME --resource-group $RESOURCE_GROUP --xml"
echo
echo "Site URL: https://${APP_NAME}.azurewebsites.net"
