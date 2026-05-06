package services

import (
	"context"
	"errors"
	"net"
	"os"
	"strings"
)

// ErrSSRFBlocked indicates the URL resolves to or targets a non-public network address.
var ErrSSRFBlocked = errors.New("target_url_blocked_ssrf")

// validatePublicHost resolves hostname and rejects loopback, private, link-local,
// and cloud metadata endpoints (best-effort SSRF mitigation for server-side fetch).
func validatePublicHost(ctx context.Context, hostname string) error {
	if os.Getenv("ALLOW_LOCAL_TARGETS") == "true" {
		return nil
	}

	hostname = strings.TrimSpace(hostname)
	if hostname == "" {
		return ErrSSRFBlocked
	}

	h := strings.ToLower(hostname)
	switch h {
	case "localhost", "metadata.google.internal", "metadata", "kubernetes.default", "kubernetes.default.svc":
		return ErrSSRFBlocked
	}

	if strings.HasSuffix(h, ".localhost") || strings.HasSuffix(h, ".internal") {
		return ErrSSRFBlocked
	}

	if ip := net.ParseIP(hostname); ip != nil {
		if isBlockedIP(ip) {
			return ErrSSRFBlocked
		}
		return nil
	}

	r := net.DefaultResolver
	addrs, err := r.LookupIPAddr(ctx, hostname)
	if err != nil {
		return err
	}
	if len(addrs) == 0 {
		return ErrSSRFBlocked
	}
	for _, na := range addrs {
		if isBlockedIP(na.IP) {
			return ErrSSRFBlocked
		}
	}
	return nil
}

func isBlockedIP(ip net.IP) bool {
	if ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() {
		return true
	}
	if ip.To4() != nil {
		if ip.Equal(net.IPv4(169, 254, 169, 254)) {
			return true
		}
	}
	if ip.Equal(net.IPv6loopback) {
		return true
	}
	return false
}
