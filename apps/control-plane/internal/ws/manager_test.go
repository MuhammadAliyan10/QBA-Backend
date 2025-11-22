package ws

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/assert"
)

func TestNewManager(t *testing.T) {
	manager := NewManager()

	assert.NotNil(t, manager)
	assert.NotNil(t, manager.m)
	assert.NotNil(t, manager.sessions)
	assert.Equal(t, 0, len(manager.sessions))
}

func TestBroadcastToJob_NoConnections(t *testing.T) {
	manager := NewManager()

	// Should not panic when broadcasting to non-existent job
	assert.NotPanics(t, func() {
		manager.BroadcastToJob("non-existent-job", []byte("test"))
	})
}

func TestHandleRequest(t *testing.T) {
	gin.SetMode(gin.TestMode)

	manager := NewManager()
	router := gin.Default()
	router.GET("/ws", manager.HandleRequest)

	// Create test request
	req, _ := http.NewRequest("GET", "/ws?job_id=test-job", nil)
	req.Header.Set("Upgrade", "websocket")
	req.Header.Set("Connection", "Upgrade")
	req.Header.Set("Sec-WebSocket-Version", "13")
	req.Header.Set("Sec-WebSocket-Key", "test-key")

	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)

	// WebSocket upgrade attempt
	assert.True(t, w.Code == http.StatusSwitchingProtocols || w.Code == http.StatusBadRequest)
}

func TestConnectionTracking(t *testing.T) {
	manager := NewManager()

	// Verify sessions map is initialized and empty
	manager.lock.RLock()
	sessionCount := len(manager.sessions)
	manager.lock.RUnlock()

	assert.Equal(t, 0, sessionCount, "Sessions map should be empty initially")
	assert.NotNil(t, manager.sessions, "Sessions map should be initialized")
}
