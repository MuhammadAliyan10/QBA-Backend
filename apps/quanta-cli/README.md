# Quanta CLI

The Quanta CLI is a zero-trace, developer experience tool designed for B2B developers to securely generate Bring Your Own Session (BYOS) states for the Quanta Execution Plane. 

It launches a local, headful browser for authentication, extracts the memory state, and vaults it directly to the Quanta Control Plane.

## Installation

Ensure you have Python 3.10+ and Poetry installed.

```bash
cd apps/quanta-cli
poetry install
poetry run playwright install chromium
```

## Configuration

Set your Quanta API key in the environment or place it in `~/.quanta/config.json`.

```bash
export QUANTA_API_KEY="your-api-key-here"
export QUANTA_API_URL="http://localhost:8080" # Optional, defaults to this
```

Alternatively, `~/.quanta/config.json`:
```json
{
  "api_key": "your-api-key-here"
}
```

## Usage

Run the `auth` command against your target domain:

```bash
poetry run quanta auth https://linkedin.com
```

**Workflow:**
1. A Chromium browser window will open.
2. Authenticate into the target website manually.
3. Close the browser window when finished.
4. The CLI will extract the session state in-memory (Zero-Trace) and upload it to the Quanta Vault.
5. The CLI will output your `credential_id` in green.
