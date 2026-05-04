# Quanta CLI 🌌

The official command-line interface for the **Quanta Execution-as-a-Service (EaaS)** platform. Quanta allows you to automate complex web workflows using high-level natural language prompts, powered by autonomous browser agents and Bring Your Own Session (BYOS) authentication.

## 🚀 Getting Started

### Installation

Clone the repository and install the CLI in editable mode:

```bash
cd backend/apps/quanta-cli
pip install -e .
```

### Configuration

Set your Quanta API key to authenticate with the Control Plane:

```bash
quanta config set-key YOUR_API_KEY
```

---

## 🛠 Commands

### 1. `quanta auth`
Authenticates a session for a specific website and stores it in your secure vault (BYOS).

```bash
quanta auth https://github.com/login
```
- **How it works**: Opens a managed browser window. Once you log in, Quanta captures the session state (cookies, local storage), encrypts it with AES-256-GCM, and uploads it to the Quanta Vault.
- **Output**: Returns a `Vault ID` which you can use for authenticated missions.

### 2. `quanta execute`
Triggers an autonomous execution mission.

```bash
quanta execute https://github.com/ --prompt "Extract the names of my top repositories" --vault-id <VAULT_ID>
```
- **Parameters**:
  - `url`: The target website.
  - `--prompt`: Natural language instructions for the agent.
  - `--vault-id`: (Optional) The ID of a saved session to inject for authenticated access.
- **Features**: 
  - **Live Telemetry**: Streams real-time logs and extracted data to your terminal.
  - **Autonomous Planning**: Dynamically generates execution steps based on the live page structure.

---

## 🌐 API Reference (Control Plane)

The CLI communicates with the Quanta Control Plane via the following REST endpoints:

### **Execution**
| Method | Route | Description |
| :--- | :--- | :--- |
| `POST` | `/v1/execute` | Dispatches an asynchronous mission. |
| `GET` | `/v1/jobs/:id` | Returns the current status and result of a job. |
| `GET` | `/v1/execute/:id/stream` | (SSE) Streams live execution logs and data events. |

### **Vault (Credentials)**
| Method | Route | Description |
| :--- | :--- | :--- |
| `POST` | `/v1/credentials` | Securely stores an encrypted session state. |
| `GET` | `/v1/credentials` | Lists all vaulted session IDs and domains. |
| `DELETE` | `/v1/credentials/:id` | Removes a session from the vault. |

### **Sighted Pipeline**
| Method | Route | Description |
| :--- | :--- | :--- |
| `POST` | `/v1/sighted` | Optimized harvesting pipeline for structured data extraction. |

---

## 🧠 How It Works

1.  **Auth (BYOS)**: You log in once; Quanta "borrows" the session. No need to share cleartext passwords.
2.  **Preflight**: The Control Plane validates the mission and injects the vaulted session into a Temporal workflow.
3.  **Execution**: A Python worker (Execution Plane) spawns a headless browser, restores the session, and uses the **SmartFinderV2** cognitive engine to navigate and extract data.
4.  **Telemetry**: Logs flow from the worker → NATS → Control Plane → SSE → your Terminal.

---

## 🔒 Security
- **AES-256-GCM**: All vaulted sessions are encrypted at rest.
- **SSRF Protection**: The execution engine is hardened against Server-Side Request Forgery.
- **Ephemeral Workers**: Browser contexts are wiped clean after every mission.
