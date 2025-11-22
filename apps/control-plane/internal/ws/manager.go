package ws

import (
	"log"
	"sync"

	"github.com/gin-gonic/gin"
	"github.com/olahol/melody"
)

// Manager handles all WebSocket connections
type Manager struct {
	m *melody.Melody
	// Map JobID -> List of Sessions
	sessions map[string][]*melody.Session
	lock     sync.RWMutex
}

func NewManager() *Manager {
	m := melody.New()

	mgr := &Manager{
		m:        m,
		sessions: make(map[string][]*melody.Session),
	}

	// Handle Connection
	m.HandleConnect(func(s *melody.Session) {
		jobID := s.Request.URL.Query().Get("job_id")
		if jobID == "" {
			// Optional: Allow connecting without job_id for general dashboard updates
			log.Println("🟢 WS Connected (Global)")
			return
		}

		log.Printf("🟢 WS Connected: Job %s", jobID)

		mgr.lock.Lock()
		mgr.sessions[jobID] = append(mgr.sessions[jobID], s)
		mgr.lock.Unlock()
	})

	// Handle Disconnect
	m.HandleDisconnect(func(s *melody.Session) {
		jobID := s.Request.URL.Query().Get("job_id")
		log.Printf("🔴 WS Disconnected: Job %s", jobID)
	})

	return mgr
}

// HandleRequest upgrades HTTP to WebSocket
func (mgr *Manager) HandleRequest(c *gin.Context) {
	mgr.m.HandleRequest(c.Writer, c.Request)
}

// BroadcastToJob sends a message ONLY to users watching a specific job
func (mgr *Manager) BroadcastToJob(jobID string, message []byte) {
	mgr.lock.RLock()
	defer mgr.lock.RUnlock()

	conns, ok := mgr.sessions[jobID]
	if !ok {
		return
	}

	for _, s := range conns {
		if !s.IsClosed() {
			s.Write(message)
		}
	}
}
