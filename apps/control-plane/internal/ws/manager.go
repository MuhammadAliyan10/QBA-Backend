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
		// If no Job ID, we treat it as a "Global Dashboard" listener (optional)
		if jobID == "" {
			return
		}

		log.Printf("🟢 WS Connected: Job %s", jobID)

		mgr.lock.Lock()
		mgr.sessions[jobID] = append(mgr.sessions[jobID], s)
		mgr.lock.Unlock()
	})

	// Handle Disconnect (THE FIX)
	m.HandleDisconnect(func(s *melody.Session) {
		jobID := s.Request.URL.Query().Get("job_id")
		log.Printf("🔴 WS Disconnected: Job %s", jobID)

		mgr.lock.Lock()
		defer mgr.lock.Unlock()

		// Remove this specific session from the list to prevent Memory Leaks
		if conns, ok := mgr.sessions[jobID]; ok {
			// Filter in place
			newConns := conns[:0]
			for _, conn := range conns {
				if conn != s {
					newConns = append(newConns, conn)
				}
			}
			// If list is empty, delete the map key to save memory
			if len(newConns) == 0 {
				delete(mgr.sessions, jobID)
			} else {
				mgr.sessions[jobID] = newConns
			}
		}
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
