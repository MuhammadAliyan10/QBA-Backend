package config

import (
	"os"
	"strconv"
	"strings"
)

// FeatureFlags holds all feature flag states for the control plane.
// All flags default to false for safe deployment.
type FeatureFlags struct {
	EnableBilling       bool
	EnableS3Upload      bool
	EnableNotifications bool
}

// flags is the singleton instance of FeatureFlags
var flags *FeatureFlags

// GetFlags returns the feature flags singleton, initializing from env on first call.
func GetFlags() *FeatureFlags {
	if flags == nil {
		flags = &FeatureFlags{
			EnableBilling:       parseBool(os.Getenv("ENABLE_BILLING"), false),
			EnableS3Upload:      parseBool(os.Getenv("ENABLE_S3_UPLOAD"), false),
			EnableNotifications: parseBool(os.Getenv("ENABLE_NOTIFICATIONS"), false),
		}
	}
	return flags
}

// parseBool parses a boolean from string with a default value.
// Accepts: "true", "1", "yes", "on" (case insensitive) as true.
func parseBool(val string, defaultVal bool) bool {
	if val == "" {
		return defaultVal
	}

	val = strings.ToLower(strings.TrimSpace(val))
	switch val {
	case "true", "1", "yes", "on":
		return true
	case "false", "0", "no", "off":
		return false
	default:
		return defaultVal
	}
}

// IsBillingEnabled returns whether billing features are active.
func IsBillingEnabled() bool {
	return GetFlags().EnableBilling
}

// IsS3UploadEnabled returns whether S3 upload is active.
func IsS3UploadEnabled() bool {
	return GetFlags().EnableS3Upload
}

// IsNotificationsEnabled returns whether notification integrations are active.
func IsNotificationsEnabled() bool {
	return GetFlags().EnableNotifications
}

// GetInt reads an integer from environment with a default value.
func GetInt(key string, defaultVal int) int {
	val := os.Getenv(key)
	if val == "" {
		return defaultVal
	}
	i, err := strconv.Atoi(val)
	if err != nil {
		return defaultVal
	}
	return i
}

// GetString reads a string from environment with a default value.
func GetString(key, defaultVal string) string {
	val := os.Getenv(key)
	if val == "" {
		return defaultVal
	}
	return val
}
