import os, requests, json, re, math, datetime, pytz, random
from flask import Flask

# --- KONFIGURATION (Render Environment Variables) ---
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
CHAN_LOG = os.environ.get("CHAN_MAIN_LOG")
CHAN_NEWS = os.environ.get("CHAN_NEWS_FLASH")
MAKE_WEBHOOK_URL = os.environ.get("MAKE_WEBHOOK_URL")

# Schalter & Werte (Präfix SET_)
DELETE_OLD_EVENT = os.environ.get("SET_DELETE_OLD_EVENT", "0") == "1"
EXTRA_GRID_THRESHOLD = int(os.environ.get("SET_EXTRA_GRID_THRESHOLD", 10))
MIN_GRIDS_FOR_MESSAGE = int(os.environ.get("SET_MIN_GRIDS_MSG", 1))

# Texte (Präfix MSG_)
GRID_FULL_TEXT = os.environ.get("MSG_GRID_FULL_TEXT", "Grid {full_grids} ist voll!")
SUNDAY_MSG_TEXT = os.environ.get("MSG_SUNDAY_TEXT", "Sonntag 18 Uhr: {driver_count} Fahrer.")
WAITLIST_SINGLE = os.environ.get("MSG_WAITLIST_SINGLE", "Warteliste: {driver_names} ist neu.")
WAITLIST_MULTI = os.environ.get("MSG_WAITLIST_MULTI", "Warteliste: {driver_names} sind neu.")
MOVED_UP_SINGLE = os.environ.get("MSG_MOVED_UP_SINGLE", "{driver_names} ist nachgerückt!")
MOVED_UP_MULTI = os.environ.get("MSG_MOVED_UP_MULTI", "{driver_names} sind nachgerückt!")
EXTRA_GRID_TEXT = os.environ.get("MSG_EXTRA_GRID_TEXT", "Zusatzgrid eröffnet!")

APOLLO_BOT_ID = "475744554910351370"
DRIVERS_PER_GRID = 15
MAX_GRIDS = 4
STATE_FILE = "state.json"
BERLIN_TZ = pytz.timezone("Europe/Berlin")

app = Flask(__name__)

# --- DISCORD AKTIONEN ---
def discord_post(channel_id, content):
    if not content or not channel_id: return
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"}
    res = requests.post(url, headers=headers, json={"content": content})
    return res.json().get("id") if res.status_code == 200 else None

def discord_delete(channel_id, msg_id):
    if not msg_id or not channel_id: return
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{msg_id}"
    requests.delete(url, headers={"Authorization": f"Bot {DISCORD_TOKEN}"})

def send_or_edit_log(content, current_log_id, state):
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"}
    payload = {"content": f"**Logbuch der Anmeldungen:**\n```\n{content}\n```"}
    if current_log_id:
        url = f"https://discord.com/api/v10/channels/{CHAN_LOG}/messages/{current_log_id}"
        res = requests.patch(url, headers=headers, json=payload)
        if res.status_code == 200: return current_log_id
    return discord_post(CHAN_LOG, payload["content"])

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
    if not all([DISCORD_TOKEN, CHAN_LOG, CHAN_NEWS]): return "Config Error"
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}"}
    try:
        res = requests.get(f"https://discord.com/api/v10/channels/{CHAN_LOG}/messages?limit=10", headers=headers)
        messages = res.json()
        apollo_msg = next((m for m in messages if m.get("author", {}).get("id") == APOLLO_BOT_ID and m.get("embeds")), None)
        if not apollo_msg: return "No Apollo message"

        event_id = apollo_msg["id"]
        drivers, raw_content = extract_data(apollo_msg["embeds"][0])
        current_hash = str(hash(raw_content))
        state = load_state()
        ts = f"{['Mo','Di','Mi','Do','Fr','Sa','So'][get_now().weekday()]} {get_now().strftime('%H:%M')}"
        
        is_new = (state.get("event_id") and state["event_id"] != event_id)
        if is_new or not state.get("event_id"):
            if is_new:
                discord_delete(CHAN_LOG, state.get("log_msg_id"))
                if DELETE_OLD_EVENT: discord_delete(CHAN_LOG, state["event_id"])
            state.update({"event_id": event_id, "sent_grids": [], "extra_grid_active": False, "log": f"{ts} 📅 Start", "log_msg_id": None, "drivers": []})

        driver_count = len(drivers)
        grid_cap = 75 if state["extra_grid_active"] else 60
        wait_count = max(0, driver_count - 60)

        # 1. Extra Grid
        if not state["extra_grid_active"] and (get_now().weekday() in [6,0,1]) and wait_count >= EXTRA_GRID_THRESHOLD:
            state["extra_grid_active"] = True
            discord_post(CHAN_NEWS, pick_text(EXTRA_GRID_TEXT).format(waitlist_count=wait_count))
            grid_cap = 75

        # 2. Grid Voll
        if driver_count > 0 and driver_count % 15 == 0:
            full = driver_count // 15
            if full >= MIN_GRIDS_FOR_MESSAGE and full not in state["sent_grids"]:
                discord_post(CHAN_NEWS, pick_text(GRID_FULL_TEXT).format(full_grids=full))
                state["sent_grids"].append(full)

        # 3. Sonntag 18 Uhr
        if get_now().weekday() == 6 and get_now().hour == 18 and get_now().minute < 10:
            if state.get("last_sunday_msg_event") != event_id:
                free = max(0, grid_cap - driver_count)
                discord_post(CHAN_LOG, pick_text(SUNDAY_MSG_TEXT).format(driver_count=driver_count, free_slots=free))
                state["last_sunday_msg_event"] = event_id

        # 4. Roster Update
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
                    txt = WAITLIST_SINGLE if len(wait_list) == 1 else WAITLIST_MULTI
                    discord_post(CHAN_NEWS, pick_text(txt).format(driver_names=", ".join(wait_list)))
                
                up = [clean_name(d) for i, d in enumerate(drivers) if i < grid_cap and d in old and old.index(d) >= grid_cap]
                if up:
                    txt = MOVED_UP_SINGLE if len(up) == 1 else MOVED_UP_MULTI
                    discord_post(CHAN_NEWS, pick_text(txt).format(driver_names=", ".join(up)))

        if state.get("hash") != current_hash:
            state["log_msg_id"] = send_or_edit_log(state["log"], state.get("log_msg_id"), state)
            state["hash"] = current_hash
            save_state(state)
            if MAKE_WEBHOOK_URL: requests.post(MAKE_WEBHOOK_URL, json={"status": "updated", "drivers": driver_count})
        
        return "OK"
    except Exception as e: return str(e)

@app.route('/')
def home(): return run_check()
if __name__ == "__main__": app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))