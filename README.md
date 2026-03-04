# Diet Feedout Ask — n8n + Structured Governor

Employees can send a message to ask about **animal diet feedouts**. Every answer is passed through the **Structured Governor** so responses stay factual, constrained, and free of rhetorical drift (no moralizing, persuasion, or authority substitution).

## What’s in this repo

| Item | Purpose |
|------|--------|
| `structured_governor.py` | Governor implementation: grounding → exploration → resolution → reflection → approval, with retry + PATCH MODE. |
| `diet-feedout-ask-workflow.json` | n8n workflow: Webhook → call governor API → format and return answer. |
| `requirements.txt` | Python deps: pydantic, openai, flask. |

## Quick start

### 1. Governor (HTTP API)

The n8n workflow calls the governor over HTTP. Run the server on the same machine as n8n (or one n8n can reach):

```bash
cd "/Users/a/Desktop/Projects/n8n - Structured Gov"
pip install -r requirements.txt
export OPENAI_API_KEY="sk-..."   # optional; without it the script uses a mock LLM
python structured_governor.py --serve
```

By default the server listens on **http://localhost:5050** (override with `PORT`).

- **GET /health** — readiness check.
- **POST /govern** — body: `{ "user_message": "What are the diet feedouts for cattle?", "context": {} }`. Returns full StructuredGovernor JSON.

### 2. n8n workflow

1. In n8n: **Workflows → Import from File** and select `diet-feedout-ask-workflow.json`.
2. Ensure the **Call Structured Governor** node points to your governor URL (default `http://localhost:5050/govern`). If the governor runs on another host/port, set the **URL** in that node (e.g. `http://governor-host:5050/govern`).
3. **Activate** the workflow.
4. Use the Webhook node’s **Production URL** (e.g. `https://your-n8n.com/webhook/ask-diet-feedout`).

### 3. Employee request format

**POST** to the webhook URL with a JSON body:

```json
{
  "message": "What are the diet feedouts for cattle?"
}
```

Alternatively the workflow accepts `user_message` or `text` in the body.

### 4. Response shape

- **200**: Governor approved. Body includes `answer`, `next_actions`, `options`, `success: true`.
- **400**: Bad request (e.g. missing message). Body includes `success: false` and `error`.
- **502**: Governor error or unreachable. Body includes `success: false` and `error`.

Example success body:

```json
{
  "answer": "...",
  "next_actions": ["..."],
  "success": true,
  "error": null,
  "options": [{ "option": "...", "pros": [], "cons": [] }]
}
```

## Structured Governor (no drift)

The governor enforces:

- **Grounding**: task, known facts, constraints, unknowns, output contract, verification hooks.
- **Exploration**: approach options, chosen path, tradeoffs, alternative interpretations, timebox plan.
- **Resolution**: answer, options (pros/cons), next_actions (1–10).
- **Reflection**: drift flags and score (moralizing, persuasion, authority substitution, etc.); clarity edits.
- **Approval**: only outputs when constraints pass, structure score ≥ threshold, and drift below max.

If the first LLM output fails validation, the governor uses **PATCH MODE** to apply minimal edits and retries (up to `max_retries`), so answers stay on-contract and low-drift.

## CLI (no n8n)

To run the governor from the command line (e.g. for testing):

```bash
echo '{"user_message":"What are the diet feedouts for cattle?","context":{"domain":"animal_diet_feedouts"}}' | python structured_governor.py --stdin
```

Or pass the JSON as the first argument:

```bash
python structured_governor.py '{"user_message":"What are the diet feedouts for cattle?"}'
```

Without `OPENAI_API_KEY`, the script uses a built-in mock LLM (demo only).

## Optional: Slack / Teams

To let employees ask via Slack or Teams:

1. Add a trigger (e.g. Slack “Message posted” or Incoming Webhook) that outputs the user’s message.
2. Feed that message into the same “Build Governor Payload” logic (set `user_message` and the same `context` for diet feedouts).
3. Call the same governor URL, then format and post the reply back to the channel or user.

The workflow in this repo is HTTP-only so it works with any client (Slack, Teams, custom app, curl).

---

## Deploying on Hostinger KVM VPS

When n8n runs on a Hostinger KVM (or any Linux VPS), run the Structured Governor on the same server so n8n can call `http://localhost:5050/govern`.

### 1. SSH into the VPS

```bash
ssh your-user@your-vps-ip
```

Use the SSH details from Hostinger hPanel (VPS → SSH access).

### 2. Install Python 3 (if needed)

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv
python3 --version
```

### 3. Create app directory and copy files

```bash
sudo mkdir -p /opt/structured-governor
sudo chown $USER:$USER /opt/structured-governor
cd /opt/structured-governor
```

Copy from your machine (run from your Mac):

```bash
scp "/Users/a/Desktop/Projects/n8n - Structured Gov/structured_governor.py" your-user@your-vps-ip:/opt/structured-governor/
scp "/Users/a/Desktop/Projects/n8n - Structured Gov/requirements.txt" your-user@your-vps-ip:/opt/structured-governor/
```

### 4. Virtual env and dependencies

On the VPS:

```bash
cd /opt/structured-governor
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 5. Test run

```bash
export OPENAI_API_KEY="sk-your-key"
python3 structured_governor.py --serve
```

In another terminal: `curl http://localhost:5050/health`. Stop with `Ctrl+C` when done.

### 6. Run as a service (systemd)

Create the service file:

```bash
sudo nano /etc/systemd/system/structured-governor.service
```

Paste (replace `YOUR_VPS_USERNAME` and the API key):

```ini
[Unit]
Description=Structured Governor API for n8n
After=network.target

[Service]
Type=simple
User=YOUR_VPS_USERNAME
WorkingDirectory=/opt/structured-governor
Environment="PATH=/opt/structured-governor/venv/bin"
Environment="OPENAI_API_KEY=sk-your-actual-key-here"
Environment="PORT=5050"
ExecStart=/opt/structured-governor/venv/bin/python structured_governor.py --serve
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable structured-governor
sudo systemctl start structured-governor
sudo systemctl status structured-governor
```

### 7. Point n8n at the governor

In n8n, open the **Call Structured Governor** node and set URL to:

`http://localhost:5050/govern`

Save and activate the workflow. No need to open port 5050 in the firewall; only n8n (on the same server) needs to reach it.
