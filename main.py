import os, requests, json, re, math, datetime, pytz, random
from flask import Flask

# --- KONFIGURATION ---
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
MAKE_WEBHOOK_URL = os.environ.get("MAKE_WEBHOOK_URL")

# Schalter (0/1)
DELETE_OLD_EVENT = os.environ.get("DELETE_OLD_EVENT", "0") == "1"
ENABLE_SUNDAY_MSG = os.environ.get("ENABLE_SUNDAY_MSG", "1") == "1"
ENABLE_GRID_FULL_MSG = os.environ.get("ENABLE_GRID_FULL_MSG", "1") == "1"
ENABLE_WAITLIST_MSG = os.environ.get("ENABLE_WAITLIST_MSG", "0") == "1"
ENABLE_EXTRA_GRID = os.environ.get("ENABLE_EXTRA_GRID", "0") == "1"

# Texte & Werte
GRID_FULL_TEXT = os.environ.get("GRID_FULL_TEXT", "Grid {full_grids} ist voll!")
SUNDAY_MSG_TEXT = os.environ.get("SUNDAY_MSG_TEXT", "Sonntag 18 Uhr: {driver_count} Fahrer.")
WAITLIST_TEXT_SINGLE = os.environ.get("WAITLIST_TEXT_SINGLE", "Warteliste: {driver_names} ist neu dabei.")
WAITLIST_TEXT_MULTI = os.environ.get("WAITLIST_TEXT_MULTI", "Warteliste: {driver_names} sind neu dabei.")
EXTRA_GRID_TEXT = os.environ.get("EXTRA_GRID_TEXT", "Zusatzgrid eröffnet!")
EXTRA_GRID_THRESHOLD = int(os.environ.get("EXTRA_GRID_THRESHOLD", 10))
MIN_GRIDS_FOR_MESSAGE = int(os.environ.get("MIN_GRIDS_FOR_MESSAGE", 1))

APOLLO_BOT_ID = "475744554910351370"
DRIVERS_PER_GRID = 15
MAX_GRIDS = 4
STATE_FILE = "state.json"
BERLIN_TZ = pytz.timezone("Europe/Berlin")

app = Flask(__name__)

def get_now(): return datetime.datetime.now(BERLIN_TZ)
def get_log_timestamp():
    days = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    now = get_now()
    return f"{days[now.weekday()]} {now.strftime('%H:%M')}"

def grid_locked():
    now = get_now()
    wd = now.weekday()
    return (wd == 6 and now.hour >= 18) or (wd == 0) or (wd == 1 and now.hour < 10)

def clean_log_name(name):
    return name.replace("\\_", "_").replace("\\*", "*").replace("*", "").strip()

def delete_discord_message(msg_id):
    url = f"https://discord.com/api/v10/channels/{CHANNEL_ID}/messages/{msg_id}"
    requests.delete(url, headers={"Authorization": f"Bot {DISCORD_TOKEN}"})

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                d = json.load(f)
                d.setdefault("sent_grids", [])
                d.setdefault("last_sunday_msg_event", None)
                d.setdefault("extra_grid_active", False)
                return d
        except: pass
    return {"event_id": None, "hash": None, "drivers": [], "grids": 1, "log": "", "sent_grids": [], "last_sunday_msg_event": None, "extra_grid_active": False}

def save_state(state):
    with open(STATE_FILE, "w") as f: json.dump(state, f)

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
                clean_name = line.replace(">>>", "").replace(">", "")
                clean_name = re.sub(r"^\d+[\s.)-]*", "", clean_name).strip()
                if clean_name and "Grid" not in clean_name and len(clean_name) > 1:
                    all_drivers.append(clean_name)
    return all_drivers, full_text_for_hash

def run_check():
    if not all([DISCORD_TOKEN, CHANNEL_ID, MAKE_WEBHOOK_URL]): return "Missing Config"
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}"}
    url = f"https://discord.com/api/v10/channels/{CHANNEL_ID}/messages?limit=10"
    
    try:
        response = requests.get(url, headers=headers)
        messages = response.json()
        apollo_msg = next((m for m in messages if m.get("author", {}).get("id") == APOLLO_BOT_ID and m.get("embeds")), None)
        if not apollo_msg: return "No Apollo message"

        event_id = apollo_msg["id"]
        drivers, raw_content = extract_data_from_embed(apollo_msg["embeds"][0])
        current_hash = str(hash(raw_content))
        state = load_state()
        
        now_dt = get_now()
        ts = get_log_timestamp()
        is_new_event = (state.get("event_id") != event_id)
        
        grid_full_msg = sunday_msg = waitlist_msg = extra_grid_msg = None
        force_send = False

        if is_new_event:
            if DELETE_OLD_EVENT and state["event_id"]: delete_discord_message(state["event_id"])
            state.update({"sent_grids": [], "last_sunday_msg_event": None, "extra_grid_active": False, "log": f"{ts} 📅 Apollo gestartet"})
            msg_type, force_send = "event_reset", True
        else:
            msg_type = "status_trigger"

        driver_count = len(drivers)
        current_max_grids = 5 if state["extra_grid_active"] else MAX_GRIDS
        grids = min(current_max_grids, max(1, math.ceil(driver_count / DRIVERS_PER_GRID)))
        waitlist_count = max(0, driver_count - (MAX_GRIDS * DRIVERS_PER_GRID))

        # Extra Grid Check
        if ENABLE_EXTRA_GRID and not state["extra_grid_active"] and grid_locked():
            if waitlist_count >= EXTRA_GRID_THRESHOLD:
                state["extra_grid_active"] = True
                extra_grid_msg = EXTRA_GRID_TEXT.format(waitlist_count=waitlist_count)
                grids = 5
                force_send = True

        # Grid Voll Check
        if ENABLE_GRID_FULL_MSG and driver_count > 0 and driver_count % DRIVERS_PER_GRID == 0:
            full_grids_count = driver_count // DRIVERS_PER_GRID
            if full_grids_count >= MIN_GRIDS_FOR_MESSAGE and full_grids_count not in state["sent_grids"]:
                options = [opt.strip() for opt in GRID_FULL_TEXT.split(";")]
                grid_full_msg = random.choice(options).format(driver_count=driver_count, full_grids=full_grids_count)
                state["sent_grids"].append(full_grids_count)
                force_send = True

        # Sonntag 18 Uhr
        if ENABLE_SUNDAY_MSG and now_dt.weekday() == 6 and now_dt.hour == 18 and now_dt.minute < 10:
            if state["last_sunday_msg_event"] != event_id:
                options = [opt.strip() for opt in SUNDAY_MSG_TEXT.split(";")]
                free = max(0, (current_max_grids * DRIVERS_PER_GRID) - driver_count)
                sunday_msg = random.choice(options).format(driver_count=driver_count, grids=grids, free_slots=free)
                state["last_sunday_msg_event"] = event_id
                force_send = True

        old_drivers = state.get("drivers", [])
        added = [d for d in drivers if d not in old_drivers]
        removed = [d for d in old_drivers if d not in drivers]

        if added or removed:
            msg_type, force_send = "roster_update", True
            new_entries = [f"{ts} 🟢 {clean_log_name(d)}" for d in added] + [f"{ts} 🔴 {clean_log_name(d)}" for d in removed]
            state["log"] = (state["log"] + "\n" + "\n".join(new_entries)).strip()
            
            # Wartelisten-Meldung mit Singular/Plural Check
            if ENABLE_WAITLIST_MSG and added and grid_locked() and driver_count > (current_max_grids * DRIVERS_PER_GRID):
                names_str = ", ".join([clean_log_name(d) for d in added])
                if len(added) == 1:
                    waitlist_msg = WAITLIST_TEXT_SINGLE.format(driver_names=names_str)
                else:
                    waitlist_msg = WAITLIST_TEXT_MULTI.format(driver_names=names_str)

        if not force_send and state.get("hash") == current_hash: return "No change"

        state.update({"event_id": event_id, "hash": current_hash, "drivers": drivers, "grids": grids})
        save_state(state)
        
        payload = {
            "type": msg_type, "drivers": drivers, "grids": grids, "grid_locked": grid_locked(),
            "log": state["log"], "grid_full_msg": grid_full_msg, "sunday_msg": sunday_msg,
            "waitlist_msg": waitlist_msg, "extra_grid_msg": extra_grid_msg, "timestamp": now_dt.isoformat()
        }
        requests.post(MAKE_WEBHOOK_URL, json=payload)
        return f"Success: {msg_type} sent"
    except Exception as e: return f"Error: {str(e)}"

@app.route('/')
def home(): return run_check()
if __name__ == "__main__": app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))