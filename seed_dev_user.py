#!/usr/bin/env python3
# backend/seed_dev_user.py
# Seeds the dev user into user_profiles so the Go control plane can resolve tenant context.
import psycopg2

DSN = "postgresql://postgres.wnhwdwognyvzyrpqtxyy:Itsaliyan2580@aws-1-ap-south-1.pooler.supabase.com:6543/postgres"
CLERK_ID = "user_39NStlJpISwJs7M8Uo1hhp1sqqT"

conn = psycopg2.connect(DSN)
conn.autocommit = True
cur = conn.cursor()

cur.execute("""
    INSERT INTO user_profiles (id, clerk_user_id, email, tier, created_at, updated_at)
    VALUES (gen_random_uuid(), %s, 'dev@quanta.local', 'FREE', now(), now())
    ON CONFLICT (clerk_user_id) DO NOTHING
""", (CLERK_ID,))

cur.execute("SELECT id, clerk_user_id, email FROM user_profiles WHERE clerk_user_id = %s", (CLERK_ID,))
row = cur.fetchone()
print(f"Dev user ready: id={row[0]}, clerk_id={row[1]}, email={row[2]}")

cur.close()
conn.close()
