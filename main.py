import requests
import hashlib
import os
import json
import re
import math
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime
from zoneinfo import ZoneInfo

# ---------- Konfiguration (Umgebungsvariablen) ----------
TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
WEBHOOK = os.getenv("MAKE_WEBHOOK")

DRIVERS_PER_GRID = int(os.getenv("DRIVERS_PER_GRID", 15))
MAX_GRIDS = int(os.getenv("MAX_GRIDS", 4))

STATE_FILE = "state.json"
APOLLO_BOT_ID = "475744554910351370"

# ---------- Helpers ----------

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {"event_id": None, "hash": None, "drivers": []}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def hash_text(text):
    return hashlib.sha256(text.encode()).hexdigest()

def get_roster_changes(old_drivers, new_drivers):
    added = list(set(new_drivers) - set(old_drivers))
    removed = list(set(old_drivers) - set(new_drivers))
    return added, removed

# ---------- Apollo Logic ----------

def fetch_messages():
    url = f"https://discord.com/api/v10/channels/{CHANNEL_ID}/messages?limit=10"
    headers = {"Authorization": f"Bot {TOKEN}"}
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    return r.json()

def extract_data_from_embed(embed):
    fields = embed.get("fields", [])
    all_drivers = []
    full_text_for_hash = ""
    
    for field in fields:
        name = field.get("name", "")
        value = field.get("value", "")
        full_text_for_hash += f"{name}{value}"
        
        if any(keyword in name for keyword in ["Accepted", "Anmeldung", "Teilnehmer", "Confirmed", "Zusagen"]):
            lines = [l.strip() for l in value.split("\n") if l.strip()]
            for line in lines:
                clean_name = re.sub(r"[*<>@!]", "", line)
                clean_name = re.sub(r"^\d+[\s.)-]*", "", clean_name)
                if clean_name and "Grid" not in clean_name:
                    all_drivers.append(clean_name)

    driver_count = len(all_drivers)
    if driver_count == 0:
        grids_needed = 1
    else:
        grids_needed = math.ceil(driver_count / DRIVERS_PER_GRID)
        if grids_needed > MAX_GRIDS:
            grids_needed = MAX_GRIDS

    return all_drivers, grids_needed, full_text_for_hash

# ---------- Business Logic ----------

def grid_locked():
    berlin = ZoneInfo("Europe/Berlin")
    now = datetime.now(berlin)
    wd = now.weekday() 
    if (wd == 6 and now.hour >= 18) or (wd == 0) or (wd == 1 and now.hour < 10):
        return True
    return False

def send_webhook(payload):
    try:
        requests.post(WEBHOOK, json=payload, timeout=5)
        print(f"Webhook gesendet: {payload['type']}")
    except Exception as e:
        print(f"Webhook Fehler: {e}")

# ---------- Core Check ----------

def run_check():
    state = load_state()
    try:
        messages = fetch_messages()
    except Exception as e:
        print(f"Discord API Fehler: {e}")
        return {"status": "error"}
    
    apollo_msg = None
    for msg in messages:
        if msg.get("author", {}).get("id") == APOLLO_BOT_ID and msg.get("embeds"):
            apollo_msg = msg
            break
            
    if not apollo_msg:
        return {"status": "no_event"}

    event_id = apollo_msg["id"]
    embed = apollo_msg["embeds"][0]
    
    drivers, grids, raw_content = extract_data_from_embed(embed)
    new_hash = hash_text(raw_content)
    
    berlin = ZoneInfo("Europe/Berlin")
    payload_base = {
        "event_id": event_id,
        "drivers": drivers,
        "driver_count": len(drivers),
        "grids": grids,
        "grid_locked": grid_locked(),
        "timestamp": datetime.now(berlin).isoformat()
    }

    if state["event_id"] != event_id:
        p_type = "event_reset_with_roster" if drivers else "event_reset"
        payload = {"type": p_type, **payload_base}
        send_webhook(payload)
        save_state({"event_id": event_id, "hash": new_hash, "drivers": drivers})
        return {"status": "new_event_detected"}

    if state["hash"] != new_hash:
        added, removed = get_roster_changes(state.get("drivers", []), drivers)
        payload = {
            "type": "roster_update", 
            "added_drivers": added,
            "removed_drivers": removed,
            **payload_base
        }
        send_webhook(payload)
        save_state({"event_id": event_id, "hash": new_hash, "drivers": drivers})
        return {"status": "roster_updated"}

    return {"status": "no_change"}

# ---------- Server ----------

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        result = run_check()
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    print(f"Apollo Grabber V2 aktiv (Grids: 1-{MAX_GRIDS}, Drivers/Grid: {DRIVERS_PER_GRID})")
    server = HTTPServer(("", port), Handler)
    server.serve_forever()