package httpclient

import (
	"net"
	"net/http"
	"sync"
	"time"
)

var (
	once   sync.Once
	client *http.Client
)

// GetClient returns a thread-safe singleton HTTP client optimized for high throughput.
// It prevents socket exhaustion by using a custom Transport with connection pooling.
func GetClient() *http.Client {
	once.Do(func() {
		transport := &http.Transport{
			// MaxIdleConns is the maximum number of idle (keep-alive) connections across all hosts.
			MaxIdleConns: 100,
			// MaxIdleConnsPerHost is the maximum number of idle connections to keep per-host.
			// Set to 100 to match MaxIdleConns for high-concurrency to single targets (e.g., LLM API).
			MaxIdleConnsPerHost: 100,
			// IdleConnTimeout is the maximum amount of time an idle connection will remain idle before closing itself.
			IdleConnTimeout: 90 * time.Second,
			// Proxy: http.ProxyFromEnvironment (default)

			// DialContext defines the function for creating unencrypted TCP connections.
			DialContext: (&net.Dialer{
				Timeout:   30 * time.Second,
				KeepAlive: 30 * time.Second,
			}).DialContext,

			// TLSHandshakeTimeout specifies the maximum amount of time waiting for a TLS handshake.
			TLSHandshakeTimeout: 10 * time.Second,
			// ExpectContinueTimeout specifies the amount of time to wait for a server's first response headers.
			ExpectContinueTimeout: 1 * time.Second,

			// Force HTTP/2 if possible.
			ForceAttemptHTTP2: true,
		}

		client = &http.Client{
			Transport: transport,
			// Individual request timeouts should be handled via Context,
			// but we set a sane default header-level timeout here as well.
			Timeout: 60 * time.Second,
		}
	})
	return client
}
