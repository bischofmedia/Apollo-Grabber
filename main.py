import os
import requests
import json
import re
import math
import datetime
import pytz
import random
from flask import Flask

# --- KONFIGURATION ---
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
MAKE_WEBHOOK_URL = os.environ.get("MAKE_WEBHOOK_URL")

# Texte und Schwellenwerte aus Umgebungsvariablen
GRID_FULL_TEXT = os.environ.get("GRID_FULL_TEXT", "Schon {driver_count} Anmeldungen, {full_grids} Grids sind voll!")
SUNDAY_MSG_TEXT = os.environ.get("SUNDAY_MSG_TEXT", "Sonntag 18 Uhr: {driver_count} Fahrer, {grids} Grids. {free_slots} Plätze frei.")
MIN_GRIDS_FOR_MESSAGE = int(os.environ.get("MIN_GRIDS_FOR_MESSAGE", 1))

APOLLO_BOT_ID = "475744554910351370"
DRIVERS_PER_GRID = 15
MAX_GRIDS = 4
STATE_FILE = "state.json"
BERLIN_TZ = pytz.timezone("Europe/Berlin")

app = Flask(__name__)

def get_now():
    return datetime.datetime.now(BERLIN_TZ)

def get_log_timestamp():
    days = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    now = get_now()
    return f"{days[now.weekday()]} {now.strftime('%H:%M')}"

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
        driver_count = len(drivers)
        grid_full_msg = None
        sunday_msg = None

        # 1. Grid Voll Check mit Schwellenwert
        if driver_count > 0 and driver_count % DRIVERS_PER_GRID == 0:
            full_grids = driver_count // DRIVERS_PER_GRID
            # Nur senden, wenn die Mindestanzahl an Grids erreicht ist
            if full_grids >= MIN_GRIDS_FOR_MESSAGE:
                options = [opt.strip() for opt in GRID_FULL_TEXT.split(";")]
                grid_full_msg = random.choice(options).format(
                    driver_count=driver_count, 
                    full_grids=full_grids
                )

        # 2. Sonntag 18 Uhr Check
        if now_dt.weekday() == 6 and now_dt.hour == 18 and now_dt.minute < 10:
            options = [opt.strip() for opt in SUNDAY_MSG_TEXT.split(";")]
            free = max(0, (MAX_GRIDS * DRIVERS_PER_GRID) - driver_count)
            sunday_msg = random.choice(options).format(
                driver_count=driver_count, 
                grids=grids, 
                free_slots=free
            )

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