package streaming

import (
	"log"
	"strings"
	"sync"

	"github.com/nats-io/nats.go"
)

// DataAccumulator stores extracted data in memory (simulate Redis for now)
type DataAccumulator struct {
	mu   sync.RWMutex
	data map[string][]string // job_id -> []extracted_payloads
}

var GlobalAccumulator = &DataAccumulator{
	data: make(map[string][]string),
}

func (a *DataAccumulator) Append(jobID string, payload string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.data[jobID] = append(a.data[jobID], payload)
}

func (a *DataAccumulator) RetrieveAndClear(jobID string) []string {
	a.mu.Lock()
	defer a.mu.Unlock()
	data := a.data[jobID]
	delete(a.data, jobID)
	return data
}

// StartDataSubscriber connects to NATS and listens for extracted data chunks
func StartDataSubscriber(natsURL string) error {
	nc, err := nats.Connect(natsURL)
	if err != nil {
		return err
	}
	
	js, err := nc.JetStream()
	if err != nil {
		return err
	}

	subject := "quanta.data.extracted.*"
	_, err = js.Subscribe(subject, func(msg *nats.Msg) {
		// Subject format: quanta.data.extracted.<job_id>
		parts := strings.Split(msg.Subject, ".")
		if len(parts) < 4 {
			return
		}
		jobID := parts[3]
		payload := string(msg.Data)
		
		GlobalAccumulator.Append(jobID, payload)
		log.Printf("[NATS] Received data for job %s", jobID)
		msg.Ack()
	})

	if err != nil {
		return err
	}

	log.Printf("[NATS] Subscribed to %s", subject)
	return nil
}
