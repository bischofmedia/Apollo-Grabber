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
        return [line.strip() for line in f.readlines() if line.strip()]

def restore_log_from_discord(config):
    """Liest die letzte Bot-Nachricht in Discord, um das Log wiederherzustellen."""
    if os.path.exists(LOG_FILE): return
    headers = {"Authorization": f"Bot {config['TOKEN_APOLLO']}"}
    msg_id = SET_MANUAL_LOG_ID
    
    # Wenn keine ID fixiert ist, suchen wir die letzte Nachricht im Kanal
    url = f"https://discord.com/api/v10/channels/{config['CHAN_LOG']}/messages"
    if msg_id: url += f"/{msg_id}"
    else: url += "?limit=5"
    
    res = requests.get(url, headers=headers)
    if res.status_code == 200:
        data = res.json()
        msg = data if msg_id else next((m for m in data if "```" in m.get("content", "")), None)
        if msg:
            # Extrahiere Text zwischen den Code-Blöcken
            match = re.search(r"```\n(.*?)\n```", msg["content"], re.DOTALL)
            if match:
                content = match.group(1)
                if content and "..." not in content: # Nur wenn Log nicht gekürzt war
                    with open(LOG_FILE, "w", encoding="utf-8") as f:
                        f.write(content)

def reconstruct_drivers_from_log():
    current_drivers = []
    log_lines = read_persistent_log()
    for line in log_lines:
        if " 🟢 " in line:
            name = line.split(" 🟢 ")[1].replace(" (Waitlist)", "").replace(" (Nachgerückt)", "").strip()
            if name not in current_drivers: current_drivers.append(name)
        elif " 🔴 " in line:
            name = line.split(" 🔴 ")[1].strip()
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

# --- MAIN ---
@app.route('/')
def home():
    config = get_env_config()
    if not all([config["TOKEN_APOLLO"], config["CHAN_APOLLO"], config["CHAN_LOG"]]):
        return "Config Error", 500

    try:
        # 1. VERSUCHE WIEDERHERSTELLUNG AUS DISCORD
        restore_log_from_discord(config)
        
        headers = {"Authorization": f"Bot {config['TOKEN_APOLLO']}"}
        res = requests.get(f"[https://discord.com/api/v10/channels/](https://discord.com/api/v10/channels/){config['CHAN_APOLLO']}/messages?limit=10", headers=headers)
        apollo_msg = next((m for m in res.json() if m.get("author", {}).get("id") == APOLLO_BOT_ID and m.get("embeds")), None)
        if not apollo_msg: return "Kein Apollo-Post."

        event_title, apollo_drivers = extract_data(apollo_msg["embeds"][0])
        now = get_now()
        now_iso = now.isoformat()
        wd = now.weekday()
        grid_cap = MAX_GRIDS * DRIVERS_PER_GRID
        
        # Lock Prüfung
        is_locked = (wd == 6 and now.hour >= 18) or (wd == 0)
        if not is_locked and wd == 0 and REG_END_TIME:
            try:
                h, m = map(int, REG_END_TIME.split(":"))
                if now >= now.replace(hour=h, minute=m, second=0, microsecond=0): is_locked = True
            except: pass
        if wd == 1 and now.hour < 10: is_locked = True

        # State simulieren (da Datei nach Deploy weg)
        logged_drivers = reconstruct_drivers_from_log()
        
        # Ist es ein ID-Wechsel? (Wir prüfen gegen die letzte Zeile des Logs)
        last_log = read_persistent_log()
        is_new = False
        if not last_log or (event_title not in last_log[0] and "✨" in last_log[0]):
             # Wenn Titel im Log nicht zum aktuellen Apollo-Titel passt -> Neues Event
             is_new = True

        webhook_type = "update"
        added, removed = [], []

        if is_new or not os.path.exists(LOG_FILE):
            if os.path.exists(LOG_FILE): os.remove(LOG_FILE)
            webhook_type = "event_reset"
            write_to_persistent_log(f"{format_ts_short(now)} ✨ Event gestartet ({event_title})")
            lobby_cleanup(config)
            for idx, d in enumerate(apollo_drivers):
                icon = "🟢" if idx < grid_cap else "🟡"
                write_to_persistent_log(f"{format_ts_short(now)} {icon} {clean_for_log(d)}{'' if idx < grid_cap else ' (Waitlist)'}")
            added = apollo_drivers # Für Webhook
        else:
            # Änderungen basierend auf rekonstruiertem Log ermitteln
            added = [d for d in apollo_drivers if clean_for_log(d) not in logged_drivers]
            removed = [d for d in logged_drivers if d not in [clean_for_log(ad) for ad in apollo_drivers]]
            
            # Systemstart Zeile einfügen, falls wir gerade erst hochgefahren sind
            if "⚡ Systemstart" not in last_log[-1]:
                write_to_persistent_log(f"{format_ts_short(now)} ⚡ Systemstart ({event_title})")

            for d in added:
                idx = apollo_drivers.index(d)
                icon = "🟢" if idx < grid_cap else "🟡"
                write_to_persistent_log(f"{format_ts_short(now)} {icon} {clean_for_log(d)}{'' if idx < grid_cap else ' (Waitlist)'}")
            for d_name in removed:
                write_to_persistent_log(f"{format_ts_short(now)} 🔴 {d_name}")

        driver_count = len(apollo_drivers)
        grid_count = min(math.ceil(driver_count/DRIVERS_PER_GRID), MAX_GRIDS)

        # Webhook Sync
        if config['MAKE_WEBHOOK_URL'] and (added or removed or is_new):
            if not is_locked or is_new:
                payload = {
                    "type": webhook_type,
                    "driver_count": driver_count,
                    "drivers": [raw_for_make(d) for d in apollo_drivers],
                    "grids": grid_count,
                    "log_history": "\n".join(read_persistent_log()),
                    "timestamp": now_iso
                }
                requests.post(config['MAKE_WEBHOOK_URL'], json=payload)

        # Discord Log Update
        # Wir brauchen die msg_id aus einem temporären State oder suchen sie erneut
        res_log = send_or_edit_log(driver_count, grid_count, is_locked, config)
        return "OK"

    except Exception as e: return f"Error: {str(e)}", 500

# ... Restliche Funktionen (send_or_edit_log etc.) wie in V60 ...