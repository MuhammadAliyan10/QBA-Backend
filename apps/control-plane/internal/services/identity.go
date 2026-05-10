// internal/services/identity.go
package services

import (
	"errors"
	"fmt"
	"log"
	"sync"
	"time"

	"e2e-platform/apps/control-plane/internal/models"

	"gorm.io/gorm"
)

// ErrUserNotFound is returned when a Clerk user ID has no corresponding UserProfile.
// This means the Clerk user.created webhook has not yet fired, or the user was deleted.
var ErrUserNotFound = errors.New("user profile not found")

// IdentityService resolves Clerk user IDs to internal database UUIDs.
// It maintains a short-lived in-memory cache to avoid repeated DB lookups
// on concurrent requests from the same user session.
type IdentityService struct {
	db    *gorm.DB
	mu    sync.RWMutex
	cache map[string]cachedIdentity
	ttl   time.Duration
}

type cachedIdentity struct {
	dbUserID string
	fetchedAt time.Time
}

// NewIdentityService creates a new IdentityService.
func NewIdentityService(db *gorm.DB) *IdentityService {
	return &IdentityService{
		db:    db,
		cache: make(map[string]cachedIdentity),
		ttl:   5 * time.Minute,
	}
}

// ResolveUserProfileID maps a Clerk user ID (from JWT "sub" claim, e.g. "user_2abc...")
// to the internal database UUID (user_profiles.id).
//
// This function NEVER auto-creates profiles. User provisioning is handled exclusively
// by the Clerk webhook listener (POST /v1/webhooks/clerk → user.created event).
// If the user is not found, it returns ErrUserNotFound.
func (is *IdentityService) ResolveUserProfileID(clerkUserID string) (string, error) {
	if clerkUserID == "" {
		return "", fmt.Errorf("identity: empty clerk user ID")
	}

	// Check cache first
	is.mu.RLock()
	if cached, ok := is.cache[clerkUserID]; ok && time.Since(cached.fetchedAt) < is.ttl {
		is.mu.RUnlock()
		return cached.dbUserID, nil
	}
	is.mu.RUnlock()

	// Cache miss — query database
	var profile models.UserProfile
	err := is.db.
		Select("id").
		Where("clerk_user_id = ?", clerkUserID).
		First(&profile).Error

	if errors.Is(err, gorm.ErrRecordNotFound) {
		log.Printf("[Identity] User not found for clerk_id=%s — webhook may not have fired yet", clerkUserID)
		return "", ErrUserNotFound
	}
	if err != nil {
		return "", fmt.Errorf("identity: database lookup failed: %w", err)
	}

	// Populate cache
	is.mu.Lock()
	is.cache[clerkUserID] = cachedIdentity{
		dbUserID:  profile.ID,
		fetchedAt: time.Now(),
	}
	is.mu.Unlock()

	return profile.ID, nil
}

// InvalidateCache removes a specific Clerk user ID from the cache.
// Called after webhook events that modify user data.
func (is *IdentityService) InvalidateCache(clerkUserID string) {
	is.mu.Lock()
	delete(is.cache, clerkUserID)
	is.mu.Unlock()
}
