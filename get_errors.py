import psycopg2

DB_URL = "postgresql://postgres.wnhwdwognyvzyrpqtxyy:Itsaliyan2580@aws-1-ap-south-1.pooler.supabase.com:6543/postgres"

def get_errors():
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    cur.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements;")
    conn.commit()
    
    cur.execute("""
        SELECT query, calls, total_exec_time, rows 
        FROM pg_stat_statements 
        WHERE query ILIKE '%INSERT INTO%workflows%'
           OR query ILIKE '%INSERT INTO%jobs%'
        ORDER BY total_exec_time DESC LIMIT 10;
    """)
    for row in cur.fetchall():
        print(row)
    cur.close()
    conn.close()

if __name__ == "__main__":
    get_errors()
