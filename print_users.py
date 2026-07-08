import psycopg2

DB_URL = "postgresql://postgres.wnhwdwognyvzyrpqtxyy:Itsaliyan2580@aws-1-ap-south-1.pooler.supabase.com:6543/postgres"

def show_db():
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    cur.execute("SELECT id, clerk_user_id FROM user_profiles LIMIT 10;")
    users = cur.fetchall()
    for u in users:
        print(f"ID: {u[0]}, Clerk: {u[1]}")
    cur.close()
    conn.close()

if __name__ == "__main__":
    show_db()
