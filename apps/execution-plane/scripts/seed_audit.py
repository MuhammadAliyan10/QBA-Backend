import sys
import os

# Add src to path to import core modules
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'src'))

from qdrant_client import QdrantClient
from qdrant_client.http import models
from sentence_transformers import SentenceTransformer
import uuid

# 1. Connect to Qdrant
client = QdrantClient(url="http://localhost:6333")
model = SentenceTransformer('all-MiniLM-L6-v2')

collection_name = "recipes"

# 2. Define the Audit Recipe
recipe_text = "navigate wikipedia ceo audit investor test"
recipe_steps = [
    # Step 1: Navigate
    {"action": "GOTO", "params": {"url": "https://en.wikipedia.org/wiki/Main_Page"}},

    # Step 2: Vague Input Test (Find Search Bar)
    {"action": "TYPE", "params": {
        "intent": "input field for finding articles",
        "text": "Nvidia"
    }},

    # Step 3: Trigger Test (Click Search)
    {"action": "CLICK", "params": {"intent": "trigger the lookup"}},

    # Step 4: Semantic Link Test (Find CEO)
    # This checks if your Hybrid Semantic Scoring works!
    {"action": "CLICK", "params": {
        "intent": "link to the person who runs the company"
    }},

    # Step 5: Extraction (Placeholder - screenshot will capture proof)
    # Note: EXTRACT action not implemented, but we'll get visual proof
]

# 3. Generate Vector
vector = model.encode(recipe_text).tolist()

# 4. Upsert into Qdrant
print(f"Seeding recipe: '{recipe_text}'...")
client.upsert(
    collection_name=collection_name,
    points=[
        models.PointStruct(
            id=str(uuid.uuid4()),
            vector=vector,
            payload={
                "name": "investor_audit_v1",
                "description": "Investor Due Diligence Test: Wikipedia CEO Search",
                "steps": recipe_steps
            }
        )
    ]
)

print("✅ Recipe Seeded Successfully!")
print(f"\nTo run this recipe, use:")
print(f'  curl -X POST http://localhost:8080/run \\')
print(f'    -H "Content-Type: application/json" \\')
print(f'    -H "X-User-ID: test-investor-audit" \\')
print(f'    -d \'{{\"workflow_id\": "navigate wikipedia ceo audit"}}\'')
