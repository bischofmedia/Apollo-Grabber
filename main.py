import os, requests, json, re, math, datetime, pytz, time, random, threading
from flask import Flask, request

# ---------- GLOBAL ----------
APOLLO_BOT_ID = "475744554910351370"
LOG_FILE = "event_log.txt"
BERLIN_TZ = pytz.timezone("Europe/Berlin")
LOG_LOCK = threading.Lock()

app = Flask(__name__)

# ---------- CONFIG ----------
def safe_int(val, default):
    try: return int(val)
    except: return default

def get_config():
    # Diese Funktion entfernt alles, was keine Zahl ist (behebt deinen URL-Fehler)
    def clean_id(v):
        if not v: return ""
        return re.sub(r'[^0-9]', '', str(v)).strip()
    
    env = os.environ
    return {
        "DRIVERS_PER_GRID": safe_int(env.get("DRIVERS_PER_GRID"), 15),
        "MAX_GRIDS": safe_int(env.get("MAX_GRIDS"), 4),
        "EXTRA_THRESHOLD": safe_int(env.get("EXTRA_GRID_THRESHOLD"), 10),
        "MIN_GRIDS_NEWS": safe_int(env.get("SET_MIN_GRIDS_MSG"), 2),
        "CHAN_NEWS": clean_id(env.get("CHAN_NEWS")),
        "CHAN_CODES": clean_id(env.get("CHAN_CODES")),
        "CHAN_APOLLO": clean_id(env.get("CHAN_APOLLO")),
        "CHAN_LOG": clean_id(env.get("CHAN_LOG")),
        "TOKEN_APOLLO": env.get("DISCORD_TOKEN_APOLLOGRABBER", "").strip(),
        "TOKEN_LOBBY": env.get("DISCORD_TOKEN_LOBBYCODEGRABBER", "").strip(),
        "MAKE_WEBHOOK_URL": env.get("MAKE_WEBHOOK_URL", "").strip(),
        "REG_END_TIME": env.get("REGISTRATION_END_TIME", "").strip(),
        "SET_MANUAL_LOG_ID": clean_id(env.get("SET_MANUAL_LOG_ID")),
        "MSG_LOBBYCODES": env.get("MSG_LOBBYCODES", ""),
        "DELETE_OLD_EVENT": env.get("DELETE_OLD_EVENT") == "1",
        "ENABLE_EXTRA_GRID": env.get("ENABLE_EXTRA_GRID") == "1",
        "SW_EXTRA": env.get("SET_MSG_EXTRA_GRID_TEXT") == "1",
        "SW_FULL": env.get("SET_MSG_GRID_FULL_TEXT") == "1",
        "SW_MOVE": env.get("SET_MSG_MOVED_UP_TEXT") == "1",
        "SW_SUNDAY": env.get("ENABLE_SUNDAY_MSG") == "1",
        "SW_WAIT": env.get("ENABLE_WAITLIST_MSG") == "1"
    }

# ---------- HELPERS ----------
def get_now(): return datetime.datetime.now(BERLIN_TZ)
def format_ts_short(dt): 
    return dt.strftime("%a %H:%M").replace("Mon","Mo").replace("Tue","Di").replace("Wed","Mi").replace("Thu","Do").replace("Fri","Fr").replace("Sat","Sa").replace("Sun","So")

def clean_for_log(name): return re.sub(r"[>\\]", "", name).strip()
def raw_for_make(name): return name.replace(">>>", "").replace(">", "").strip()

# ---------- LOGGING & RESTORE ----------
def read_log():
    with LOG_LOCK:
        if not os.path.exists(LOG_FILE): return []
        with open(LOG_FILE, "r", encoding="utf-8") as f: return [l.strip() for l in f if l.strip()]

def restore_log(config):
    if os.path.exists(LOG_FILE) or not config['CHAN_LOG']: return
    h = {"Authorization": f"Bot {config['TOKEN_APOLLO']}"}
    url = f"https://discord.com/api/v10/channels/{config['CHAN_LOG']}/messages"
    if config['SET_MANUAL_LOG_ID']: url += f"/{config['SET_MANUAL_LOG_ID']}"
    else: url += "?limit=10"
    try:
        res = requests.get(url, headers=h, timeout=5)
        if res.ok:
            data = res.json()
            msg = data if config['SET_MANUAL_LOG_ID'] else next((m for m in data if "```" in m.get("content", "")), None)
            if msg:
                match = re.search(r"```\n(.*?)\n```", msg["content"], re.DOTALL)
                if match:
                    with open(LOG_FILE, "w", encoding="utf-8") as f: f.write(match.group(1).strip() + "\n")
    except: pass

# ---------- MAIN ----------
@app.route("/")
def home():
    config = get_config()
    restore_log(config)
    
    if not config['TOKEN_APOLLO'] or not config['CHAN_APOLLO']:
        return "Missing Config: TOKEN_APOLLO or CHAN_APOLLO", 500
    
    # URL wird hier absolut sauber zusammengebaut (kein Markdown!)
    api_url = f"[https://discord.com/api/v10/channels/](https://discord.com/api/v10/channels/){config['CHAN_APOLLO']}/messages?limit=10"
    
    res = requests.get(api_url, headers={"Authorization": f"Bot {config['TOKEN_APOLLO']}"})
    if not res.ok: return f"Discord API Error: {res.status_code}", 500
    
    data = res.json()
    apollo_msg = next((m for m in data if str(m.get("author", {}).get("id")) == APOLLO_BOT_ID and m.get("embeds")), None)
    if not apollo_msg: return "Waiting for Apollo..."

    title = apollo_msg["embeds"][0].get("title", "Event")
    raw_drivers = []
    for f in apollo_msg["embeds"][0].get("fields", []):
        if any(k in f.get("name", "").lower() for k in ("accepted", "confirmed", "anmeldung")):
            for line in f.get("value", "").split("\n"):
                d = re.sub(r"^\d+[.)-]*\s*", "", line).strip()
                if d: raw_drivers.append(d)

    log = read_log()
    known_names = set(l.split(" 🟢 ")[-1].split(" 🟡 ")[-1].replace(" (Waitlist)", "").replace(" (Nachgerückt)", "").strip() for l in log if " 🟢 " in l or " 🟡 " in l)
    
    drivers_clean = [clean_for_log(d) for d in raw_drivers]
    added = [d for d in raw_drivers if clean_for_log(d) not in known_names]
    removed = [name for name in known_names if name not in drivers_clean]

    now = get_now()
    cap = config["DRIVERS_PER_GRID"] * config["MAX_GRIDS"]
    is_new = not log or (title not in log[0] and "✨" in log[0])
    
    if is_new:
        if os.path.exists(LOG_FILE): os.remove(LOG_FILE)
        with open(LOG_FILE, "w", encoding="utf-8") as f: f.write(f"{format_ts_short(now)} ✨ Event: {title}\n")

    for d in added:
        idx = raw_drivers.index(d)
        icon = "🟢" if idx < cap else "🟡"
        line = f"{format_ts_short(now)} {icon} {clean_for_log(d)}{'' if idx < cap else ' (Waitlist)'}"
        with LOG_LOCK:
            with open(LOG_FILE, "a", encoding="utf-8") as f: f.write(line + "\n")

    for d in removed:
        with LOG_LOCK:
            with open(LOG_FILE, "a", encoding="utf-8") as f: f.write(f"{format_ts_short(now)} 🔴 {d}\n")

    # Grids & Webhook
    count = len(raw_drivers)
    grids = min(math.ceil(count / config["DRIVERS_PER_GRID"]), config["MAX_GRIDS"])
    if config["ENABLE_EXTRA_GRID"] and count > cap + config["EXTRA_THRESHOLD"]: grids += 1

    if config["MAKE_WEBHOOK_URL"] and (added or removed or is_new):
        payload = {
            "type": "event_reset" if is_new else "update",
            "driver_count": count,
            "drivers": [raw_for_make(d) for d in raw_drivers],
            "grids": grids,
            "log_history": "\n".join(read_log()),
            "timestamp": now.isoformat()
        }
        requests.post(config["MAKE_WEBHOOK_URL"], json=payload)

    return "OK"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=safe_int(os.environ.get("PORT"), 10000))