package ws

import (
	"log"
	"sync"

	"e2e-platform/apps/control-plane/internal/authz"
	"e2e-platform/apps/control-plane/internal/middleware"

	"github.com/gin-gonic/gin"
	"github.com/olahol/melody"
	"gorm.io/gorm"
)

// Manager handles WebSocket connections for authorized job subscribers only.
type Manager struct {
	m *melody.Melody
	// Map JobID -> List of Sessions
	sessions map[string][]*melody.Session
	lock     sync.RWMutex
	db       *gorm.DB
}

// NewManager wires the DB checker for job ownership at connection time.
func NewManager(gdb *gorm.DB) *Manager {
	m := melody.New()

	mgr := &Manager{
		m:        m,
		sessions: make(map[string][]*melody.Session),
		db:       gdb,
	}

	m.HandleConnect(func(s *melody.Session) {
		jobID := s.Request.URL.Query().Get("job_id")
		if jobID == "" {
			log.Printf("[WS] Reject: missing job_id query parameter")
			_ = s.Close()
			return
		}

		userID, err := middleware.ResolveBearerFromRequest(s.Request)
		if err != nil {
			log.Printf("[WS] Reject: authentication failed for job %s: %v", jobID, err)
			_ = s.Close()
			return
		}

		if !authz.UserOwnsJob(mgr.db, jobID, userID) {
			log.Printf("[WS] Reject: forbidden job access job=%s user=%s", jobID, userID)
			_ = s.Close()
			return
		}

		log.Printf("🟢 WS Connected: Job %s", jobID)

		mgr.lock.Lock()
		mgr.sessions[jobID] = append(mgr.sessions[jobID], s)
		mgr.lock.Unlock()
	})

	m.HandleDisconnect(func(s *melody.Session) {
		jobID := s.Request.URL.Query().Get("job_id")
		log.Printf("🔴 WS Disconnected: Job %s", jobID)

		mgr.lock.Lock()
		defer mgr.lock.Unlock()

		if conns, ok := mgr.sessions[jobID]; ok {
			newConns := conns[:0]
			for _, conn := range conns {
				if conn != s {
					newConns = append(newConns, conn)
				}
			}
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
	log.Printf("[WS] Incoming Handshake Request from %s for URL: %s", c.Request.RemoteAddr, c.Request.URL.String())
	err := mgr.m.HandleRequest(c.Writer, c.Request)
	if err != nil {
		log.Printf("[WS] Upgrade Error: %v", err)
	}
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
