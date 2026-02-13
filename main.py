import os
import requests
import json
import re
import math
import datetime
import pytz
from flask import Flask

# --- KONFIGURATION ---
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
MAKE_WEBHOOK_URL = os.environ.get("MAKE_WEBHOOK_URL")

APOLLO_BOT_ID = "475744554910351370"
DRIVERS_PER_GRID = 15
MAX_GRIDS = 4
STATE_FILE = "state.json"
BERLIN_TZ = pytz.timezone("Europe/Berlin")

app = Flask(__name__)

def get_now():
    """Hilfsfunktion für aktuelle Zeit in Berlin."""
    return datetime.datetime.now(BERLIN_TZ)

def get_log_timestamp():
    days = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    now = get_now()
    day_str = days[now.weekday()]
    return now.strftime(f"{day_str} %H:%M")

def grid_locked():
    now = get_now()
    wd = now.weekday()
    if (wd == 6 and now.hour >= 18) or (wd == 0) or (wd == 1 and now.hour < 10):
        return True
    return False

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                if "log" not in data: data["log"] = ""
                return data
        except: pass
    return {"event_id": None, "hash": None, "drivers": [], "grids": 1, "log": ""}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def extract_data_from_embed(embed):
    fields = embed.get("fields", [])
    all_drivers = []
    full_text_for_hash = ""
    for field in fields:
        name, value = field.get("name", ""), field.get("value", "")
        full_text_for_hash += f"{name}{value}"
        if any(kw in name for kw in ["Accepted", "Anmeldung", "Teilnehmer", "Confirmed", "Zusagen"]):
            lines = [l.strip() for l in value.split("\n") if l.strip()]
            for line in lines:
                clean_name = re.sub(r"[*_<>@!]", "", line)
                clean_name = re.sub(r"^\d+[\s.)-]*", "", clean_name).strip()
                if clean_name and "Grid" not in clean_name and len(clean_name) > 1:
                    all_drivers.append(clean_name)
    grids = max(1, math.ceil(len(all_drivers) / DRIVERS_PER_GRID))
    return all_drivers, min(grids, MAX_GRIDS), full_text_for_hash

def run_check():
    if not all([DISCORD_TOKEN, CHANNEL_ID, MAKE_WEBHOOK_URL]):
        return "Error: Missing Environment Variables"
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}"}
    url = f"https://discord.com/api/v10/channels/{CHANNEL_ID}/messages?limit=10"
    
    try:
        response = requests.get(url, headers=headers)
        messages = response.json()
        apollo_msg = next((m for m in messages if m.get("author", {}).get("id") == APOLLO_BOT_ID and m.get("embeds")), None)
        
        if not apollo_msg: return "No Apollo message found"

        event_id, embed = apollo_msg["id"], apollo_msg["embeds"][0]
        drivers, grids, raw_content = extract_data_from_embed(embed)
        current_hash = str(hash(raw_content))
        state = load_state()
        is_new_event = (state.get("event_id") != event_id)
        
        now_dt = get_now()
        ts = get_log_timestamp()
        grid_full_msg = None
        sunday_msg = None

        # Logik für Meldungen
        driver_count = len(drivers)
        if driver_count > 0 and driver_count % 15 == 0:
            grid_full_msg = f"Schon {driver_count} Anmeldungen, {driver_count // 15} Grids sind voll!"

        if now_dt.weekday() == 6 and now_dt.hour == 18 and now_dt.minute < 10:
            free = (MAX_GRIDS * DRIVERS_PER_GRID) - driver_count
            sunday_msg = f"Es ist Sonntag 18 Uhr. Wir haben {driver_count} Anmeldungen und damit {grids} Grids. Es sind noch {max(0, free)} Plätze frei."

        if is_new_event:
            start_log = f"📅 {ts} Event gestartet"
            if drivers:
                initial = [f"🟢 {ts} {d} angemeldet" for d in drivers]
                state["log"] = start_log + "\n" + "\n".join(initial)
            else:
                state["log"] = start_log
            msg_type = "event_reset"
        else:
            old_drivers = state.get("drivers", [])
            added = [d for d in drivers if d not in old_drivers]
            removed = [d for d in old_drivers if d not in drivers]
            if added or removed:
                new_entries = []
                for d in added: new_entries.append(f"🟢 {ts} {d} angemeldet")
                for d in removed: new_entries.append(f"🔴 {ts} {d} abgemeldet")
                state["log"] = (state.get("log", "") + "\n" + "\n".join(new_entries)).strip()
                msg_type = "roster_update"
            elif state.get("hash") != current_hash:
                msg_type = "roster_update"
            else: return "No change detected"

        state.update({"event_id": event_id, "hash": current_hash, "drivers": drivers, "grids": grids})
        save_state(state)
        
        payload = {
            "type": msg_type, "drivers": drivers, "grids": grids, 
            "grid_locked": grid_locked(), "log": state["log"], 
            "grid_full_msg": grid_full_msg, "sunday_msg": sunday_msg,
            "timestamp": now_dt.isoformat()
        }
        requests.post(MAKE_WEBHOOK_URL, json=payload)
        return f"Success: {msg_type} sent"
    except Exception as e: return f"Error: {str(e)}"

@app.route('/')
def home(): return run_check()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))