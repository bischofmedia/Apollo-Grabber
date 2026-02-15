import os, requests, json, re, math, datetime, pytz, threading
from flask import Flask, request

# --- KONFIGURATION (Angepasst an deine aktuellen Render-Variablen) ---
def get_env_config():
    # Schützt vor dem "Connection Adapter" Fehler durch radikale ID-Reinigung
    def c_id(v): return re.sub(r'[^0-9]', '', str(v)) if v else ""
    
    e = os.environ
    return {
        "TOKEN_APOLLO": e.get("DISCORD_TOKEN_APOLLOGRABBER"),
        "TOKEN_LOBBY": e.get("DISCORD_TOKEN_LOBBYCODEGRABBER"),
        "CHAN_APOLLO": c_id(e.get("CHAN_APOLLO")),
        "CHAN_LOG": c_id(e.get("CHAN_LOG")),
        "CHAN_NEWS": c_id(e.get("CHAN_NEWS")),
        "CHAN_CODES": c_id(e.get("CHAN_CODES")),
        "MAKE_WEBHOOK": e.get("MAKE_WEBHOOK_URL"),
        "DRIVERS_PER_GRID": int(e.get("DRIVERS_PER_GRID", 15)),
        "MAX_GRIDS": int(e.get("MAX_GRIDS", 4)),
        "EXTRA_THRESH": int(e.get("EXTRA_GRID_THRESHOLD", 10)),
        "ENABLE_EXTRA": e.get("ENABLE_EXTRA_GRID") == "1",
        "MSG_LOBBY": e.get("MSG_LOBBYCODES", "Lobby offen!"),
        "REG_END_TIME": e.get("REGISTRATION_END_TIME", "").strip(),
        "MANUAL_LOG_ID": c_id(e.get("SET_MANUAL_LOG_ID"))
    }

APOLLO_BOT_ID = "475744554910351370"
LOG_FILE = "event_log.txt"
STATE_FILE = "state.json"
BERLIN_TZ = pytz.timezone("Europe/Berlin")
LOG_LOCK = threading.Lock()

app = Flask(__name__)

# --- HELFER ---
def get_now(): return datetime.datetime.now(BERLIN_TZ)

def format_ts_short(dt_obj):
    days = {"Mon":"Mo", "Tue":"Di", "Wed":"Mi", "Thu":"Do", "Fri":"Fr", "Sat":"Sa", "Sun":"So"}
    raw = dt_obj.strftime("%a %H:%M")
    for en, de in days.items(): raw = raw.replace(en, de)
    return raw

def clean_for_log(n): return n.replace("\\", "").replace(">>>", "").replace(">", "").strip()
def raw_for_make(n): return n.replace(">>>", "").replace(">", "").strip()

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f: return json.load(f)
        except: pass
    return {"event_id": None, "drivers": [], "last_make_sync": None}

def save_state(s):
    with open(STATE_FILE, "w") as f: json.dump(s, f)

def read_persistent_log():
    if not os.path.exists(LOG_FILE): return []
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip()]

# --- MAIN ---
@app.route('/')
def home():
    conf = get_env_config()
    # Check ob die kritischsten Variablen da sind
    if not conf["TOKEN_APOLLO"] or not conf["CHAN_APOLLO"]:
        return "Config Error: Check Tokens/Channels", 500

    try:
        # SICHERER URL-AUFRUF (Zusammenbau ohne f-string Risiko)
        api_url = "https://discord.com/api/v10/channels/" + conf["CHAN_APOLLO"] + "/messages?limit=10"
        headers = {"Authorization": "Bot " + conf["TOKEN_APOLLO"]}
        
        res = requests.get(api_url, headers=headers, timeout=10)
        if not res.ok: return f"Discord API Error: {res.status_code}", 500
        
        data = res.json()
        apollo_msg = next((m for m in data if str(m.get("author", {}).get("id")) == APOLLO_BOT_ID and m.get("embeds")), None)
        if not apollo_msg: return "Kein Apollo-Post gefunden."

        # Daten extrahieren
        embed = apollo_msg["embeds"][0]
        event_title = embed.get("title", "Event")
        drivers = []
        for field in embed.get("fields", []):
            if any(kw in field.get("name", "").lower() for kw in ["accepted", "confirmed", "anmeldung"]):
                for line in field.get("value", "").split("\n"):
                    c = re.sub(r"^\d+[\s.)-]*", "", line).strip()
                    if c: drivers.append(c)

        state = load_state()
        now = get_now()
        grid_cap = conf["MAX_GRIDS"] * conf["DRIVERS_PER_GRID"]
        
        is_new = (state.get("event_id") and state["event_id"] != apollo_msg["id"])
        
        # LOG HANDLING
        if is_new or not os.path.exists(LOG_FILE):
            if os.path.exists(LOG_FILE): os.remove(LOG_FILE)
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.write(f"{format_ts_short(now)} ✨ Event: {event_title}\n")
            state = {"event_id": apollo_msg["id"], "drivers": [], "last_make_sync": None}
            # Hier könnte der Lobby-Cleanup rein, falls gewünscht
        
        old_drivers = state.get("drivers", [])
        added = [d for d in drivers if d not in old_drivers]
        removed = [d for d in old_drivers if d not in drivers]

        if added or removed:
            with LOG_LOCK:
                with open(LOG_FILE, "a", encoding="utf-8") as f:
                    for d in added:
                        idx = drivers.index(d)
                        icon = "🟢" if idx < grid_cap else "🟡"
                        f.write(f"{format_ts_short(now)} {icon} {clean_for_log(d)}{'' if idx < grid_cap else ' (Waitlist)'}\n")
                    for d in removed:
                        f.write(f"{format_ts_short(now)} 🔴 {clean_for_log(d)}\n")

        # GRID BERECHNUNG
        driver_count = len(drivers)
        grids = min(math.ceil(driver_count / conf["DRIVERS_PER_GRID"]), conf["MAX_GRIDS"])
        if conf["ENABLE_EXTRA"] and driver_count > grid_cap + conf["EXTRA_THRESH"]:
            grids += 1

        # WEBHOOK AN MAKE
        if conf["MAKE_WEBHOOK"] and (added or removed or is_new):
            payload = {
                "type": "event_reset" if is_new else "update",
                "driver_count": driver_count,
                "drivers": [raw_for_make(d) for d in drivers],
                "grids": grids,
                "log_history": "\n".join(read_persistent_log()),
                "timestamp": now.isoformat()
            }
            requests.post(conf["MAKE_WEBHOOK"], json=payload)
            state["last_make_sync"] = now.isoformat()

        # DISCORD LOG UPDATE
        send_or_edit_discord_log(conf, state, driver_count, grids)
        
        state["drivers"] = drivers
        save_state(state)
        return "OK - V85"

    except Exception as e:
        return f"Fehler: {str(e)}", 500

def send_or_edit_discord_log(conf, state, count, grids):
    if not conf["CHAN_LOG"]: return
    
    log_entries = read_persistent_log()
    log_text = "\n".join(log_entries[-15:]) # Letzte 15 Zeilen für Discord
    
    sync_ts = "--"
    if state.get("last_make_sync"):
        dt = datetime.datetime.fromisoformat(state["last_make_sync"])
        sync_ts = format_ts_short(dt.astimezone(BERLIN_TZ))

    content = (f"📊 **Event Status: {count} Fahrer / {grids} Grids**\n"
               f"```\n{log_text}```\n"
               f"*Letzter Sync: {sync_ts}*")

    h = {"Authorization": "Bot " + conf["TOKEN_APOLLO"]}
    msg_id = conf["MANUAL_LOG_ID"]
    
    if msg_id:
        requests.patch(f"https://discord.com/api/v10/channels/{conf['CHAN_LOG']}/messages/{msg_id}", headers=h, json={"content": content})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
   