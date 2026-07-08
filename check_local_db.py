import psycopg2

DB_URL = "postgresql://postgres:postgres@localhost:5433/quanta"

def check_local_db():
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    cur.execute("SELECT id, clerk_user_id FROM user_profiles;")
    users = cur.fetchall()
    print("Users in local DB:")
    for row in users:
        print(row)
        
    cur.execute("""
        SELECT table_name 
        FROM information_schema.tables 
        WHERE table_schema = 'public'
    """)
    print("\nTables:")
    for row in cur.fetchall():
        print(row)
        
    cur.close()
    conn.close()

if __name__ == "__main__":
    check_local_db()
