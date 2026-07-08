import psycopg2
import uuid

DB_URL = "postgresql://postgres:postgres@localhost:5433/quanta"

def test_insert():
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    tenant_id = '00000000-0000-0000-0000-000000000000'
    workflow_id = str(uuid.uuid4())
    
    try:
        cur.execute("""
            INSERT INTO workflows (id, user_id, name, trigger_type, recipe_json, is_active)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (workflow_id, tenant_id, 'Test', 'ON_DEMAND', '{}', True))
        conn.commit()
        print("Workflow inserted")
    except Exception as e:
        print("Workflow insert failed:", e)
        conn.rollback()
        
    job_id = str(uuid.uuid4())
    try:
        cur.execute("""
            INSERT INTO jobs (id, user_id, workflow_id, status)
            VALUES (%s, %s, %s, %s)
        """, (job_id, tenant_id, workflow_id, 'QUEUED'))
        conn.commit()
        print("Job inserted")
    except Exception as e:
        print("Job insert failed:", e)

    cur.close()
    conn.close()

if __name__ == "__main__":
    test_insert()
