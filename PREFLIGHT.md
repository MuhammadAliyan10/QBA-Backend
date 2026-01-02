# 🚀 Production Pre-Flight Checklist

**Status**: REQUIRED before `git push`
**Date**: January 1, 2026

---

## 1. 💰 Cost Safety (Manual Steps)

### Google Cloud / Gemini (Cost Budget)

1.  Go to [Google Cloud Console > Billing](https://console.cloud.google.com/billing).
2.  Select your Billing Account linked to the Gemini API project.
3.  Go to **Budgets & alerts**.
4.  Click **Create Budget**.
5.  Name: `GeminiSafetyBudget`.
6.  Amount: **$5.00** (or your safe amount).
7.  Actions: Select "Email alerts to billing admins and users".
8.  ✅ Verify: Budget is active.

### Azure (Infrastructure Budget)

1.  Go to [Azure Portal > Cost Management + Billing](https://portal.azure.com/#view/Microsoft_Azure_CostManagement/Menu/~/budgets).
2.  Select your Subscription.
3.  Click **Budgets** > **Add**.
4.  Name: `InfraSafetyBudget`.
5.  Amount: **$5.00** (Reset: Monthly).
6.  Alert Condition: **Actual > 80% ($4.00)**.
7.  Action Group: Add your email.
8.  ✅ Verify: Budget is active.

---

## 2. 🗄️ Database Hydration (Supabase)

**Critical**: Prisma cannot use the Transaction Pooler (Port 6543) for migrations. You must use the Session Pooler (Port 5432).

### Command to Hydrate Database

Run this from the `backend` root directory:

```bash
# Override DATABASE_URL to use Port 5432 (Session Mode)
DATABASE_URL="postgresql://postgres.wnhwdwognyvzyrpqtxyy:Itsaliyan2580@aws-1-ap-south-1.pooler.supabase.com:5432/postgres" \
prisma db push --schema=apps/execution-plane/prisma/schema.prisma
```

**Verification**:

1.  Go to [Supabase Dashboard > Table Editor](https://supabase.com/dashboard/project/_/editor).
2.  ✅ Verify tables (`Job`, `Step`, etc.) are created.

---

## 3. 🔐 Security & Sanity Scan

Run the automated security scanner to check for leaked secrets in git history:

```bash
./scripts/security_scan.sh
```

**Success Criteria**:

- Output must say: `✅ No secrets found in git history.`
- Output must say: `✅ SCAN PASSED: Ready for Deployment`

---

## 4. 🌐 Connection Pooling (Supabase)

1.  Go to [Supabase Dashboard > Database > Settings > Connection Pooling](https://supabase.com/dashboard/project/_/settings/database).
2.  **Pool Mode**: Transaction.
3.  **Pool Size**: 15 (Leave room for direct connections).
4.  **Port**: 6543.
5.  ✅ Verify: `pgbouncer=true` is in your production `.env` `DATABASE_URL`.

---

## 5. 🚀 Final Launch

1.  Commit changes: `git commit -m "chore: production readiness"`
2.  Push to main: `git push origin main`
3.  Monitor GitHub Actions: [Actions Tab](https://github.com/MuhammadAliyan10/Quanta/actions)
