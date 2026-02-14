import os, requests, json, re, math, datetime, pytz, random
from flask import Flask, request

# --- KONFIGURATION ---
def get_env_config():
    return {
        "DISCORD_TOKEN": os.environ.get("DISCORD_TOKEN"),
        "CHAN_APOLLO": os.environ.get("CHAN_APOLLO"),
        "CHAN_LOG": os.environ.get("CHAN_LOG"),
        "CHAN_NEWS": os.environ.get("CHAN_NEWS"),
        "MAKE_WEBHOOK_URL": os.environ.get("MAKE_WEBHOOK_URL")
    }

DELETE_OLD_EVENT = os.environ.get("DELETE_OLD_EVENT", "0") == "1"
EXTRA_GRID_THRESHOLD = int(os.environ.get("EXTRA_GRID_THRESHOLD", 10))
DRIVERS_PER_GRID = int(os.environ.get("DRIVERS_PER_GRID", 15))
MAX_GRIDS = int(os.environ.get("MAX_GRIDS", 4))
REG_END_TIME = os.environ.get("REGISTRATION_END_TIME", "").strip()
SET_MANUAL_LOG_ID = os.environ.get("SET_MANUAL_LOG_ID", "").strip()

APOLLO_BOT_ID = "475744554910351370"
STATE_FILE = "state.json"
BERLIN_TZ = pytz.timezone("Europe/Berlin")

app = Flask(__name__)

# --- HELFER ---
def get_now(): return datetime.datetime.now(BERLIN_TZ)

def format_ts_short(dt_obj):
    days = {"Mon":"Mo", "Tue":"Di", "Wed":"Mi", "Thu":"Do", "Fri":"Fr", "Sat":"Sa", "Sun":"So"}
    raw = dt_obj.strftime("%a %H:%M")
    for en, de in days.items(): raw = raw.replace(en, de)
    return raw

def clean_name(n): return n.replace("\\_", "_").replace("\\*", "*").replace("*", "").strip()

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f: return json.load(f)
        except: pass
    return {"event_id": None, "drivers": [], "log_v2": [], "sent_grids": [], "extra_grid_active": False, "log_msg_id": None, "grid_override": None, "last_make_sync": None}

def save_state(s):
    with open(STATE_FILE, "w") as f: json.dump(s, f)

def extract_data(embed):
    drivers = []
    for field in embed.get("fields", []):
        if any(kw in field.get("name", "") for kw in ["Accepted", "Anmeldung", "Teilnehmer", "Confirmed", "Zusagen"]):
            for line in field.get("value", "").split("\n"):
                c = re.sub(r"^\d+[\s.)-]*", "", line.replace(">>>", "").replace(">", "")).strip()
                if c and "Grid" not in c and len(c) > 1: drivers.append(c)
    return drivers

# --- MAIN ---
@app.route('/')
def home():
    config = get_env_config()
    if not all([config["DISCORD_TOKEN"], config["CHAN_APOLLO"], config["CHAN_LOG"], config["CHAN_NEWS"]]):
        return "Config Error: Kritische Variablen fehlen.", 500

    try:
        headers = {"Authorization": f"Bot {config['DISCORD_TOKEN']}"}
        res = requests.get(f"https://discord.com/api/v10/channels/{config['CHAN_APOLLO']}/messages?limit=10", headers=headers)
        apollo_msg = next((m for m in res.json() if m.get("author", {}).get("id") == APOLLO_BOT_ID and m.get("embeds")), None)
        if not apollo_msg: return "Keine Apollo-Nachricht gefunden."

        drivers = extract_data(apollo_msg["embeds"][0])
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

        report = []
        is_new = (state.get("event_id") and state["event_id"] != apollo_msg["id"])
        webhook_type = "update"

        if is_new or state.get("event_id") is None:
            webhook_type = "event_reset"
            # Initialisiere State neu
            state.update({"event_id": apollo_msg["id"], "sent_grids": [], "log_v2": [], "drivers": drivers, "grid_override": None, "extra_grid_active": False})
            
            # WICHTIG: Alle bereits angemeldeten Fahrer sofort ins Log schreiben
            state["log_v2"].append(f"{now_iso}|✨ Neues Event erkannt")
            for idx, d in enumerate(drivers):
                icon = "🟢" if idx < grid_cap else "🟡"
                suffix = "" if idx < grid_cap else " (Waitlist)"
                state["log_v2"].append(f"{now_iso}|{icon} {clean_name(d)}{suffix}")
            
            report.append("✨ <b>Event Reset:</b> Neues Event mit Roster initialisiert.")
            added, removed = [], []
        else:
            old = state.get("drivers", [])
            added = [d for d in drivers if d not in old]
            removed = [d for d in old if d not in drivers]
            
            for d in added:
                idx = drivers.index(d)
                icon = "🟢" if idx < grid_cap else "🟡"
                state["log_v2"].append(f"{now_iso}|{icon} {clean_name(d)}{'' if idx < grid_cap else ' (Waitlist)'}")
                report.append(f"🟢 + {clean_name(d)}")
            for d in removed:
                state["log_v2"].append(f"{now_iso}|🔴 {clean_name(d)}")
                report.append(f"🔴 - {clean_name(d)}")
            for d in drivers:
                if d in old and drivers.index(d) < grid_cap and old.index(d) >= grid_cap:
                    state["log_v2"].append(f"{now_iso}|🟢 {clean_name(d)} (Nachgerückt)")

        driver_count = len(drivers)
        grid_count = state["grid_override"] if state.get("grid_override") else min(math.ceil(driver_count/DRIVERS_PER_GRID), MAX_GRIDS)

        # Webhook Sync an Make
        webhook_status = "Keine Änderung"
        if config['MAKE_WEBHOOK_URL'] and (added or removed or is_new):
            if not is_locked or is_new:
                payload = {
                    "type": webhook_type, # "event_reset" oder "update"
                    "driver_count": driver_count,
                    "drivers": [clean_name(d) for d in drivers],
                    "grids": grid_count,
                    "timestamp": now_iso
                }
                requests.post(config['MAKE_WEBHOOK_URL'], json=payload)
                state["last_make_sync"] = now_iso
                webhook_status = f"✅ {webhook_type}"
            else:
                webhook_status = "🚫 Lock"

        state["log_msg_id"] = send_or_edit_log(state, driver_count, grid_count, is_locked, config)
        state["drivers"] = drivers
        save_state(state)
        
        return f"<h2>Apollo-Monitor</h2>Status: {'LOCK' if is_locked else 'OPEN'}<br>Sync: {webhook_status}<br><br>Letzte Aktivitäten:<br>" + ("<br>".join(report) if report else "Warte auf Änderungen...")

    except Exception as e: return f"Error: {str(e)}", 500

def send_or_edit_log(state, driver_count, grid_count, is_locked, config):
    headers = {"Authorization": f"Bot {config['DISCORD_TOKEN']}", "Content-Type": "application/json"}
    grid_cap = MAX_GRIDS * DRIVERS_PER_GRID
    icon = "🟡" if is_locked and driver_count >= grid_cap else ("🔴" if is_locked else "🟢")
    
    log_entries = []
    now = get_now()
    # Zeige die letzten 24h an, aber mindestens die letzten 20 Einträge
    for entry in state.get("log_v2", []):
        ts_str, content = entry.split("|", 1)
        ts_dt = datetime.datetime.fromisoformat(ts_str)
        if now - ts_dt <= datetime.timedelta(hours=24):
            log_entries.append(f"{format_ts_short(ts_dt.astimezone(BERLIN_TZ))} {content.strip()}")
    
    log_text = "\n".join(log_entries[-25:]) if log_entries else "Initialisiere..."
    sync_ts = format_ts_short(datetime.datetime.fromisoformat(state['last_make_sync']).astimezone(BERLIN_TZ)) if state.get('last_make_sync') else "--"
    legend = "🟢 Grid | 🟡 Warteliste/Waitlist | 🔴 Abgemeldet/Withdrawn"

    formatted = (f"{icon} **Grid-Monitor**\n"
                 f"Fahrer: `{driver_count}` | Grids: `{grid_count}`\n"
                 f"```\n{log_text}\n```\n"
                 f"*Sync: {sync_ts}*\n"
                 f"**Legende:** {legend}")

    tid = SET_MANUAL_LOG_ID or state.get("log_msg_id")
    if tid:
        requests.patch(f"https://discord.com/api/v10/channels/{config['CHAN_LOG']}/messages/{tid}", headers=headers, json={"content": formatted})
        return tid
    res = requests.post(f"https://discord.com/api/v10/channels/{config['CHAN_LOG']}/messages", headers=headers, json={"content": formatted})
    return res.json().get("id")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))