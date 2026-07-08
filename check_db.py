import psycopg2

DB_URL = "postgresql://postgres.wnhwdwognyvzyrpqtxyy:Itsaliyan2580@aws-1-ap-south-1.pooler.supabase.com:6543/postgres"

def fix_db():
    try:
        conn = psycopg2.connect(DB_URL)
        cur = conn.cursor()
        
        # Check what users exist
        cur.execute("SELECT id, email FROM user_profiles LIMIT 5;")
        users = cur.fetchall()
        print("Existing users:", users)
        
        # Insert the 0000 user if it doesn't exist
        uid = '00000000-0000-0000-0000-000000000000'
        cur.execute("SELECT id FROM user_profiles WHERE id = %s", (uid,))
        if not cur.fetchone():
            print(f"Inserting {uid}")
            cur.execute("""
                INSERT INTO user_profiles (id, clerk_user_id, email, first_name, last_name, tier, updated_at)
                VALUES (%s, 'dev_clerk_id', 'dev@local.host', 'Dev', 'User', 'FREE', NOW())
            """, (uid,))
            conn.commit()
            print("Inserted successfully.")
        else:
            print(f"User {uid} already exists.")
            
        cur.close()
        conn.close()
    except Exception as e:
        print("Error:", e)

if __name__ == "__main__":
    fix_db()
