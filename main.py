import requests
import hashlib
import os
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
WEBHOOK = os.getenv("MAKE_WEBHOOK")

STATE_FILE = "state.json"


# ---------- Helpers ----------

def normalize(text):
    lines = [l.strip() for l in text.split("\n")]
    lines = [l for l in lines if l and "Grid" not in l]
    return "\n".join(lines)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"event_id": None, "hash": None}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def hash_text(text):
    return hashlib.sha256(text.encode()).hexdigest()


def is_apollo(msg):
    if not msg.get("embeds"):
        return False

    text = msg["embeds"][0].get("description", "")
    keywords = ["Grid", "Driver", "Anmeldung"]

    return any(k in text for k in keywords)


def fetch_messages():
    url = f"https://discord.com/api/v10/channels/{CHANNEL_ID}/messages?limit=20"
    headers = {"Authorization": f"Bot {TOKEN}"}

    r = requests.get(url, headers=headers)
    r.raise_for_status()
    return r.json()


def find_apollo():
    for msg in fetch_messages():
        if is_apollo(msg):
            desc = msg["embeds"][0].get("description", "")
            return msg["id"], desc

    return None, None


def send_webhook(payload):
    try:
        requests.post(WEBHOOK, json=payload, timeout=5)
        print("Webhook sent:", payload["type"])
    except Exception as e:
        print("Webhook error:", e)


# ---------- Core Poll Logic ----------

def run_check():

    state = load_state()

    event_id, raw_text = find_apollo()

    if not event_id:
        return {"status": "no_event"}

    normalized = normalize(raw_text)
    new_hash = hash_text(normalized)

    # New event
    if state["event_id"] != event_id:

        send_webhook({
            "type": "event_reset",
            "event_id": event_id,
            "apollo": normalized
        })

        save_state({
            "event_id": event_id,
            "hash": new_hash
        })

        return {"status": "event_reset"}

    # Roster change
    if state["hash"] != new_hash:

        send_webhook({
            "type": "roster_update",
            "event_id": event_id,
            "apollo": normalized
        })

        state["hash"] = new_hash
        save_state(state)

        return {"status": "roster_update"}

    return {"status": "no_change"}


# ---------- HTTP Server ----------

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):

        result = run_check()

        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()

        self.wfile.write(json.dumps(result).encode())


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    print(f"Server running on port {port}")
    HTTPServer(("", port), Handler).serve_forever()