import hashlib
import psycopg2

key = "sk_live_1234567890"
key_hash = hashlib.sha256(key.encode()).hexdigest()

# Using default credentials from docker-compose if port 5433 is mapped
conn = psycopg2.connect("postgresql://postgres:postgres@localhost:5433/quanta")
cursor = conn.cursor()

cursor.execute("INSERT INTO user_profiles (id, clerk_user_id, email) VALUES ('00000000-0000-0000-0000-000000000000', 'clerk_000', 'test@test.com') ON CONFLICT DO NOTHING")
cursor.execute(f"INSERT INTO api_keys (id, user_id, name, key_prefix, key_hash, is_active) VALUES ('00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000000', 'Test Key', 'sk_live_1234', '{key_hash}', true) ON CONFLICT DO NOTHING")

conn.commit()
print("Success")
