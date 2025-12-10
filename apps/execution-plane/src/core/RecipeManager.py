"""
RecipeManager - Dynamic Recipe Storage & Retrieval using Vector Search

Manages workflows/recipes using Qdrant vector database for semantic search.
Shares the sentence-transformer model with TensorEngine (singleton pattern).

Usage:
    mgr = RecipeManager()

    # Store a recipe
    mgr.save_recipe(
        name="github_login",
        description="Login to GitHub and navigate to repositories",
        steps=[...]
    )

    # Find a recipe by natural language
    recipe = mgr.find_recipe("authenticate on github")
"""

import os
import logging
import time
import hashlib
from typing import Dict, List, Optional
from functools import lru_cache
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from sentence_transformers import SentenceTransformer

logger = logging.getLogger("recipeManager")

# Import existing TensorEngine to share the model
from core.TensorEngine import TensorEngine


class RecipeManager:
    """
    The Recipe Brain - Vector-based workflow storage and retrieval with LRU caching.

    Performance Optimization:
    - Read-through RAM cache (LRU with TTL)
    - Shared sentence-transformer model
    - Single Qdrant query per unique recipe name
    """

    def __init__(self, cache_size: int = 128, cache_ttl: int = 3600):
        """
        Initialize RecipeManager with Qdrant connection and shared model.

        Args:
            cache_size: Maximum number of recipes to cache in RAM
            cache_ttl: Cache time-to-live in seconds (default: 1 hour)
        """
        # Connect to Qdrant with timeout to prevent indefinite hangs
        qdrant_url = os.getenv("QDRANT_URL", "http://localhost:6333")
        qdrant_timeout = int(os.getenv("QDRANT_TIMEOUT", "10"))  # Default: 10 seconds

        self.client = QdrantClient(
            url=qdrant_url,
            timeout=qdrant_timeout,  # CRITICAL: Prevents worker from hanging if Qdrant is slow/down
            prefer_grpc=False  # Use REST for better timeout handling
        )

        # Collection name for recipes
        self.collection_name = "recipes"

        # Initialize collection if it doesn't exist
        self._initialize_collection()

        # Share the sentence-transformer model with TensorEngine (singleton)
        tensor_engine = TensorEngine()
        self.model: SentenceTransformer = tensor_engine.model

        # LRU Cache with TTL
        self.cache: Dict[str, Dict] = {}  # {query_hash: {recipe, timestamp}}
        self.cache_ttl = cache_ttl
        self.cache_size = cache_size

        logger.info(f"[System] RecipeManager initialized (Qdrant: {qdrant_url}, Model: shared singleton, Cache: {cache_size} slots)")

    def _initialize_collection(self):
        """
        Create the recipes collection if it doesn't exist.
        """
        try:
            # Check if collection exists
            collections = self.client.get_collections().collections
            exists = any(c.name == self.collection_name for c in collections)

            if not exists:
                # Create collection with 384-dimensional vectors (all-MiniLM-L6-v2)
                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=VectorParams(
                        size=384,  # all-MiniLM-L6-v2 dimension
                        distance=Distance.COSINE
                    )
                )
                logger.info(f"Created Qdrant collection: {self.collection_name}")
            else:
                logger.debug(f"Collection '{self.collection_name}' already exists")

        except Exception as e:
            logger.error(f"Failed to initialize collection: {e}", exc_info=True)
            raise

    def save_recipe(
        self,
        name: str,
        description: str,
        steps: List[Dict],
        user_id: Optional[str] = None
    ) -> bool:
        """
        Store a recipe in Qdrant with semantic embedding.

        Args:
            name: Unique recipe identifier (e.g., "github_login")
            description: Natural language description for semantic search
            steps: List of action dictionaries
            user_id: Optional user ID for multi-tenancy

        Returns:
            True if successful, False otherwise

        Example:
            mgr.save_recipe(
                name="github_login",
                description="Authenticate on GitHub and access repository settings",
                steps=[
                    {"action": "GOTO", "params": {"url": "https://github.com/login"}},
                    {"action": "TYPE", "params": {"intent": "username", "text": "{username}"}},
                ]
            )
        """
        try:
            # Generate embedding from description
            vector = self.model.encode(
                description.lower().strip(),
                convert_to_numpy=True,
                normalize_embeddings=True
            ).tolist()

            # Create point for Qdrant
            # Use SHA256 for collision-resistant ID (Python's hash() is unstable and can collide)
            point_id = int(hashlib.sha256(name.encode()).hexdigest()[:15], 16)

            point = PointStruct(
                id=point_id,
                vector=vector,
                payload={
                    "name": name,
                    "description": description,
                    "steps": steps,
                    "user_id": user_id or "system"
                }
            )

            # Upsert to Qdrant (insert or update)
            self.client.upsert(
                collection_name=self.collection_name,
                points=[point]
            )

            # Invalidate cache for this recipe name
            self._invalidate_cache(name)

            logger.info(f"[Storage] Saved recipe:'{name}' (embedding dim: {len(vector)}, cache invalidated)")
            return True

        except Exception as e:
            logger.error(f"Failed to save recipe '{name}': {e}", exc_info=True)
            return False

    def find_recipe(
        self,
        query: str,
        user_id: Optional[str] = None,
        threshold: float = 0.7
    ) -> Optional[Dict]:
        """
        Find the best matching recipe using semantic search with LRU caching.

        Performance Optimization:
        1. Check RAM cache first (O(1))
        2. If cache miss, query Qdrant
        3. Store result in cache
        4. Evict oldest entry if cache full

        Args:
            query: Natural language query (e.g., "login to github")
            user_id: Optional user ID for filtering
            threshold: Minimum similarity score (0.0-1.0, default 0.7)

        Returns:
            Dictionary with recipe data if found, None otherwise
        """
        try:
            # Generate cache key (hash of normalized query)
            cache_key = hash(query.lower().strip()) % (10 ** 10)

            # 1. CHECK CACHE FIRST (The Optimization)
            if cache_key in self.cache:
                cached_entry = self.cache[cache_key]
                cached_time = cached_entry['timestamp']

                # Check if cache entry is still valid (TTL)
                if (time.time() - cached_time) < self.cache_ttl:
                    recipe = cached_entry['recipe']
                    logger.info(f"Cache hit: '{recipe['name']}' (age: {int(time.time() - cached_time)}s)")
                    return recipe
                else:
                    # Cache expired, remove it
                    del self.cache[cache_key]
                    logger.debug(f"[Error] Cache EXPIRED for query:'{query}'")

            # 2. CACHE MISS - Query Qdrant
            logger.debug(f"[Logic] Cache MISS: Querying Qdrant for'{query}'")

            # Generate query embedding
            query_vector = self.model.encode(
                query.lower().strip(),
                convert_to_numpy=True,
                normalize_embeddings=True
            )

            # Perform semantic search in Qdrant
            search_result = self.client.query_points(
                collection_name=self.collection_name,
                query=query_vector.tolist(),
                limit=1,
                score_threshold=threshold
            )

            if not search_result or not search_result.points:
                logger.warning(f"[RAG] No recipe found for query: '{query}' (threshold: {threshold})")
                return None

            # Extract the best match
            best_match = search_result.points[0]
            score = best_match.score
            payload = best_match.payload

            recipe = {
                "name": payload["name"],
                "description": payload["description"],
                "steps": payload["steps"],
                "score": score
            }

            logger.info(f"[System] Found recipe:'{recipe['name']}' (score: {score:.3f})")

            # 3. UPDATE CACHE
            self._update_cache(cache_key, recipe)

            return recipe

        except Exception as e:
            logger.error(f"Recipe search failed for query '{query}': {e}", exc_info=True)
            return None

    def _update_cache(self, key: str, recipe: Dict):
        """
        Update cache with LRU eviction policy.

        Args:
            key: Cache key (hash of query)
            recipe: Recipe data to cache
        """
        # If cache is full, evict oldest entry (LRU)
        if len(self.cache) >= self.cache_size:
            # Find and remove oldest entry
            oldest_key = min(self.cache.keys(), key=lambda k: self.cache[k]['timestamp'])
            del self.cache[oldest_key]
            logger.debug(f"[Storage] Cache full: Evicted oldest entry")

        # Add new entry
        self.cache[key] = {
            'recipe': recipe,
            'timestamp': time.time()
        }
        logger.debug(f"[Storage] Cached recipe:'{recipe['name']}' (cache size: {len(self.cache)}/{self.cache_size})")

    def _invalidate_cache(self, recipe_name: Optional[str] = None):
        """
        Invalidate cache entries.

        Args:
            recipe_name: If specified, only invalidate entries for this recipe.
                        If None, clear entire cache.
        """
        if recipe_name is None:
            # Clear entire cache
            self.cache.clear()
            logger.info("[Storage] Cache cleared (all entries)")
        else:
            # Remove specific recipe from cache
            keys_to_remove = [
                k for k, v in self.cache.items()
                if v['recipe']['name'] == recipe_name
            ]
            for key in keys_to_remove:
                del self.cache[key]
            logger.debug(f"[Storage] Cache invalidated for recipe:'{recipe_name}'")

    def list_recipes(self, user_id: Optional[str] = None, limit: int = 100) -> List[Dict]:
        """
        List all recipes (optionally filtered by user_id).

        Args:
            user_id: Optional user filter
            limit: Maximum number of recipes to return

        Returns:
            List of recipe dictionaries
        """
        try:
            # Scroll through all points
            result = self.client.scroll(
                collection_name=self.collection_name,
                limit=limit,
                with_payload=True,
                with_vectors=False
            )

            points = result[0]  # (points, next_page_offset)

            recipes = []
            for point in points:
                payload = point.payload

                # Filter by user_id if specified
                if user_id and payload.get("user_id") != user_id:
                    continue

                recipes.append({
                    "name": payload["name"],
                    "description": payload["description"],
                    "steps": payload["steps"]
                })

            logger.info(f"📋 Listed {len(recipes)} recipes")
            return recipes

        except Exception as e:
            logger.error(f"Failed to list recipes: {e}", exc_info=True)
            return []

    def delete_recipe(self, name: str) -> bool:
        """
        Delete a recipe by name and invalidate cache.

        Args:
            name: Recipe name to delete

        Returns:
            True if successful
        """
        try:
            # Use same SHA256-based ID generation as save_recipe
            point_id = int(hashlib.sha256(name.encode()).hexdigest()[:15], 16)

            self.client.delete(
                collection_name=self.collection_name,
                points_selector=[point_id]
            )

            # Invalidate cache
            self._invalidate_cache(name)

            logger.info(f"[Storage] Deleted recipe:'{name}' (cache invalidated)")
            return True

        except Exception as e:
            logger.error(f"Failed to delete recipe '{name}': {e}", exc_info=True)
            return False


# Example usage
if __name__ == "__main__":
    """
    Standalone test for RecipeManager.
    """
    import asyncio

    print("=" * 60)
    print("RECIPE MANAGER - STANDALONE TEST")
    print("=" * 60)

    mgr = RecipeManager()

    # Test 1: Save a recipe
    print("\n[Test 1] Saving recipe...")
    success = mgr.save_recipe(
        name="github_explorer",
        description="Navigate to GitHub and explore repositories",
        steps=[
            {"action": "GOTO", "params": {"url": "https://github.com"}},
            {"action": "CLICK", "params": {"intent": "Explore"}},
        ]
    )
    print(f"Save result: {success}")

    # Test 2: Find recipe by exact match
    print("\n[Test 2] Finding recipe (exact)...")
    recipe = mgr.find_recipe("explore github repositories")
    if recipe:
        print(f"Found: {recipe['name']} (score: {recipe['score']:.3f})")
    else:
        print("No match found")

    # Test 3: Find recipe by semantic match
    print("\n[Test 3] Finding recipe (semantic)...")
    recipe = mgr.find_recipe("browse github projects")
    if recipe:
        print(f"Found: {recipe['name']} (score: {recipe['score']:.3f})")
    else:
        print("No match found")

    # Test 4: List all recipes
    print("\n[Test 4] Listing all recipes...")
    all_recipes = mgr.list_recipes()
    for r in all_recipes:
        print(f"  - {r['name']}: {r['description'][:50]}...")

    print("\n" + "=" * 60)
    print("TESTS COMPLETE")
    print("=" * 60)
