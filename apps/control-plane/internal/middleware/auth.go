package middleware

import (
	"context"
	"crypto/rsa"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"log"
	"math/big"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/golang-jwt/jwt/v5"
)

// ─── CONTEXT KEYS ────────────────────────────────────────────────────────────

// ContextKey is the type for context keys to avoid collisions.
type ContextKey string

const (
	// UserIDKey is the context key for storing the verified user ID.
	UserIDKey ContextKey = "userID"
)

// ─── JWKS CACHE ─────────────────────────────────────────────────────────────

// jwksCache holds the cached Clerk JWKS (JSON Web Key Set) so we don't
// fetch it on every single request. Keys are refreshed every 10 minutes.
type jwksCache struct {
	mu      sync.RWMutex
	keys    map[string]*rsa.PublicKey // kid → public key
	fetched time.Time
	ttl     time.Duration
}

var clerkJWKS = &jwksCache{
	keys: make(map[string]*rsa.PublicKey),
	ttl:  10 * time.Minute,
}

// ─── JWKS TYPES ─────────────────────────────────────────────────────────────

type jwksResponse struct {
	Keys []jwkKey `json:"keys"`
}

type jwkKey struct {
	Kty string `json:"kty"`
	Kid string `json:"kid"`
	Use string `json:"use"`
	N   string `json:"n"`
	E   string `json:"e"`
	Alg string `json:"alg"`
}

// ─── AUTH MIDDLEWARE ─────────────────────────────────────────────────────────

// AuthMiddleware validates requests using cryptographic verification.
// Two paths only:
//
//  1. API Key:  Authorization: Bearer sk_live_xxx → SHA-256 hash → DB lookup
//  2. Clerk JWT: Authorization: Bearer <jwt> → JWKS signature verification
//
// Optional local-only bypass: SKIP_AUTH=true + DEV_USER_ID (never use in production).
// Otherwise every request is cryptographically verified or rejected with HTTP 401.
func AuthMiddleware() gin.HandlerFunc {
	if clerkIssuerURL() == "" && !strings.EqualFold(os.Getenv("SKIP_AUTH"), "true") {
		log.Println("[AUTH] WARNING: CLERK_ISSUER_URL / CLERK_PUBLISHABLE_KEY not configured. JWT auth will be unavailable for Bearer JWTs.")
	}

	return func(c *gin.Context) {
		// Explicit dev-only bypass — must never be set in production.
		if strings.EqualFold(os.Getenv("SKIP_AUTH"), "true") {
			uid := strings.TrimSpace(os.Getenv("DEV_USER_ID"))
			if uid == "" {
				uid = "dev_user_local"
			}
			log.Printf("[AUTH] WARNING: SKIP_AUTH=true — using user %s (local development only)", uid)
			setAuthUser(c, uid)
			c.Next()
			return
		}

		authHeader := strings.TrimSpace(c.GetHeader("Authorization"))
		if authHeader == "" {
			if q := strings.TrimSpace(c.Query("access_token")); q != "" {
				authHeader = "Bearer " + q
			}
		}

		userID, err := resolveAuthorizationHeader(c.Request.Context(), authHeader)
		if err != nil {
			log.Printf("[AUTH] REJECT: %v | IP=%s | Path=%s", err, c.ClientIP(), c.Request.URL.Path)
			c.JSON(http.StatusUnauthorized, gin.H{
				"error": "Authentication required. Provide an API key (Bearer sk_live_xxx) or valid JWT.",
			})
			c.Abort()
			return
		}

		setAuthUser(c, userID)
		c.Next()
	}
}

// ─── CLERK JWT VERIFICATION ─────────────────────────────────────────────────

// verifyClerkJWT cryptographically verifies a Clerk JWT token.
//
// Steps:
//  1. Parse JWT header to extract "kid" (key ID)
//  2. Fetch/cache Clerk JWKS (public keys)
//  3. Verify RS256 signature against the matching public key
//  4. Validate exp, iss claims
//  5. Extract "sub" claim (the Clerk User ID)
func verifyClerkJWT(tokenString string, issuerURL string) (string, error) {
	// Parse with key function that fetches the correct JWKS key.
	token, err := jwt.Parse(tokenString, func(t *jwt.Token) (interface{}, error) {
		// Enforce RS256
		if _, ok := t.Method.(*jwt.SigningMethodRSA); !ok {
			return nil, fmt.Errorf("unexpected signing method: %v", t.Header["alg"])
		}

		kid, ok := t.Header["kid"].(string)
		if !ok || kid == "" {
			return nil, fmt.Errorf("token missing kid header")
		}

		// Get the public key for this kid
		key, err := getClerkPublicKey(kid, issuerURL)
		if err != nil {
			return nil, fmt.Errorf("failed to get public key: %w", err)
		}

		return key, nil
	},
		jwt.WithIssuer(issuerURL),
		jwt.WithExpirationRequired(),
		jwt.WithValidMethods([]string{"RS256"}),
	)

	if err != nil {
		return "", fmt.Errorf("jwt verification failed: %w", err)
	}

	if !token.Valid {
		return "", fmt.Errorf("token is not valid")
	}

	// Extract the subject (Clerk User ID, e.g., "user_2abc123...")
	sub, err := token.Claims.GetSubject()
	if err != nil || sub == "" {
		return "", fmt.Errorf("token missing sub claim")
	}

	return sub, nil
}

// getClerkPublicKey fetches the RSA public key for the given kid from Clerk's JWKS.
func getClerkPublicKey(kid string, issuerURL string) (*rsa.PublicKey, error) {
	clerkJWKS.mu.RLock()
	if key, ok := clerkJWKS.keys[kid]; ok && time.Since(clerkJWKS.fetched) < clerkJWKS.ttl {
		clerkJWKS.mu.RUnlock()
		return key, nil
	}
	clerkJWKS.mu.RUnlock()

	// Cache miss or expired — fetch fresh JWKS.
	if err := fetchClerkJWKS(issuerURL); err != nil {
		return nil, err
	}

	clerkJWKS.mu.RLock()
	defer clerkJWKS.mu.RUnlock()

	key, ok := clerkJWKS.keys[kid]
	if !ok {
		return nil, fmt.Errorf("key ID %s not found in Clerk JWKS", kid)
	}
	return key, nil
}

// fetchClerkJWKS retrieves the JWKS from Clerk and populates the cache.
func fetchClerkJWKS(issuerURL string) error {
	jwksURL := strings.TrimRight(issuerURL, "/") + "/.well-known/jwks.json"

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "GET", jwksURL, nil)
	if err != nil {
		return fmt.Errorf("failed to create JWKS request: %w", err)
	}

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return fmt.Errorf("failed to fetch JWKS from %s: %w", jwksURL, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("JWKS endpoint returned %d", resp.StatusCode)
	}

	var jwks jwksResponse
	if err := json.NewDecoder(resp.Body).Decode(&jwks); err != nil {
		return fmt.Errorf("failed to decode JWKS: %w", err)
	}

	clerkJWKS.mu.Lock()
	defer clerkJWKS.mu.Unlock()

	clerkJWKS.keys = make(map[string]*rsa.PublicKey)
	for _, k := range jwks.Keys {
		if k.Kty != "RSA" || k.Use != "sig" {
			continue
		}
		pubKey, err := parseRSAPublicKey(k)
		if err != nil {
			log.Printf("[AUTH] WARNING: failed to parse JWKS key %s: %v", k.Kid, err)
			continue
		}
		clerkJWKS.keys[k.Kid] = pubKey
	}

	clerkJWKS.fetched = time.Now()
	log.Printf("[AUTH] Refreshed Clerk JWKS — %d keys cached", len(clerkJWKS.keys))
	return nil
}

// parseRSAPublicKey converts a JWK key to an *rsa.PublicKey.
func parseRSAPublicKey(k jwkKey) (*rsa.PublicKey, error) {
	nBytes, err := base64.RawURLEncoding.DecodeString(k.N)
	if err != nil {
		return nil, fmt.Errorf("failed to decode modulus: %w", err)
	}

	eBytes, err := base64.RawURLEncoding.DecodeString(k.E)
	if err != nil {
		return nil, fmt.Errorf("failed to decode exponent: %w", err)
	}

	n := new(big.Int).SetBytes(nBytes)
	e := 0
	for _, b := range eBytes {
		e = e<<8 + int(b)
	}

	return &rsa.PublicKey{N: n, E: e}, nil
}

// deriveClerkIssuer attempts to derive the Clerk issuer URL from the publishable key.
// Clerk publishable keys are formatted as pk_test_<base64-encoded-domain>.
func deriveClerkIssuer(publishableKey string) string {
	// Strip prefix (pk_test_ or pk_live_)
	parts := strings.SplitN(publishableKey, "_", 3)
	if len(parts) < 3 {
		return ""
	}
	encoded := parts[2]

	// Clerk base64-encodes the domain with a trailing $ — strip it.
	encoded = strings.TrimRight(encoded, "$")

	decoded, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		// Try with padding
		for i := 0; i < 3; i++ {
			encoded += "="
			decoded, err = base64.StdEncoding.DecodeString(encoded)
			if err == nil {
				break
			}
		}
		if err != nil {
			return ""
		}
	}

	domain := strings.TrimSpace(string(decoded))
	if domain == "" {
		return ""
	}

	return "https://" + domain
}

// ─── HELPERS ─────────────────────────────────────────────────────────────────

// GetUserID extracts the verified userID from the request context.
// Must be called after AuthMiddleware.
func GetUserID(c *gin.Context) (string, bool) {
	userID, exists := c.Get(string(UserIDKey))
	if !exists {
		return "", false
	}
	userIDStr, ok := userID.(string)
	return userIDStr, ok
}
