# 🚀 Production Deployment Checklist

Complete this checklist before going live with Quanta.

---

## Pre-Deployment

### 1. External Services Setup

- [ ] **Supabase** - PostgreSQL database created

  - [ ] Connection string saved (Transaction Mode, port 6543)
  - [ ] RLS policies configured

- [ ] **Temporal Cloud** - Namespace created

  - [ ] Namespace ID noted
  - [ ] TLS certificates generated (if required)

- [ ] **NATS** - Message broker provisioned

  - [ ] Synadia Cloud account or self-hosted
  - [ ] Connection URL with auth

- [ ] **Redis/Upstash** - Cache provisioned

  - [ ] TLS-enabled connection string

- [ ] **Clerk** - Authentication configured

  - [ ] Publishable key and secret key
  - [ ] Webhook endpoint configured for sync

- [ ] **Polar** - Payments configured

  - [ ] Access token generated
  - [ ] Webhook secret for events

- [ ] **Cloudflare R2** - Blob storage

  - [ ] Bucket created
  - [ ] Access keys generated
  - [ ] CORS configured for frontend domain

- [ ] **OpenAI** - API key generated

### 2. Security Keys Generated

```bash
# Generate Fernet key
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Generate Webhook secret
openssl rand -hex 32
```

- [ ] `FERNET_KEY` generated and saved
- [ ] `WEBHOOK_SECRET` generated and saved

---

## Azure Setup

### 3. Azure Infrastructure

```bash
cd backend/infra

# Initialize Terraform
terraform init

# Copy and configure variables
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your subscription ID

# Plan and review changes
terraform plan

# Apply infrastructure
terraform apply

# Save outputs
terraform output -json > outputs.json
```

- [ ] Terraform applied successfully
- [ ] ACR login server URL noted
- [ ] ACR credentials saved

### 4. Azure Service Principal

```bash
az ad sp create-for-rbac \
  --name "quanta-github-actions" \
  --role contributor \
  --scopes /subscriptions/<SUBSCRIPTION_ID> \
  --sdk-auth > azure-credentials.json
```

- [ ] Service principal created
- [ ] JSON output saved for GitHub

---

## GitHub Setup

### 5. GitHub Repository Secrets

Go to: Repository → Settings → Secrets → Actions

**Azure:**

- [ ] `AZURE_CREDENTIALS` (Service Principal JSON)

**Database:**

- [ ] `PROD_DATABASE_URL`
- [ ] `PROD_NATS_URL`
- [ ] `PROD_REDIS_URL`
- [ ] `PROD_TEMPORAL_HOST`

**Authentication:**

- [ ] `CLERK_PUBLISHABLE_KEY`
- [ ] `CLERK_SECRET_KEY`

**Payments:**

- [ ] `POLAR_ACCESS_TOKEN`
- [ ] `POLAR_WEBHOOK_SECRET`

**AI:**

- [ ] `OPENAI_API_KEY`

**Storage:**

- [ ] `R2_ACCESS_KEY_ID`
- [ ] `R2_SECRET_ACCESS_KEY`
- [ ] `R2_ENDPOINT_URL`
- [ ] `R2_BUCKET_NAME`

**Security:**

- [ ] `FERNET_KEY`

---

## Deployment

### 6. First Deployment

```bash
# Push to main to trigger deployment
git push origin main
```

- [ ] GitHub Actions workflow triggered
- [ ] Docker images built successfully
- [ ] Images pushed to ACR
- [ ] Control Plane deployed
- [ ] Execution Plane deployed

### 7. Verify Deployment

```bash
# Get Control Plane URL
az containerapp show \
  --name control-plane \
  --resource-group quanta-prod-rg \
  --query "properties.configuration.ingress.fqdn" -o tsv

# Test health endpoint
curl https://<URL>/health
```

- [ ] Health check returns 200
- [ ] All services healthy (database, redis, nats)

---

## Post-Deployment

### 8. DNS & Frontend

- [ ] Custom domain configured (optional)
- [ ] Frontend `NEXT_PUBLIC_API_URL` updated
- [ ] Frontend redeployed

### 9. Monitoring

- [ ] Azure Log Analytics reviewed
- [ ] Prometheus metrics accessible at `/metrics`
- [ ] Alerts configured (optional)

### 10. Final Verification

- [ ] Create test workflow via frontend
- [ ] Run test job via API
- [ ] Verify WebSocket updates work
- [ ] Verify job completion and results

---

## Rollback Procedure

If deployment fails:

```bash
# List revisions
az containerapp revision list \
  --name control-plane \
  --resource-group quanta-prod-rg

# Activate previous revision
az containerapp revision activate \
  --name control-plane \
  --resource-group quanta-prod-rg \
  --revision <PREVIOUS_REVISION>

# Set traffic to previous revision
az containerapp ingress traffic set \
  --name control-plane \
  --resource-group quanta-prod-rg \
  --revision-weight <PREVIOUS_REVISION>=100
```

---

## Support

- **Logs**: `az containerapp logs show --name <APP> --resource-group quanta-prod-rg --follow`
- **Metrics**: `https://<CONTROL_PLANE_URL>/metrics`
- **Health**: `https://<CONTROL_PLANE_URL>/health`

---

_Last updated: January 1, 2026_
