package services

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"strings"

	"e2e-platform/apps/control-plane/internal/httpclient"
)

var (
	// ErrWAFBlocked indicates automation is likely blocked by a WAF before execution.
	ErrWAFBlocked = errors.New("target_url_blocked_by_waf")
)

// URLValidationResult holds the result of URL validation
type URLValidationResult struct {
	Valid      bool   `json:"valid"`
	StatusCode int    `json:"statusCode,omitempty"`
	Error      string `json:"error,omitempty"`
	Domain     string `json:"domain,omitempty"`
}

// ValidateURL checks if a URL is reachable and returns the domain.
// Returns ErrSSRFBlocked when the host resolves to non-public addresses.
// Returns ErrWAFBlocked when a strict WAF block is detected (optional callers).
func ValidateURL(ctx context.Context, rawURL string) (*URLValidationResult, error) {
	// Normalize URL - add https:// if missing
	if !strings.HasPrefix(rawURL, "http://") && !strings.HasPrefix(rawURL, "https://") {
		rawURL = "https://" + rawURL
	}

	parsedURL, err := url.Parse(rawURL)
	if err != nil {
		return &URLValidationResult{
			Valid: false,
			Error: fmt.Sprintf("Invalid URL format: %v", err),
		}, nil
	}

	if parsedURL.Scheme != "http" && parsedURL.Scheme != "https" {
		return &URLValidationResult{
			Valid: false,
			Error: "Only http and https URLs are allowed",
		}, nil
	}

	host := parsedURL.Hostname()
	if host == "" {
		return &URLValidationResult{
			Valid: false,
			Error: "Could not extract host from URL",
		}, nil
	}

	if err := validatePublicHost(ctx, host); err != nil {
		if errors.Is(err, ErrSSRFBlocked) {
			return nil, ErrSSRFBlocked
		}
		return &URLValidationResult{
			Valid: false,
			Error: fmt.Sprintf("Could not validate host: %v", err),
			Domain: host,
		}, nil
	}

	domain := host

	if os.Getenv("ALLOW_LOCAL_TARGETS") == "true" {
		return &URLValidationResult{
			Valid:      true,
			StatusCode: 200,
			Domain:     domain,
		}, nil
	}

	client := httpclient.GetClient()

	req, err := http.NewRequestWithContext(ctx, "GET", rawURL, nil)
	if err != nil {
		return &URLValidationResult{
			Valid:  false,
			Error:  fmt.Sprintf("Failed to create request: %v", err),
			Domain: domain,
		}, nil
	}

	req.Header.Set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
	req.Header.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8")
	req.Header.Set("Accept-Language", "en-US,en;q=0.5")
	req.Header.Set("Accept-Encoding", "gzip, deflate, br")
	req.Header.Set("Connection", "keep-alive")
	req.Header.Set("Upgrade-Insecure-Requests", "1")

	resp, err := client.Do(req)
	if err != nil {
		return &URLValidationResult{
			Valid:  false,
			Error:  fmt.Sprintf("Target URL is unreachable: %v", err),
			Domain: domain,
		}, nil
	}
	defer resp.Body.Close()

	isCloudflare := strings.Contains(strings.ToLower(resp.Header.Get("Server")), "cloudflare") ||
		resp.Header.Get("CF-RAY") != "" ||
		resp.Header.Get("cf-mitigated") != ""

	if isCloudflare && resp.StatusCode == http.StatusForbidden {
		return &URLValidationResult{Valid: true, Domain: domain}, ErrWAFBlocked
	}

	if resp.StatusCode == http.StatusForbidden {
		return &URLValidationResult{Valid: true, Domain: domain}, ErrWAFBlocked
	}

	if resp.StatusCode >= 500 {
		return &URLValidationResult{
			Valid:      false,
			StatusCode: resp.StatusCode,
			Error:      fmt.Sprintf("Target server is experiencing issues (HTTP %d)", resp.StatusCode),
			Domain:     domain,
		}, nil
	}

	return &URLValidationResult{
		Valid:      true,
		StatusCode: resp.StatusCode,
		Domain:     domain,
	}, nil
}
