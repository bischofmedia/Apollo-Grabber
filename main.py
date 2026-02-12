import requests
import hashlib
import os
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime
from zoneinfo import ZoneInfo

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


# ---------- Apollo detection ----------

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


# ---------- Business logic ----------

def parse_drivers(text):
    return [l for l in text.split("\n") if l.strip()]


def calc_grids(count):
    import math
    return math.ceil(count / 15)


def grid_locked():
    berlin = ZoneInfo("Europe/Berlin")
    now = datetime.now(berlin)

    wd = now.weekday()  # Mon=0

    if wd == 6 and now.hour >= 18:  # Sunday
        return True

    if wd == 0:  # Monday
        return True

    if wd == 1 and now.hour < 10:  # Tuesday
        return True

    return False


def send_webhook(payload):
    try:
        requests.post(WEBHOOK, json=payload, timeout=5)
        print("Webhook:", payload["type"])
    except Exception as e:
        print("Webhook error:", e)


# ---------- Core check ----------

def run_check():

    state = load_state()

    event_id, raw_text = find_apollo()

    if not event_id:
        return {"status": "no_event"}

    normalized = normalize(raw_text)
    new_hash = hash_text(normalized)

    drivers = parse_drivers(normalized)
    driver_count = len(drivers)
    grids = calc_grids(driver_count)

    berlin = ZoneInfo("Europe/Berlin")
    timestamp = datetime.now(berlin).isoformat()

    base_payload = {
        "event_id": event_id,
        "drivers": drivers,
        "driver_count": driver_count,
        "grids": grids,
        "grid_locked": grid_locked(),
        "timestamp": timestamp
    }

    # New event
    if state["event_id"] != event_id:

        payload = {"type": "event_reset", **base_payload}
        send_webhook(payload)

        save_state({"event_id": event_id, "hash": new_hash})
        return {"status": "event_reset"}

    # Roster change
    if state["hash"] != new_hash:

        payload = {"type": "roster_update", **base_payload}
        send_webhook(payload)

        state["hash"] = new_hash
        save_state(state)

        return {"status": "roster_update"}

    return {"status": "no_change"}


# ---------- HTTP server ----------

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):

        result = run_check()

        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()

        self.wfile.write(json.dumps(result).encode())


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    print("Server running…")
    HTTPServer(("", port), Handler).serve_forever()