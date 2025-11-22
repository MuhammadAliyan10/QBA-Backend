module e2e-platform/control-plane

go 1.23.0

require (
    // --- WEB FRAMEWORK & SOCKETS ---
    // Gin: High-performance HTTP web framework.
    github.com/gin-gonic/gin v1.10.0
    // Melody: Scalable WebSocket framework based on Goroutines.
    github.com/olahol/melody v1.2.1
    // Cors: Handling Cross-Origin requests from Next.js.
    github.com/gin-contrib/cors v1.7.2

    // --- ORCHESTRATION ---
    // Temporal SDK: For triggering workflows from the API.
    go.temporal.io/sdk v1.29.1

    // --- MESSAGING ---
    // NATS JetStream: For the asynchronous event bus.
    github.com/nats-io/nats.go v1.37.0

    // --- AUTHENTICATION ---
    // Clerk: For validating JWTs from the frontend.
    github.com/clerk/clerk-sdk-go/v2 v2.0.9

    // --- DATABASE & CACHE ---
    // PGX: High-performance PostgreSQL/CockroachDB driver.
    github.com/jackc/pgx/v5 v5.7.1
    // Redis: For rate limiting and credit checks.
    github.com/redis/go-redis/v9 v9.7.0

    // --- PROTOCOLS ---
    // Protobuf & gRPC: For defining the binary API contract.
    google.golang.org/protobuf v1.35.1
    google.golang.org/grpc v1.67.1

    // --- UTILITIES ---
    // UUID: For generating Job IDs.
    github.com/google/uuid v1.6.0
    // Dotenv: For config management.
    github.com/joho/godotenv v1.5.1
)
