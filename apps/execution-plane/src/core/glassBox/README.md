# Glass Box - Real-Time Browser Streaming

Real-time video streaming from headless Playwright to frontend.

## Data Flow

```
┌────────────────────────────────────────────────────────────────────┐
│  PYTHON WORKER                                                      │
│                                                                     │
│  Playwright ──► CDP Session ──► BrowserStreamer ──► NATS Publish   │
│                    │                                    │           │
│            startScreencast                     bot.stream.{id}     │
│                    │                                    │           │
│            screencastFrame ◄─────── frameAck           │           │
└───────────────────────────────────────────────────────┬┴───────────┘
                                                        │
                                      ┌─────────────────▼──────────────┐
                                      │  GO GATEWAY                    │
                                      │                                │
                                      │  NATS Subscribe ──► WS Hub    │
                                      │       │                │       │
                                      │       ▼                ▼       │
                                      │  Binary Frame   WebSocket      │
                                      │  Passthrough    Clients        │
                                      └─────────────────┬──────────────┘
                                                        │
┌───────────────────────────────────────────────────────▼──────────────┐
│  FRONTEND                                                            │
│                                                                      │
│  WebSocket ──► Canvas.drawImage()                                   │
│       ▲                                                              │
│       │                                                              │
│  Mouse/Key Events ──► JSON ──► NATS ──► handle_remote_input()       │
└──────────────────────────────────────────────────────────────────────┘
```

## Coordinate Mapping

Frontend canvas may be different size than browser viewport:

```
Canvas (800×600)  →  Viewport (1280×720)
     (100, 50)    →       (160, 60)

scale_x = viewport_width / canvas_width = 1.6
actual_x = event_x × scale_x
```

## Usage

```python
from core.glassBox import BrowserStreamer, InputBridge

# Streaming
async with BrowserStreamer(page, "workflow_123") as streamer:
    await do_automation()

# Remote control
bridge = InputBridge(page)
await bridge.handle_event({"type": "click", "x": 500, "y": 200})
```

## RecipeEngine Integration

```python
engine = RecipeEngine("job_123")
await engine.enable_streaming()
await engine.handle_remote_input({"type": "click", "x": 100, "y": 50})
await engine.disable_streaming()
```

## NATS Subjects

| Subject           | Direction   | Format                         |
| ----------------- | ----------- | ------------------------------ |
| `bot.stream.{id}` | Python → Go | Binary JPEG                    |
| `bot.input.{id}`  | Go → Python | JSON `{"type", "x", "y", ...}` |

## Performance Targets

| Stage          | Target    |
| -------------- | --------- |
| CDP Frame      | 15-20 FPS |
| NATS Publish   | <1ms      |
| WebSocket Send | <5ms      |
| End-to-End     | <100ms    |
