# Quanta Infrastructure

This directory contains Terraform configuration for deploying the Quanta Automation Platform to Azure Container Apps.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Azure Container Apps                      │
│  ┌─────────────────┐         ┌─────────────────────────────┐│
│  │  Control Plane  │         │     Execution Plane         ││
│  │   (Go :8080)    │◄───────►│     (Python Worker)         ││
│  │   [External]    │         │     [Internal Only]         ││
│  └────────┬────────┘         └──────────────┬──────────────┘│
│           │                                  │               │
│           └──────────────┬───────────────────┘               │
│                          │                                   │
│           ┌──────────────▼──────────────┐                    │
│           │   Container App Environment │                    │
│           │   (Shared networking)       │                    │
│           └─────────────────────────────┘                    │
└─────────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
        ▼                     ▼                     ▼
  ┌──────────┐         ┌──────────┐          ┌──────────┐
  │ Supabase │         │  Upstash │          │ Temporal │
  │(Postgres)│         │ (Redis)  │          │ (Cloud)  │
  └──────────┘         └──────────┘          └──────────┘
```

## Prerequisites

1. **Azure CLI** installed and logged in:

   ```bash
   az login
   az account set --subscription <YOUR_SUBSCRIPTION_ID>
   ```

2. **Terraform** v1.5+ installed:

   ```bash
   brew install terraform  # macOS
   ```

3. **GitHub Repository Secrets** configured (see below)

## Quick Start

### 1. Initialize Terraform

```bash
cd backend/infra
terraform init
```

### 2. Configure Variables

```bash
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your subscription ID
```

### 3. Plan Changes

```bash
terraform plan
```

### 4. Deploy Infrastructure

```bash
terraform apply
```

### 5. Get Outputs for GitHub Secrets

```bash
terraform output -json
```

## GitHub Repository Secrets

Configure these secrets in your GitHub repository (Settings → Secrets → Actions):

### Azure Authentication

| Secret              | Description            | How to Get                                           |
| ------------------- | ---------------------- | ---------------------------------------------------- |
| `AZURE_CREDENTIALS` | Service Principal JSON | See [Creating SP](#creating-azure-service-principal) |

### Infrastructure

| Secret               | Description                | Example                               |
| -------------------- | -------------------------- | ------------------------------------- |
| `PROD_DATABASE_URL`  | Supabase connection string | `postgresql://user:pass@host:5432/db` |
| `PROD_NATS_URL`      | NATS server URL            | `nats://user:pass@host:4222`          |
| `PROD_REDIS_URL`     | Redis connection URL       | `redis://user:pass@host:6379`         |
| `PROD_TEMPORAL_HOST` | Temporal server            | `namespace.tmprl.cloud:7233`          |

### Authentication (Clerk)

| Secret                  | Description          |
| ----------------------- | -------------------- |
| `CLERK_PUBLISHABLE_KEY` | Clerk public API key |
| `CLERK_SECRET_KEY`      | Clerk secret API key |

### Payments (Polar)

| Secret                 | Description                  |
| ---------------------- | ---------------------------- |
| `POLAR_ACCESS_TOKEN`   | Polar API token              |
| `POLAR_WEBHOOK_SECRET` | Polar webhook signing secret |

### AI & Storage

| Secret                 | Description               |
| ---------------------- | ------------------------- |
| `OPENAI_API_KEY`       | OpenAI API key            |
| `R2_ACCESS_KEY_ID`     | Cloudflare R2 access key  |
| `R2_SECRET_ACCESS_KEY` | Cloudflare R2 secret key  |
| `R2_ENDPOINT_URL`      | Cloudflare R2 endpoint    |
| `R2_BUCKET_NAME`       | Cloudflare R2 bucket name |
| `FERNET_KEY`           | Session encryption key    |

## Creating Azure Service Principal

```bash
# Create Service Principal with Contributor role
az ad sp create-for-rbac \
  --name "quanta-github-actions" \
  --role contributor \
  --scopes /subscriptions/<SUBSCRIPTION_ID> \
  --sdk-auth

# Copy the JSON output to GitHub secret: AZURE_CREDENTIALS
```

The output looks like:

```json
{
  "clientId": "...",
  "clientSecret": "...",
  "subscriptionId": "...",
  "tenantId": "...",
  ...
}
```

## Generating Fernet Key

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Resource Naming

| Resource              | Name Pattern                | Example                 |
| --------------------- | --------------------------- | ----------------------- |
| Resource Group        | `{project}-{env}-rg`        | `quanta-prod-rg`        |
| Container Registry    | `{project}{env}acr{random}` | `quantaprodacrabcd1234` |
| Container Environment | `{project}-{env}-env`       | `quanta-prod-env`       |
| Control Plane App     | `control-plane`             | `control-plane`         |
| Execution Plane App   | `execution-plane`           | `execution-plane`       |

## Costs (Estimated)

| Resource                   | SKU            | ~Monthly Cost  |
| -------------------------- | -------------- | -------------- |
| Container Registry         | Basic          | $5             |
| Container Apps (Control)   | 0.5 vCPU, 1 GB | ~$15           |
| Container Apps (Execution) | 1 vCPU, 2 GB   | ~$30           |
| Log Analytics              | Pay-as-you-go  | ~$5            |
| **Total**                  |                | **~$55/month** |

_Costs vary based on usage. Container Apps scale to zero when idle._

## Troubleshooting

### View Container Logs

```bash
az containerapp logs show \
  --name control-plane \
  --resource-group quanta-prod-rg \
  --follow
```

### Check Container Status

```bash
az containerapp show \
  --name control-plane \
  --resource-group quanta-prod-rg \
  --query "properties.runningStatus"
```

### Force Container Restart

```bash
az containerapp revision restart \
  --name control-plane \
  --resource-group quanta-prod-rg \
  --revision <REVISION_NAME>
```

## Destroying Infrastructure

⚠️ **Warning**: This will delete all resources!

```bash
terraform destroy
```
