import psycopg2

DB_URL = "postgresql://postgres:postgres@localhost:5433/quanta"

def fix_db():
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    try:
        cur.execute("ALTER TABLE workflows ALTER COLUMN updated_at DROP NOT NULL;")
        cur.execute("ALTER TABLE workflows ALTER COLUMN created_at DROP NOT NULL;")
        
        cur.execute("ALTER TABLE jobs ALTER COLUMN updated_at DROP NOT NULL;")
        cur.execute("ALTER TABLE jobs ALTER COLUMN created_at DROP NOT NULL;")
        
        conn.commit()
        print("Altered tables successfully")
    except Exception as e:
        print("Failed to alter tables:", e)
        
    cur.close()
    conn.close()

if __name__ == "__main__":
    fix_db()
