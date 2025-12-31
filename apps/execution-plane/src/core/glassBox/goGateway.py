"""Go Gateway WebSocket Hub - Reference Implementation"""

GO_GATEWAY_CODE = """
package main

import (
    "sync"
    "github.com/gofiber/fiber/v2"
    "github.com/gofiber/websocket/v2"
    "github.com/nats-io/nats.go"
)

type Client struct {
    Conn       *websocket.Conn
    WorkflowID string
}

type Hub struct {
    clients    map[string][]*Client
    subsLock   sync.RWMutex
    natsConn   *nats.Conn
    natsSubs   map[string]*nats.Subscription
}

var hub = &Hub{
    clients:  make(map[string][]*Client),
    natsSubs: make(map[string]*nats.Subscription),
}

func wsHandler(c *websocket.Conn) {
    workflowID := c.Params("id")
    client := &Client{Conn: c, WorkflowID: workflowID}

    hub.RegisterClient(client)
    defer hub.UnregisterClient(client)

    for {
        _, msg, err := c.ReadMessage()
        if err != nil {
            break
        }
        hub.natsConn.Publish("bot.input."+workflowID, msg)
    }
}

func (h *Hub) RegisterClient(client *Client) {
    h.subsLock.Lock()
    defer h.subsLock.Unlock()

    h.clients[client.WorkflowID] = append(h.clients[client.WorkflowID], client)

    if len(h.clients[client.WorkflowID]) == 1 {
        sub, _ := h.natsConn.Subscribe("bot.stream."+client.WorkflowID, func(msg *nats.Msg) {
            h.BroadcastFrame(client.WorkflowID, msg.Data)
        })
        h.natsSubs[client.WorkflowID] = sub
    }
}

func (h *Hub) BroadcastFrame(workflowID string, data []byte) {
    h.subsLock.RLock()
    clients := h.clients[workflowID]
    h.subsLock.RUnlock()

    for _, client := range clients {
        client.Conn.WriteMessage(websocket.BinaryMessage, data)
    }
}

func main() {
    nc, _ := nats.Connect("nats://localhost:4222")
    hub.natsConn = nc

    app := fiber.New()
    app.Get("/ws/:id", websocket.New(wsHandler))
    app.Listen(":8080")
}
"""
