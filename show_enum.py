import psycopg2

DB_URL = "postgresql://postgres.wnhwdwognyvzyrpqtxyy:Itsaliyan2580@aws-1-ap-south-1.pooler.supabase.com:6543/postgres"

def show_schema():
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    cur.execute("""
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_name = 'jobs';
    """)
    for row in cur.fetchall():
        print(row)
    cur.close()
    conn.close()

if __name__ == "__main__":
    show_schema()
