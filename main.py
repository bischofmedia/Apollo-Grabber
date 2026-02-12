import requests
import hashlib
import os
import json
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime
from zoneinfo import ZoneInfo

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
WEBHOOK = os.getenv("MAKE_WEBHOOK")

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
    return {"event_id": None, "hash": None}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def hash_text(text):
    return hashlib.sha256(text.encode()).hexdigest()

# ---------- Apollo Logic ----------

def fetch_messages():
    url = f"https://discord.com/api/v10/channels/{CHANNEL_ID}/messages?limit=10"
    headers = {"Authorization": f"Bot {TOKEN}"}
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    return r.json()

def extract_data_from_embed(embed):
    """
    Extrahiert Fahrer aus Fields und sucht nach Grid-Limit im gesamten Text.
    """
    fields = embed.get("fields", [])
    all_drivers = []
    full_text_for_hash = ""
    
    # 1. Fahrer-Extraktion aus den Fields (ab Field[1] laut deiner Erfahrung)
    # Wir nehmen zur Sicherheit alle Fields, die Namen im Discord-Format (@...) enthalten
    for field in fields:
        value = field.get("value", "")
        full_text_for_hash += f"{field.get('name', '')}{value}"
        
        # Extrahiere Namen: Apollo listet Fahrer oft mit Zeilenumbrüchen auf
        # Wir filtern Zeilen, die wie User-Erwähnungen oder Namen aussehen
        lines = [l.strip() for l in value.split("\n") if l.strip()]
        for line in lines:
            # Einfache Reinigung von Discord-Markdown (Fett, Kursiv, Mentions)
            clean_name = re.sub(r"[*<>@!]", "", line)
            if clean_name and "Grid" not in clean_name:
                all_drivers.append(clean_name)

    # 2. Grid-Limit Suche (z.B. "0/22" oder "Limit: 22")
    # Wir scannen Titel, Description und alle Field-Namen
    search_area = f"{embed.get('title', '')} {embed.get('description', '')} "
    search_area += " ".join([f.get("name", "") for f in fields])
    
    grid_match = re.search(r"/(\d{2})", search_area) # Sucht nach /22, /20 etc.
    max_grid = int(grid_match.group(1)) if grid_match else 15

    return all_drivers, max_grid, full_text_for_hash

# ---------- Business Logic ----------

def grid_locked():
    berlin = ZoneInfo("Europe/Berlin")
    now = datetime.now(berlin)
    wd = now.weekday()  # Mon=0, Sun=6
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
    messages = fetch_messages()
    
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

    # Fall 1: Neues Event entdeckt
    if state["event_id"] != event_id:
        p_type = "event_reset_with_roster" if drivers else "event_reset"
        payload = {"type": p_type, **payload_base}
        send_webhook(payload)
        save_state({"event_id": event_id, "hash": new_hash})
        return {"status": "new_event_detected"}

    # Fall 2: Roster-Update (Hash hat sich geändert)
    if state["hash"] != new_hash:
        payload = {"type": "roster_update", **payload_base}
        send_webhook(payload)
        state["hash"] = new_hash
        save_state(state)
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
    print(f"Server läuft auf Port {port}...")
    HTTPServer(("", port), Handler).serve_forever()