# 🚀 Deployment Guide

### _Taking e2e-Platform to Production_

This guide explains how to deploy the full stack on any server (AWS EC2, DigitalOcean, Hetzner, or bare metal).

---

## 📋 Requirements

- **OS**: Linux (Ubuntu 22.04 LTS recommended)
- **RAM**: Minimum 4GB (8GB recommended for multiple browser sessions)
- **CPU**: 2 vCPUs
- **Software**: Docker & Docker Compose

---

## 🛠️ Step-by-Step Deployment

### 1. Prepare the Server

Update your system and install Docker:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install docker.io docker-compose -y
sudo systemctl enable --now docker
```

### 2. Clone the Repository

```bash
git clone https://github.com/your-org/e2e-platform.git
cd e2e-platform
```

### 3. Configure Environment Variables

Create a production `.env` file. **NEVER** commit this file.

```bash
cp .env.production.example .env
nano .env
```

**Critical Variables to Set:**

- `GIN_MODE=release` (Optimizes Go API)
- `WEBHOOK_SECRET=...` (Secures your webhooks)
- `POSTGRES_PASSWORD=...` (Secure your DB)
- `PROXY_SERVER=...` (If you need to bypass IP blocks)

### 4. Build and Run

We use `make` to simplify the Docker commands.

```bash
# Build production images
make docker-build

# Start the stack in detached mode
make up
```

### 5. Verify Deployment

Check if all containers are healthy:

```bash
docker ps
```

You should see:

- `e2e_control_plane` (Port 8080)
- `e2e_execution_plane`
- `e2e_temporal`
- `e2e_nats`
- `e2e_postgres`

---

## 🌐 Exposing to the World (Nginx)

Do not expose port 8080 directly. Use Nginx as a reverse proxy with SSL (Certbot).

**Nginx Config Snippet:**

```nginx
server {
    listen 443 ssl;
    server_name api.yourdomain.com;

    location / {
        proxy_pass http://localhost:8080;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade"; # Critical for WebSockets
    }
}
```

---

## 🔄 Updating the Application

To deploy a new version:

1.  `git pull origin main`
2.  `make docker-build`
3.  `make up` (Docker Compose will recreate only changed containers)

---

## ⚠️ Production Checklist

- [ ] **Secrets**: Are all passwords in `.env` strong?
- [ ] **Firewall**: Is port 8080 blocked from outside? (Only Nginx should be open)
- [ ] **Persistence**: Are Docker volumes backed up?
- [ ] **Monitoring**: Check logs via `docker logs -f e2e_control_plane`
