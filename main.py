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
    # Strikte Bereinigung der IDs um Connection-Fehler zu vermeiden
    def only_num(v): return re.sub(r'[^0-9]', '', str(v)) if v else ""
    
    env = os.environ
    return {
        "DRIVERS_PER_GRID": safe_int(env.get("DRIVERS_PER_GRID"), 15),
        "MAX_GRIDS": safe_int(env.get("MAX_GRIDS"), 4),
        "EXTRA_THRESHOLD": safe_int(env.get("EXTRA_GRID_THRESHOLD"), 10),
        "MIN_GRIDS_NEWS": safe_int(env.get("SET_MIN_GRIDS_MSG"), 2),
        "CHAN_NEWS": only_num(env.get("CHAN_NEWS")),
        "CHAN_CODES": only_num(env.get("CHAN_CODES")),
        "CHAN_APOLLO": only_num(env.get("CHAN_APOLLO")),
        "CHAN_LOG": only_num(env.get("CHAN_LOG")),
        "TOKEN_APOLLO": env.get("DISCORD_TOKEN_APOLLOGRABBER", ""),
        "TOKEN_LOBBY": env.get("DISCORD_TOKEN_LOBBYCODEGRABBER", ""),
        "MAKE_WEBHOOK_URL": env.get("MAKE_WEBHOOK_URL", ""),
        "REG_END_TIME": env.get("REGISTRATION_END_TIME", ""),
        "SET_MANUAL_LOG_ID": only_num(env.get("SET_MANUAL_LOG_ID")),
        "MSG_LOBBYCODES": env.get("MSG_LOBBYCODES", ""),
        "SW_EXTRA": env.get("SET_MSG_EXTRA_GRID_TEXT") == "1",
        "SW_FULL": env.get("SET_MSG_GRID_FULL_TEXT") == "1",
        "SW_MOVE": env.get("SET_MSG_MOVED_UP_TEXT") == "1",
        "SW_SUNDAY": env.get("ENABLE_SUNDAY_MSG") == "1",
        "SW_WAIT": env.get("ENABLE_WAITLIST_MSG") == "1",
        "ENABLE_EXTRA_LOGIC": env.get("ENABLE_EXTRA_GRID") == "1"
    }

# ---------- HELPERS ----------
def get_now(): return datetime.datetime.now(BERLIN_TZ)
def format_ts_short(dt): return dt.strftime("%a %H:%M").replace("Mon","Mo").replace("Tue","Di").replace("Wed","Mi").replace("Thu","Do").replace("Fri","Fr").replace("Sat","Sa").replace("Sun","So")
def clean_for_log(name): return re.sub(r"[>\\]", "", name).strip()
def raw_for_make(name): return name.replace(">>>", "").replace(">", "").strip()

# ---------- LOGGING & RESTORE ----------
def read_log():
    with LOG_LOCK:
        if not os.path.exists(LOG_FILE): return []
        with open(LOG_FILE, "r", encoding="utf-8") as f: return [l.strip() for l in f if l.strip()]

def restore_log(config):
    if os.path.exists(LOG_FILE): return
    url = f"https://discord.com/api/v10/channels/{config['CHAN_LOG']}/messages"
    if config['SET_MANUAL_LOG_ID']: url += f"/{config['SET_MANUAL_LOG_ID']}"
    else: url += "?limit=10"
    res = requests.get(url, headers={"Authorization": f"Bot {config['TOKEN_APOLLO']}"})
    if res.ok:
        data = res.json()
        msg = data if config['SET_MANUAL_LOG_ID'] else next((m for m in data if "```" in m.get("content", "")), None)
        if msg:
            match = re.search(r"```\n(.*?)\n```", msg["content"], re.DOTALL)
            if match:
                with open(LOG_FILE, "w", encoding="utf-8") as f: f.write(match.group(1).strip() + "\n")

# ---------- DISCORD NEWS ----------
def send_combined_news(config, key_de, **fmt):
    msg_de = os.environ.get(key_de, "")
    msg_en = os.environ.get(key_de + "_EN", "")
    if not msg_de: return
    try: 
        content = msg_de.format(**fmt)
        if msg_en: content += "\n\n" + msg_en.format(**fmt)
    except: content = msg_de
    requests.post(f"[https://discord.com/api/v10/channels/](https://discord.com/api/v10/channels/){config['CHAN_NEWS']}/messages", 
                  headers={"Authorization": f"Bot {config['TOKEN_APOLLO']}"}, json={"content": content})

# ---------- MAIN ----------
@app.route("/")
def home():
    config = get_config()
    restore_log(config)
    
    # Apollo Abruf
    res = requests.get(f"[https://discord.com/api/v10/channels/](https://discord.com/api/v10/channels/){config['CHAN_APOLLO']}/messages?limit=10", 
                       headers={"Authorization": f"Bot {config['TOKEN_APOLLO']}"})
    if not res.ok: return "Discord Error", 500
    
    apollo_msg = next((m for m in res.json() if m.get("author", {}).get("id") == APOLLO_BOT_ID and m.get("embeds")), None)
    if not apollo_msg: return "Waiting for Apollo..."

    # Daten extrahieren
    embed = apollo_msg["embeds"][0]
    title = embed.get("title", "Event")
    raw_drivers = []
    for f in embed.get("fields", []):
        if any(k in f.get("name", "").lower() for k in ("accepted", "confirmed", "anmeldung")):
            for line in f.get("value", "").split("\n"):
                d = re.sub(r"^\d+[.)-]*\s*", "", line).strip()
                if d: raw_drivers.append(d)

    now = get_now()
    log = read_log()
    known_names = set(l.split(" 🟢 ")[-1].split(" 🟡 ")[-1].replace(" (Waitlist)", "").replace(" (Nachgerückt)", "").strip() for l in log if " 🟢 " in l or " 🟡 " in l)
    
    added = [d for d in raw_drivers if clean_for_log(d) not in known_names]
    removed = [clean_for_log(d) for d in known_names if clean_for_log(d) not in [clean_for_log(rd) for rd in raw_drivers]]

    # Logging
    cap = config["DRIVERS_PER_GRID"] * config["MAX_GRIDS"]
    is_new = not log or (title not in log[0] and "✨" in log[0])
    
    if is_new:
        if os.path.exists(LOG_FILE): os.remove(LOG_FILE)
        with open(LOG_FILE, "w", encoding="utf-8") as f: f.write(f"{format_ts_short(now)} ✨ Event: {title}\n")
        # Hier könnte noch lobby_cleanup rein...
    
    for d in added:
        idx = raw_drivers.index(d)
        icon = "🟢" if idx < cap else "🟡"
        line = f"{format_ts_short(now)} {icon} {clean_for_log(d)}{'' if idx < cap else ' (Waitlist)'}"
        with open(LOG_FILE, "a", encoding="utf-8") as f: f.write(line + "\n")
        if idx >= cap and config["SW_WAIT"]: send_combined_news(config, "MSG_WAITLIST_SINGLE", driver_names=clean_for_log(d))

    for d in removed:
        with open(LOG_FILE, "a", encoding="utf-8") as f: f.write(f"{format_ts_short(now)} 🔴 {d}\n")

    # Grids & Webhook
    count = len(raw_drivers)
    grids = min(math.ceil(count / config["DRIVERS_PER_GRID"]), config["MAX_GRIDS"])
    if config["ENABLE_EXTRA_LOGIC"] and count > cap + config["EXTRA_THRESHOLD"]: grids += 1

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

    # Monitor Update (send_or_edit_log Logik hier integrieren...)
    return "OK"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))