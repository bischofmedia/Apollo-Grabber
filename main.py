import requests
import hashlib
import os
import json
import re
import math
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime
from zoneinfo import ZoneInfo

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
WEBHOOK = os.getenv("MAKE_WEBHOOK")

STATE_FILE = "state.json"
APOLLO_BOT_ID = "475744554910351370"
DRIVERS_PER_GRID = 15  # Festwert basierend auf Gran Turismo

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
    Sammelt alle Fahrer aus den Fields und berechnet die Grids basierend auf 15er Schritten.
    """
    fields = embed.get("fields", [])
    all_drivers = []
    full_text_for_hash = ""
    
    # Wir loopen durch alle Fields (Field[0] ist oft Beschreibung, Rest sind Fahrerlisten)
    for field in fields:
        name = field.get("name", "")
        value = field.get("value", "")
        
        # Alles für den Hash sammeln, um jede Änderung (auch Status-Wechsel) zu bemerken
        full_text_for_hash += f"{name}{value}"
        
        # Nur Fields verarbeiten, die tatsächlich Fahrerlisten enthalten 
        # (Apollo nutzt oft Emojis wie :white_check_mark: oder Namen wie "Accepted")
        if any(keyword in name for keyword in ["Accepted", "Anmeldung", "Teilnehmer", "Confirmed"]):
            lines = [l.strip() for l in value.split("\n") if l.strip()]
            for line in lines:
                # Entferne Discord-Erwähnungen <@...>, Sternchen, etc.
                clean_name = re.sub(r"[*<>@!]", "", line)
                # Entferne führende Nummern oder Punkte (z.B. "1. Name" -> "Name")
                clean_name = re.sub(r"^\d+\.\s*", "", clean_name)
                
                if clean_name:
                    all_drivers.append(clean_name)

    # Berechnung der Grids: Immer aufgerundet auf Basis von 15
    driver_count = len(all_drivers)
    grids_needed = math.ceil(driver_count / DRIVERS_PER_GRID) if driver_count > 0 else 0

    return all_drivers, grids_needed, full_text_for_hash

# ---------- Business Logic ----------

def grid_locked():
    berlin = ZoneInfo("Europe/Berlin")
    now = datetime.now(berlin)
    wd = now.weekday()  # Mon=0, Sun=6
    
    # Sperrzeiten: Sonntag ab 18 Uhr, ganzer Montag, Dienstag bis 10 Uhr
    if (wd == 6 and now.hour >= 18) or (wd == 0) or (wd == 1 and now.hour < 10):
        return True
    return False

def send_webhook(payload):
    try:
        requests.post(WEBHOOK, json=payload, timeout=5)
        print(f"Webhook gesendet: {payload['type']} ({payload['driver_count']} Fahrer, {payload['grids']} Grids)")
    except Exception as e:
        print(f"Webhook Fehler: {e}")

# ---------- Core Check ----------

def run_check():
    state = load_state()
    try:
        messages = fetch_messages()
    except Exception as e:
        print(f"Fehler beim Abrufen der Nachrichten: {e}")
        return {"status": "error"}
    
    apollo_msg = None
    for msg in messages:
        if msg.get("author", {}).get("id") == APOLLO_BOT_ID and msg.get("embeds"):
            apollo_msg = msg
            break
            
    if not apollo_msg:
        return {"status": "no_event