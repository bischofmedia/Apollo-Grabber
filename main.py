import os, requests, json, re, math, datetime, pytz, time
from flask import Flask, request

# --- KONFIGURATION ---
# Wir laden die Config einmal global, damit alle Funktionen darauf zugreifen können
def get_config():
    return {
        "TOKEN_APOLLO": os.environ.get("DISCORD_TOKEN_APOLLOGRABBER"),
        "TOKEN_LOBBY": os.environ.get("DISCORD_TOKEN_LOBBYCODEGRABBER"),
        "CHAN_APOLLO": os.environ.get("CHAN_APOLLO"),
        "CHAN_LOG": os.environ.get("CHAN_LOG"),
        "CHAN_CODES": os.environ.get("CHAN_CODES"),
        "MSG_LOBBY": os.environ.get("MSG_LOBBYCODES", "Willkommen im neuen Event!"),
        "MAKE_WEBHOOK_URL": os.environ.get("MAKE_WEBHOOK_URL"),
        "DRIVERS_PER_GRID": int(os.environ.get("DRIVERS_PER_GRID", 15)),
        "MAX_GRIDS": int(os.environ.get("MAX_GRIDS", 4)),
        "REG_END_TIME": os.environ.get("REGISTRATION_END_TIME", "").strip(),
        "SET_MANUAL_LOG_ID": os.environ.get("SET_MANUAL_LOG_ID", "").strip()
    }

APOLLO_BOT_ID = "475744554910351370"
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
        return [line.strip() for line in f.readlines() if line.strip()]

def restore_log_from_discord(config):
    if os.path.exists(LOG_FILE): return
    headers = {"Authorization": f"Bot {config['TOKEN_APOLLO']}"}
    
    # Sicherstellen, dass CHAN_LOG existiert
    if not config['CHAN_LOG']: return

    url = f"https://discord.com/api/v10/channels/{config['CHAN_LOG']}/messages"
    if config['SET_MANUAL_LOG_ID']: 
        url += f"/{config['SET_MANUAL_LOG_ID']}"
    else: 
        url += "?limit=10"
    
    try:
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            data = res.json()
            msg = data if config['SET_MANUAL_LOG_ID'] else next((m for m in data if "```" in m.get("content", "")), None)
            if msg:
                match = re.search(r"```\n(.*?)\n```", msg["content"], re.DOTALL)
                if match:
                    content = match.group(1).replace("...", "").strip()
                    if content:
                        with open(LOG_FILE, "w", encoding="utf-8") as f:
                            f.write(content + "\n")
    except: pass

def reconstruct_drivers_from_log():
    current_drivers = []
    for line in read_persistent_log():
        if " 🟢 " in line:
            parts = line.split(" 🟢 ")
            if len(parts) > 1:
                name = parts[1].replace(" (Waitlist)", "").replace(" (Nachgerückt)", "").strip()
                if name not in current_drivers: current_drivers.append(name)
        elif " 🔴 " in line:
            parts = line.split(" 🔴 ")
            if len(parts) > 1:
                name = parts[1].strip()
                if name in current_drivers: current_drivers.remove(name)
    return current_drivers

def lobby_cleanup(config):
    if not config["TOKEN_LOBBY"] or not config["CHAN_CODES"]: return
    headers = {"Authorization": f"Bot {config['TOKEN_LOBBY']}"}
    url = f"[https://discord.com/api/v10/channels/](https://discord.com/api/v10/channels/){config['CHAN_CODES']}/messages"
    res = requests.get(f"{url}?limit=100", headers=headers)
    if res.status_code == 200:
        for m in res.json():
            requests.delete(f"{url}/{m['id']}", headers=headers)
            time.sleep(0.4)
    requests.post(url, headers=headers, json={"content": config["MSG_LOBBY"]})

def extract_data(embed):
    title = embed.get("title", "Unbekanntes Event")
    drivers = []
    for field in embed.get("fields", []):
        if any(kw in field.get("name", "") for kw in ["Accepted", "Anmeldung", "Confirmed", "Zusagen"]):
            for line in field.get("value", "").split("\n"):
                c = re.sub(r"^\d+[\s.)-]*", "", line).strip()
                if c and "Grid" not in c and len(c) > 1: drivers.append(c)
    return title, drivers

# --- MAIN ---
@app.route('/')
def home():
    config = get_config()
    if not all([config["TOKEN_APOLLO"], config["CHAN_APOLLO"], config["CHAN_LOG"]]):
        return "Config Error: Kritische Variablen fehlen.", 500

    try:
        # 1. Wiederherstellung
        restore_log_from_discord(config)
        
        headers = {"Authorization": f"Bot {config['TOKEN_APOLLO']}"}
        res = requests.get(f"[https://discord.com/api/v10/channels/](https://discord.com/api/v10/channels/){config['CHAN_APOLLO']}/messages?limit=10", headers=headers)
        apollo_msg = next((m for m in res.json() if m.get("author", {}).get("id") == APOLLO_BOT_ID and m.get("embeds")), None)
        if not apollo_msg: return "Kein Apollo-Post."

        event_title, apollo_drivers = extract_data(apollo_msg["embeds"][0])
        now = get_now()
        wd = now.weekday()
        grid_cap = config['MAX_GRIDS'] * config['DRIVERS_PER_GRID']
        
        is_locked = (wd == 6 and now.hour >= 18) or (wd == 0)
        if not is_locked and wd == 0 and config['REG_END_TIME']:
            try:
                h, m = map(int, config['REG_END_TIME'].split(":"))
                if now >= now.replace(hour=h, minute=m, second=0, microsecond=0): is_locked = True
            except: pass
        if wd == 1 and now.hour < 10: is_locked = True

        log_lines = read_persistent_log()
        logged_drivers = reconstruct_drivers_from_log()
        
        is_new = not log_lines or (event_title not in log_lines[0] and "✨" in log_lines[0])
        webhook_type = "update"
        added, removed = [], []

        if is_new:
            if os.path.exists(LOG_FILE): os.remove(LOG_FILE)
            webhook_type = "event_reset"
            write_to_persistent_log(f"{format_ts_short(now)} ✨ Event gestartet ({event_title})")
            lobby_cleanup(config)
            for idx, d in enumerate(apollo_drivers):
                icon = "🟢" if idx < grid_cap else "🟡"
                write_to_persistent_log(f"{format_ts_short(now)} {icon} {clean_for_log(d)}{'' if idx < grid_cap else ' (Waitlist)'}")
            added = apollo_drivers
        else:
            if log_lines and "⚡ Systemstart" not in log_lines[-1]:
                write_to_persistent_log(f"{format_ts_short(now)} ⚡ Systemstart ({event_title})")
            
            added = [d for d in apollo_drivers if clean_for_log(d) not in logged_drivers]
            removed = [d for d in logged_drivers if d not in [clean_for_log(ad) for ad in apollo_drivers]]
            
            for d in added:
                idx = apollo_drivers.index(d)
                icon = "🟢" if idx < grid_cap else "🟡"
                write_to_persistent_log(f"{format_ts_short(now)} {icon} {clean_for_log(d)}{'' if idx < grid_cap else ' (Waitlist)'}")
            for d_name in removed:
                write_to_persistent_log(f"{format_ts_short(now)} 🔴 {d_name}")

        driver_count = len(apollo_drivers)
        grid_count = min(math.ceil(driver_count/config['DRIVERS_PER_GRID']), config['MAX_GRIDS'])

        sync_ts = "--"
        if config['MAKE_WEBHOOK_URL'] and (added or removed or is_new):
            if not is_locked or is_new:
                payload = {
                    "type": webhook_type,
                    "driver_count": driver_count,
                    "drivers": [raw_for_make(d) for d in apollo_drivers],
                    "grids": grid_count,
                    "log_history": "\n".join(read_persistent_log()),
                    "timestamp": now.isoformat()
                }
                requests.post(config['MAKE_WEBHOOK_URL'], json=payload)
                sync_ts = format_ts_short(now)

        send_or_edit_log(driver_count, grid_count, is_locked, config, sync_ts)
        return "OK"

    except Exception as e: return f"Error: {str(e)}", 500

def send_or_edit_log(driver_count, grid_count, is_locked, config, sync_ts):
    headers = {"Authorization": f"Bot {config['TOKEN_APOLLO']}", "Content-Type": "application/json"}
    grid_cap = config['MAX_GRIDS'] * config['DRIVERS_PER_GRID']
    icon = "🟡" if is_locked and driver_count >= grid_cap else ("🔴" if is_locked else "🟢")
    status = "Anmeldung geöffnet / Registration open" if not is_locked else ("Grids gesperrt & voll / Grids full" if driver_count >= grid_cap else "Grids gesperrt / Locked")
    
    full_log = read_persistent_log()
    log_text = ""
    for entry in reversed(full_log):
        if len(log_text) + len(entry) + 15 > 980:
            log_text = "...\n" + log_text
            break
        log_text = entry + "\n" + log_text
    
    legend = "🟢 Angemeldet / Registered\n🟡 Warteliste / Waitlist\n🔴 Abgemeldet / Withdrawn"
    formatted = (f"{icon} **{status}**\nFahrer / Drivers: `{driver_count}` | Grids: `{grid_count}`\n\n"
                 f"```\n{log_text or 'Initialisiere...'}```\n"
                 f"*Stand: {format_ts_short(get_now())}*\n"
                 f"*Letzte Übertragung / Last Grid Sync: {sync_ts}*\n\n**Legende:**\n{legend}")

    tid = config['SET_MANUAL_LOG_ID']
    if tid:
        requests.patch(f"[https://discord.com/api/v10/channels/](https://discord.com/api/v10/channels/){config['CHAN_LOG']}/messages/{tid}", headers=headers, json={"content": formatted})
    else:
        requests.post(f"[https://discord.com/api/v10/channels/](https://discord.com/api/v10/channels/){config['CHAN_LOG']}/messages", headers=headers, json={"content": formatted})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))