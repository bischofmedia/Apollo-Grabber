import os, requests, json, re, math, datetime, pytz, time
from flask import Flask, request

# --- KONFIGURATION ---
def get_env_config():
    return {
        "TOKEN_APOLLO": os.environ.get("DISCORD_TOKEN_APOLLOGRABBER"),
        "TOKEN_LOBBY": os.environ.get("DISCORD_TOKEN_LOBBYCODEGRABBER"),
        "CHAN_APOLLO": os.environ.get("CHAN_APOLLO"),
        "CHAN_LOG": os.environ.get("CHAN_LOG"),
        "CHAN_CODES": os.environ.get("CHAN_CODES"),
        "MSG_LOBBY": os.environ.get("MSG_LOBBYCODES", "Willkommen im neuen Event!"),
        "MAKE_WEBHOOK_URL": os.environ.get("MAKE_WEBHOOK_URL")
    }

SET_DELETE_OLD_EVENT = os.environ.get("SET_DELETE_OLD_EVENT", "0") == "1"
SET_EXTRA_GRID_THRESHOLD = int(os.environ.get("SET_EXTRA_GRID_THRESHOLD", 10))
DRIVERS_PER_GRID = int(os.environ.get("DRIVERS_PER_GRID", 15))
MAX_GRIDS = int(os.environ.get("MAX_GRIDS", 4))
REG_END_TIME = os.environ.get("REGISTRATION_END_TIME", "").strip()
SET_MANUAL_LOG_ID = os.environ.get("SET_MANUAL_LOG_ID", "").strip()

APOLLO_BOT_ID = "475744554910351370"
STATE_FILE = "state.json"
LOG_FILE = "event_log.txt"
BERLIN_TZ = pytz.timezone("Europe/Berlin")

app = Flask(__name__)

# --- HELFER ---
def get_now(): return datetime.datetime.now(BERLIN_TZ)

def format_ts_short(dt_obj):
    days = {"Mon":"Mo", "Tue":"Di", "Wed":"Mi", "Thu":"Do", "Fri":"Fr", "Sat":"Sa", "Sun":"So"}
    raw = dt_obj.strftime("%a %H:%M")
    for en, de in days.items(): raw = raw.replace(en, de)
    return raw

def clean_for_log(n):
    return n.replace("\\", "").replace(">>>", "").replace(">", "").strip()

def raw_for_make(n):
    return n.replace(">>>", "").replace(">", "").strip()

def write_to_persistent_log(line):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def read_persistent_log():
    if not os.path.exists(LOG_FILE): return []
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f.readlines()]

def lobby_cleanup(config):
    if not config["TOKEN_LOBBY"] or not config["CHAN_CODES"]: return
    headers = {"Authorization": f"Bot {config['TOKEN_LOBBY']}"}
    url = f"https://discord.com/api/v10/channels/{config['CHAN_CODES']}/messages"
    res = requests.get(f"{url}?limit=100", headers=headers)
    if res.status_code == 200:
        for m in res.json():
            requests.delete(f"{url}/{m['id']}", headers=headers)
            time.sleep(0.4)
    requests.post(url, headers=headers, json={"content": config["MSG_LOBBY"]})

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f: return json.load(f)
        except: pass
    return {"event_id": None, "drivers": [], "log_msg_id": None, "last_make_sync": None}

def save_state(s):
    with open(STATE_FILE, "w") as f: json.dump(s, f)

def extract_data(embed):
    title = embed.get("title", "Unbekanntes Event")
    drivers = []
    for field in embed.get("fields", []):
        if any(kw in field.get("name", "") for kw in ["Accepted", "Anmeldung", "Teilnehmer", "Confirmed", "Zusagen"]):
            for line in field.get("value", "").split("\n"):
                c = re.sub(r"^\d+[\s.)-]*", "", line).strip()
                if c and "Grid" not in c and len(c) > 1: drivers.append(c)
    return title, drivers

# --- MAIN ---
@app.route('/')
def home():
    config = get_env_config()
    if not all([config["TOKEN_APOLLO"], config["CHAN_APOLLO"], config["CHAN_LOG"]]):
        return "Config Error", 500

    try:
        headers = {"Authorization": f"Bot {config['TOKEN_APOLLO']}"}
        res = requests.get(f"https://discord.com/api/v10/channels/{config['CHAN_APOLLO']}/messages?limit=10", headers=headers)
        apollo_msg = next((m for m in res.json() if m.get("author", {}).get("id") == APOLLO_BOT_ID and m.get("embeds")), None)
        if not apollo_msg: return "Kein Apollo-Post."

        event_title, drivers = extract_data(apollo_msg["embeds"][0])
        state = load_state()
        now = get_now()
        now_iso = now.isoformat()
        wd = now.weekday()
        grid_cap = MAX_GRIDS * DRIVERS_PER_GRID
        
        is_locked = (wd == 6 and now.hour >= 18) or (wd == 0)
        if not is_locked and wd == 0 and REG_END_TIME:
            try:
                h, m = map(int, REG_END_TIME.split(":"))
                if now >= now.replace(hour=h, minute=m, second=0, microsecond=0): is_locked = True
            except: pass
        if wd == 1 and now.hour < 10: is_locked = True

        is_new = (state.get("event_id") and state["event_id"] != apollo_msg["id"])
        webhook_type = "update"

        # LOGIC FÜR PERSISTENZ
        log_exists = os.path.exists(LOG_FILE)
        
        if is_new or not log_exists:
            if is_new:
                if log_exists: os.remove(LOG_FILE)
                webhook_type = "event_reset"
                start_line = f"{format_ts_short(now)} ✨ Event gestartet ({event_title})"
                lobby_cleanup(config)
            else:
                start_line = f"{format_ts_short(now)} ⚡ Systemstart ({event_title})"
            
            write_to_persistent_log(start_line)
            # Nur bei echtem NEUEN Event oder komplett fehlendem Log werden alle Fahrer geloggt
            for idx, d in enumerate(drivers):
                icon = "🟢" if idx < grid_cap else "🟡"
                write_to_persistent_log(f"{format_ts_short(now)} {icon} {clean_for_log(d)}{'' if idx < grid_cap else ' (Waitlist)'}")
            
            state.update({"event_id": apollo_msg["id"], "drivers": drivers})
            added, removed = [], []
        else:
            # Normaler Betrieb oder Systemstart mit existierender Datei
            old = state.get("drivers", [])
            added = [d for d in drivers if d not in old]
            removed = [d for d in old if d not in drivers]
            
            # Nur loggen, wenn sich wirklich etwas geändert hat (verhindert Log-Flut nach Deploy)
            for d in added:
                idx = drivers.index(d)
                icon = "🟢" if idx < grid_cap else "🟡"
                write_to_persistent_log(f"{format_ts_short(now)} {icon} {clean_for_log(d)}{'' if idx < grid_cap else ' (Waitlist)'}")
            for d in removed:
                write_to_persistent_log(f"{format_ts_short(now)} 🔴 {clean_for_log(d)}")

        driver_count = len(drivers)
        grid_count = min(math.ceil(driver_count/DRIVERS_PER_GRID), MAX_GRIDS)

        if config['MAKE_WEBHOOK_URL'] and (added or removed or is_new):
            if not is_locked or is_new:
                payload = {
                    "type": webhook_type,
                    "driver_count": driver_count,
                    "drivers": [raw_for_make(d) for d in drivers],
                    "grids": grid_count,
                    "log_history": "\n".join(read_persistent_log()),
                    "timestamp": now_iso
                }
                requests.post(config['MAKE_WEBHOOK_URL'], json=payload)
                state["last_make_sync"] = now_iso

        state["log_msg_id"] = send_or_edit_log(state, driver_count, grid_count, is_locked, config)
        state["drivers"] = drivers
        save_state(state)
        return "OK"

    except Exception as e: return f"Error: {str(e)}", 500

def send_or_edit_log(state, driver_count, grid_count, is_locked, config):
    headers = {"Authorization": f"Bot {config['TOKEN_APOLLO']}", "Content-Type": "application/json"}
    grid_cap = MAX_GRIDS * DRIVERS_PER_GRID
    icon = "🟡" if is_locked and driver_count >= grid_cap else ("🔴" if is_locked else "🟢")
    status = "Anmeldung geöffnet / Registration open" if not is_locked else ("Grids gesperrt & voll / Grids full" if driver_count >= grid_cap else "Grids gesperrt / Locked")
    
    full_log = read_persistent_log()
    log_text = ""
    # Von unten nach oben aufbauen bis Zeichenlimit
    for entry in reversed(full_log):
        if len(log_text) + len(entry) + 10 > 980:
            log_text = "...\n" + log_text
            break
        log_text = entry + "\n" + log_text
    
    sync_ts = format_ts_short(datetime.datetime.fromisoformat(state['last_make_sync']).astimezone(BERLIN_TZ)) if state.get('last_make_sync') else "--"
    legend = "🟢 Angemeldet / Registered\n🟡 Warteliste / Waitlist\n🔴 Abgemeldet / Withdrawn"

    formatted = (f"{icon} **{status}**\n"
                 f"Fahrer / Drivers: `{driver_count}` | Grids: `{grid_count}`\n\n"
                 f"```\n{log_text or 'Initialisiere...'}```\n"
                 f"*Stand: {format_ts_short(get_now())}*\n"
                 f"*Letzte Übertragung / Last Grid Sync: {sync_ts}*\n\n"
                 f"**Legende:**\n{legend}")

    tid = SET_MANUAL_LOG_ID or state.get("log_msg_id")
    if tid:
        requests.patch(f"https://discord.com/api/v10/channels/{config['CHAN_LOG']}/messages/{tid}", headers=headers, json={"content": formatted})
        return tid
    res = requests.post(f"https://discord.com/api/v10/channels/{config['CHAN_LOG']}/messages", headers=headers, json={"content": formatted})
    return res.json().get("id")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))