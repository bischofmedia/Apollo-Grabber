import os, requests, json, re, math, datetime, pytz, random
from flask import Flask

# --- KONFIGURATION ---
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
CHAN_APOLLO = os.environ.get("CHAN_APOLLO") 
CHAN_LOG = os.environ.get("CHAN_MAIN_LOG")    
CHAN_NEWS = os.environ.get("CHAN_NEWS_FLASH")  
MAKE_WEBHOOK_URL = os.environ.get("MAKE_WEBHOOK_URL")

# Schalter & Werte
DELETE_OLD_EVENT = os.environ.get("SET_DELETE_OLD_EVENT", "0") == "1"
EXTRA_GRID_THRESHOLD = int(os.environ.get("SET_EXTRA_GRID_THRESHOLD", 10))
MIN_GRIDS_FOR_MESSAGE = int(os.environ.get("SET_MIN_GRIDS_MSG", 1))
MANUAL_LOG_ID = os.environ.get("SET_MANUAL_LOG_ID", "").strip()

# Texte
GRID_FULL_TEXT = os.environ.get("MSG_GRID_FULL_TEXT", "Grid {full_grids} ist voll!")
SUNDAY_MSG_TEXT = os.environ.get("MSG_SUNDAY_TEXT", "Sonntag 18 Uhr: {driver_count} Fahrer.")
WAITLIST_SINGLE = os.environ.get("MSG_WAITLIST_SINGLE", "Warteliste: {driver_names} ist neu.")
WAITLIST_MULTI = os.environ.get("MSG_WAITLIST_MULTI", "Warteliste: {driver_names} sind neu.")
MOVED_UP_SINGLE = os.environ.get("MSG_MOVED_UP_SINGLE", "{driver_names} ist nachgerückt!")
MOVED_UP_MULTI = os.environ.get("MSG_MOVED_UP_MULTI", "{driver_names} sind nachgerückt!")
EXTRA_GRID_TEXT = os.environ.get("MSG_EXTRA_GRID_TEXT", "Zusatzgrid eröffnet!")

APOLLO_BOT_ID = "475744554910351370"
DRIVERS_PER_GRID = 15
STATE_FILE = "state.json"
BERLIN_TZ = pytz.timezone("Europe/Berlin")

app = Flask(__name__)

# --- DISCORD API ---
def discord_post(channel_id, content):
    if not content or not channel_id: return None
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"}
    try:
        res = requests.post(url, headers=headers, json={"content": content})
        return res.json().get("id") if res.status_code == 200 else None
    except: return None

def discord_delete(channel_id, msg_id):
    if not msg_id or not channel_id: return
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{msg_id}"
    try: requests.delete(url, headers={"Authorization": f"Bot {DISCORD_TOKEN}"})
    except: pass

def send_or_edit_log(content, current_log_id):
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"}
    formatted_content = f"**Logbuch der Anmeldungen:**\n```\n{content}\n```"
    target_id = MANUAL_LOG_ID if MANUAL_LOG_ID else current_log_id
    if target_id:
        url = f"https://discord.com/api/v10/channels/{CHAN_LOG}/messages/{target_id}"
        res = requests.patch(url, headers=headers, json={"content": formatted_content})
        if res.status_code == 200: return target_id
    return discord_post(CHAN_LOG, formatted_content)

# --- HELFER ---
def get_now(): return datetime.datetime.now(BERLIN_TZ)
def pick_text(env_value): return random.choice([opt.strip() for opt in env_value.split(";")])
def clean_name(n): return n.replace("\\_", "_").replace("\\*", "*").replace("*", "").strip()

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f: return json.load(f)
        except: pass
    return {"event_id": None, "hash": None, "drivers": [], "log": "", "sent_grids": [], "last_sunday_msg_event": None, "extra_grid_active": False, "log_msg_id": None}

def save_state(s):
    with open(STATE_FILE, "w") as f: json.dump(s, f)

def extract_data(embed):
    drivers = []
    raw = ""
    for field in embed.get("fields", []):
        name, val = field.get("name", ""), field.get("value", "")
        raw += f"{name}{val}"
        if any(kw in name for kw in ["Accepted", "Anmeldung", "Teilnehmer", "Confirmed", "Zusagen"]):
            for line in val.split("\n"):
                c = re.sub(r"^\d+[\s.)-]*", "", line.replace(">>>", "").replace(">", "")).strip()
                if c and "Grid" not in c and len(c) > 1: drivers.append(c)
    return drivers, raw

# --- MAIN ---
def run_check():
    if not all([DISCORD_TOKEN, CHAN_APOLLO, CHAN_LOG, CHAN_NEWS]): return "Config Error"
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}"}
    try:
        res = requests.get(f"https://discord.com/api/v10/channels/{CHAN_APOLLO}/messages?limit=10", headers=headers)
        messages = res.json()
        apollo_msg = next((m for m in messages if m.get("author", {}).get("id") == APOLLO_BOT_ID and m.get("embeds")), None)
        if not apollo_msg: return "Keine Apollo-Nachricht gefunden."

        event_id = apollo_msg["id"]
        drivers, raw_content = extract_data(apollo_msg["embeds"][0])
        current_hash = str(hash(raw_content))
        state = load_state()
        ts = f"{['So','Mo','Di','Mi','Do','Fr','Sa'][get_now().isoweekday() % 7]} {get_now().strftime('%H:%M')}"
        
        is_new = (state.get("event_id") and state["event_id"] != event_id)
        is_first_start = (state.get("event_id") is None)
        msg_type = "status_trigger"
        force_send = False

        if is_new or is_first_start:
            if is_new:
                if not MANUAL_LOG_ID: discord_delete(CHAN_LOG, state.get("log_msg_id"))
                if DELETE_OLD_EVENT: discord_delete(CHAN_APOLLO, state["event_id"])
                state.update({"event_id": event_id, "sent_grids": [], "extra_grid_active": False, "log": f"{ts} 📅 Start", "log_msg_id": None, "drivers": []})
                msg_type = "event_reset"
                force_send = True
            elif is_first_start:
                existing_entries = [f"{ts} ⚪ {clean_name(d)} (Bestand)" for d in drivers]
                state.update({"event_id": event_id, "sent_grids": [], "extra_grid_active": False, "log": f"{ts} 🔄 Systemstart\n" + "\n".join(existing_entries), "log_msg_id": None, "drivers": drivers})
                msg_type = "initial_load"
                force_send = True

        driver_count = len(drivers)
        grid_cap = 75 if state["extra_grid_active"] else 60
        wait_count = max(0, driver_count - 60)
        grid_full_msg = sunday_msg = waitlist_msg = moved_up_msg = extra_grid_msg = None

        if not state["extra_grid_active"] and (get_now().weekday() in [6,0,1]) and wait_count >= EXTRA_GRID_THRESHOLD:
            state["extra_grid_active"] = True
            extra_grid_msg = pick_text(EXTRA_GRID_TEXT).format(waitlist_count=wait_count)
            discord_post(CHAN_NEWS, extra_grid_msg)
            grid_cap = 75
            force_send = True

        if driver_count > 0 and driver_count % 15 == 0:
            full = driver_count // 15
            if full >= MIN_GRIDS_FOR_MESSAGE and full not in state["sent_grids"]:
                grid_full_msg = pick_text(GRID_FULL_TEXT).format(full_grids=full)
                discord_post(CHAN_NEWS, grid_full_msg)
                state["sent_grids"].append(full)
                force_send = True

        if get_now().weekday() == 6 and get_now().hour == 18 and get_now().minute < 10:
            if state.get("last_sunday_msg_event") != event_id:
                free = max(0, grid_cap - driver_count)
                sunday_msg = pick_text(SUNDAY_MSG_TEXT).format(driver_count=driver_count, free_slots=free)
                discord_post(CHAN_LOG, sunday_msg)
                state["last_sunday_msg_event"] = event_id
                force_send = True

        old = state.get("drivers", [])
        added = [d for d in drivers if d not in old]
        removed = [d for d in old if d not in drivers]
        
        if added or removed:
            state["log"] += "\n" + "\n".join([f"{ts} 🟢 {clean_name(d)}" for d in added] + [f"{ts} 🔴 {clean_name(d)}" for d in removed])
            wd = get_now().weekday()
            grid_locked = (wd == 6 and get_now().hour >= 18) or (wd == 0) or (wd == 1 and get_now().hour < 10)
            if grid_locked:
                wait_list = [clean_name(d) for d in added if drivers.index(d) >= grid_cap]
                if wait_list:
                    waitlist_msg = pick_text(WAITLIST_SINGLE if len(wait_list) == 1 else WAITLIST_MULTI).format(driver_names=", ".join(wait_list))
                    discord_post(CHAN_NEWS, waitlist_msg)
                up = [clean_name(d) for i, d in enumerate(drivers) if i < grid_cap and d in old and old.index(d) >= grid_cap]
                if up:
                    moved_up_msg = pick_text(MOVED_UP_SINGLE if len(up) == 1 else MOVED_UP_MULTI).format(driver_names=", ".join(up))
                    discord_post(CHAN_NEWS, moved_up_msg)
            msg_type = "roster_update"

        if state.get("hash") != current_hash or force_send:
            state["log_msg_id"] = send_or_edit_log(state["log"], state.get("log_msg_id"))
            state["hash"] = current_hash
            state["drivers"] = drivers
            save_state(state)
            
            if MAKE_WEBHOOK_URL:
                payload = {"type": msg_type, "drivers_count": driver_count, "drivers_list": drivers, "log": state["log"], "grid_full_msg": grid_full_msg, "sunday_msg": sunday_msg, "waitlist_msg": waitlist_msg, "moved_up_msg": moved_up_msg, "extra_grid_msg": extra_grid_msg, "timestamp": get_now().isoformat()}
                requests.post(MAKE_WEBHOOK_URL, json=payload)
            
            # Ausführliche Rückmeldung für den Browser
            res_txt = f"Aktion: {msg_type} (Fahrer: {driver_count})<br>"
            if added: res_txt += f"🟢 Neu: {', '.join([clean_name(d) for d in added])}<br>"
            if removed: res_txt += f"🔴 Weg: {', '.join([clean_name(d) for d in removed])}<br>"
            if grid_full_msg: res_txt += "📢 Nachricht: Grid Voll gesendet<br>"
            return res_txt
        
        return f"Keine Änderungen am Roster. Fahrer aktuell: {driver_count}"
    except Exception as e: return f"Fehler: {str(e)}"

@app.route('/')
def home(): return run_check()
if __name__ == "__main__": app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))