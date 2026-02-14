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

# FIXES LIMIT (Darf nicht überschritten werden)
MAX_GRIDS = 4 

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

def get_now(): return datetime.datetime.now(BERLIN_TZ)
def pick_text(env_name, default): 
    val = os.environ.get(env_name, default)
    return random.choice([opt.strip() for opt in val.split(";")])

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
        
        wd = get_now().weekday()
        is_locked = (wd == 6 and get_now().hour >= 18) or (wd == 0) or (wd == 1 and get_now().hour < 10)

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
                existing = [f"{ts} ⚪ {clean_name(d)} (Bestand)" for d in drivers]
                state.update({"event_id": event_id, "sent_grids": [], "extra_grid_active": False, "log": f"{ts} 🔄 Systemstart\n" + "\n".join(existing), "log_msg_id": None, "drivers": drivers})
                msg_type = "initial_load"
                force_send = True

        driver_count = len(drivers)
        # Aktuelles Kapazitätslimit (Extra Grid öffnet bei Bedarf bis MAX_GRIDS)
        grid_cap = MAX_GRIDS * DRIVERS_PER_GRID
        
        # Grid-Berechnung mit hartem Limit
        if is_locked:
            effective_drivers = min(driver_count, grid_cap)
            grid_count = min(MAX_GRIDS, math.ceil(effective_drivers / 15)) if effective_drivers > 0 else 0
        else:
            grid_count = min(MAX_GRIDS, math.ceil(driver_count / 15)) if driver_count > 0 else 0

        # Warteliste basiert immer auf dem harten MAX_GRIDS Limit
        wait_count = max(0, driver_count - grid_cap)
        grid_full_msg = sunday_msg = waitlist_msg = moved_up_msg = extra_grid_msg = None

        # 1. Extra Grid Check (Nur falls ihr temporär unter MAX_GRIDS plantet, sonst hier wirkungslos)
        if not state["extra_grid_active"] and (wd in [6,0,1]) and wait_count >= EXTRA_GRID_THRESHOLD:
            # Hinweis: Wenn MAX_GRIDS fix ist, dient das hier nur noch der News-Meldung
            state["extra_grid_active"] = True
            extra_grid_msg = pick_text("MSG_EXTRA_GRID_TEXT", "Zusatzgrid eröffnet!").format(waitlist_count=wait_count)
            discord_post(CHAN_NEWS, extra_grid_msg)
            force_send = True

        # 2. Grid Voll Check (Nur bis zum harten Limit melden)
        if driver_count > 0 and driver_count % 15 == 0:
            full = driver_count // 15
            if full <= MAX_GRIDS and full >= MIN_GRIDS_FOR_MESSAGE and full not in state["sent_grids"]:
                grid_full_msg = pick_text("MSG_GRID_FULL_TEXT", "Grid {full_grids} voll").format(full_grids=full)
                discord_post(CHAN_NEWS, grid_full_msg)
                state["sent_grids"].append(full)
                force_send = True

        # 3. Sonntag 18 Uhr Check
        if wd == 6 and get_now().hour == 18 and get_now().minute < 10:
            if state.get("last_sunday_msg_event") != event_id:
                free = max(0, grid_cap - driver_count)
                sunday_msg = pick_text("MSG_SUNDAY_TEXT", "Sonntag 18h Report").format(driver_count=driver_count, free_slots=free)
                discord_post(CHAN_LOG, sunday_msg)
                state["last_sunday_msg_event"] = event_id
                force_send = True

        old = state.get("drivers", [])
        added = [d for d in drivers if d not in old]
        removed = [d for d in old if d not in drivers]
        
        if added or removed:
            state["log"] += "\n" + "\n".join([f"{ts} 🟢 {clean_name(d)}" for d in added] + [f"{ts} 🔴 {clean_name(d)}" for d in removed])
            if is_locked:
                wait_list = [clean_name(d) for d in added if drivers.index(d) >= grid_cap]
                if wait_list:
                    waitlist_msg = pick_text("MSG_WAITLIST_SINGLE" if len(wait_list)==1 else "MSG_WAITLIST_MULTI", "Warteliste Update").format(driver_names=", ".join(wait_list))
                    discord_post(CHAN_NEWS, waitlist_msg)
                up = [clean_name(d) for i, d in enumerate(drivers) if i < grid_cap and d in old and old.index(d) >= grid_cap]
                if up:
                    moved_up_msg = pick_text("MSG_MOVED_UP_SINGLE" if len(up)==1 else "MSG_MOVED_UP_MULTI", "Nachgerückt").format(driver_names=", ".join(up))
                    discord_post(CHAN_NEWS, moved_up_msg)
            msg_type = "roster_update"

        if state.get("hash") != current_hash or force_send:
            state["log_msg_id"] = send_or_edit_log(state["log"], state.get("log_msg_id"))
            state["hash"] = current_hash
            state["drivers"] = drivers
            save_state(state)
            
            if MAKE_WEBHOOK_URL:
                payload = {"type": msg_type, "driver_count": driver_count, "drivers": drivers, "grids": grid_count, "log": state["log"], "grid_full_msg": grid_full_msg, "sunday_msg": sunday_msg, "waitlist_msg": waitlist_msg, "moved_up_msg": moved_up_msg, "extra_grid_msg": extra_grid_msg, "timestamp": get_now().isoformat()}
                requests.post(MAKE_WEBHOOK_URL, json=payload)
            
            res_txt = f"Aktion: {msg_type} (Fahrer: {driver_count}, Grids: {grid_count})<br>"
            if added: res_txt += f"🟢 Neu: {', '.join([clean_name(d) for d in added])}<br>"
            if removed: res_txt += f"🔴 Weg: {', '.join([clean_name(d) for d in removed])}<br>"
            return res_txt
        
        return f"Keine Änderungen. Fahrer: {driver_count} (Grids: {grid_count})"
    except Exception as e: return f"Fehler: {str(e)}"

@app.route('/')
def home(): return run_check()
if __name__ == "__main__": app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))