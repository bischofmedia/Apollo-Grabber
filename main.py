import os, requests, json, re, math, datetime, pytz, time, random
from flask import Flask, request

# --- KONFIGURATION ---
def get_config():
    conf = {k: os.environ.get(k, "") for k in os.environ}
    return {
        **conf,
        "DRIVERS_PER_GRID": int(conf.get("DRIVERS_PER_GRID", 15)),
        "MAX_GRIDS": int(conf.get("MAX_GRIDS", 4)),
        "EXTRA_THRESHOLD": int(conf.get("EXTRA_GRID_THRESHOLD", 10)),
        "MIN_GRIDS_NEWS": int(conf.get("SET_MIN_GRIDS_MSG", 2)),
        "SW_EXTRA": conf.get("SET_MSG_EXTRA_GRID_TEXT") == "1",
        "SW_FULL": conf.get("SET_MSG_GRID_FULL_TEXT") == "1",
        "SW_MOVE": conf.get("SET_MSG_MOVED_UP_TEXT") == "1",
        "SW_SUNDAY": conf.get("ENABLE_SUNDAY_MSG") == "1",
        "SW_WAIT": conf.get("ENABLE_WAITLIST_MSG") == "1",
        "ENABLE_EXTRA_LOGIC": conf.get("ENABLE_EXTRA_GRID") == "1"
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

def clean_for_log(n): return n.replace("\\", "").replace(">>>", "").replace(">", "").strip()
def raw_for_make(n): return n.replace(">>>", "").replace(">", "").strip()

def get_random_msg(key, **kwargs):
    raw = os.environ.get(key, "")
    if not raw: return ""
    msgs = [m.strip() for m in raw.split(";") if m.strip()]
    if not msgs: return ""
    msg = random.choice(msgs)
    try: return msg.format(**kwargs)
    except: return msg

def send_combined_news(config, key_de, key_en, test_mode=False, **kwargs):
    msg_de = get_random_msg(key_de, **kwargs)
    msg_en = get_random_msg(key_en, **kwargs)
    if not msg_de: return ""
    text = f"📢 **NEWS-POST:**\n{msg_de}" + (f"\n\n{msg_en}" if msg_en else "")
    if test_mode: return text
    
    url = f"https://discord.com/api/v10/channels/{config['CHAN_NEWS']}/messages"
    h = {"Authorization": f"Bot {config['DISCORD_TOKEN_APOLLOGRABBER']}"}
    requests.post(url, headers=h, json={"content": text})
    return text

def write_to_persistent_log(line):
    with open(LOG_FILE, "a", encoding="utf-8") as f: f.write(line + "\n")

def read_persistent_log():
    if not os.path.exists(LOG_FILE): return []
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f.readlines() if line.strip()]

def restore_log_from_discord(config):
    if os.path.exists(LOG_FILE): return
    h = {"Authorization": f"Bot {config['DISCORD_TOKEN_APOLLOGRABBER']}"}
    target_id = config.get('SET_MANUAL_LOG_ID')
    url = f"https://discord.com/api/v10/channels/{config['CHAN_LOG']}/messages"
    if target_id: url += f"/{target_id}"
    else: url += "?limit=10"
    try:
        res = requests.get(url, headers=h, timeout=5)
        if res.status_code == 200:
            data = res.json()
            msg = data if target_id else next((m for m in data if "```" in m.get("content", "")), None)
            if msg:
                match = re.search(r"```\n(.*?)\n```", msg["content"], re.DOTALL)
                if match:
                    content = match.group(1).replace("...", "").strip()
                    if content:
                        with open(LOG_FILE, "w", encoding="utf-8") as f: f.write(content + "\n")
    except: pass

def lobby_cleanup(config):
    h = {"Authorization": f"Bot {config['DISCORD_TOKEN_LOBBYCODEGRABBER']}"}
    url = f"[https://discord.com/api/v10/channels/](https://discord.com/api/v10/channels/){config['CHAN_CODES']}/messages"
    res = requests.get(f"{url}?limit=100", headers=h)
    if res.status_code == 200:
        for m in res.json():
            requests.delete(f"{url}/{m['id']}", headers=h)
            time.sleep(0.4)
    requests.post(url, headers=h, json={"content": config["MSG_LOBBYCODES"]})

def extract_data(embed):
    title = embed.get("title", "Event")
    drivers = []
    for field in embed.get("fields", []):
        if any(kw in field.get("name", "").lower() for kw in ["accepted", "anmeldung", "confirmed", "zusagen"]):
            for line in field.get("value", "").split("\n"):
                c = re.sub(r"^\d+[\s.)-]*", "", line).strip()
                if c and "grid" not in c.lower() and len(c) > 1: drivers.append(c)
    return title, drivers

def reconstruct_drivers_from_log(lines=None):
    current = []
    source = lines if lines is not None else read_persistent_log()
    for line in source:
        if " 🟢 " in line:
            name = line.split(" 🟢 ")[1].replace(" (Waitlist)", "").replace(" (Nachgerückt)", "").strip()
            if name not in current: current.append(name)
        elif " 🔴 " in line:
            name = line.split(" 🔴 ")[1].strip()
            if name in current: current.remove(name)
    return current

# --- TEST-LOGIK ---
def run_simulation(config):
    report = ["<html><body style='font-family:sans-serif; padding:20px;'><h1>System-Simulation V80</h1>"]
    dummy_now = get_now()
    fake_log = [f"{format_ts_short(dummy_now)} ✨ Simulation Start", f"{format_ts_short(dummy_now)} 🟢 Test_Fahrer_1"]
    drivers = ["Test_Fahrer_1", "Test_Fahrer_2"]

    report.append("<h3>Vollständiger Make.com Payload:</h3>")
    full_payload = {
        "type": "update",
        "driver_count": len(drivers),
        "drivers": [raw_for_make(d) for d in drivers],
        "grids": 1,
        "log_history": "\n".join(fake_log),
        "timestamp": dummy_now.isoformat()
    }
    report.append(f"<pre style='background:#222; color:#0f0; padding:15px;'>{json.dumps(full_payload, indent=4)}</pre>")
    report.append("</body></html>")
    return "".join(report)

# --- MAIN ---
@app.route('/')
def home():
    config = get_config()
    if request.args.get('test') == '1': return run_simulation(config)

    try:
        restore_log_from_discord(config)
        h = {"Authorization": f"Bot {config['DISCORD_TOKEN_APOLLOGRABBER']}"}
        url = f"[https://discord.com/api/v10/channels/](https://discord.com/api/v10/channels/){config['CHAN_APOLLO']}/messages?limit=10"
        res = requests.get(url, headers=h)
        
        apollo_msg = next((m for m in res.json() if m.get("author", {}).get("id") == APOLLO_BOT_ID and m.get("embeds")), None)
        if not apollo_msg: return "Warte auf Apollo..."

        event_title, apollo_drivers = extract_data(apollo_msg["embeds"][0])
        now = get_now()
        grid_cap = config['MAX_GRIDS'] * config['DRIVERS_PER_GRID']
        
        is_locked = (now.weekday() == 6 and now.hour >= 18) or (now.weekday() == 0)
        if not is_locked and now.weekday() == 0 and config.get('REGISTRATION_END_TIME'):
            try:
                hl, ml = map(int, config['REGISTRATION_END_TIME'].split(":"))
                if now >= now.replace(hour=hl, minute=ml, second=0, microsecond=0): is_locked = True
            except: pass
        if now.weekday() == 1 and now.hour < 10: is_locked = True

        log_lines = read_persistent_log()
        logged_drivers = reconstruct_drivers_from_log()
        is_new = not log_lines or (event_title not in log_lines[0] and "✨" in log_lines[0])
        
        added = [d for d in apollo_drivers if clean_for_log(d) not in logged_drivers]
        removed = [d for d in logged_drivers if d not in [clean_for_log(ad) for ad in apollo_drivers]]

        if is_new:
            if os.path.exists(LOG_FILE): os.remove(LOG_FILE)
            write_to_persistent_log(f"{format_ts_short(now)} ✨ Event gestartet ({event_title})")
            lobby_cleanup(config)
            for idx, d in enumerate(apollo_drivers):
                icon = "🟢" if idx < grid_cap else "🟡"
                write_to_persistent_log(f"{format_ts_short(now)} {icon} {clean_for_log(d)}{'' if idx < grid_cap else ' (Waitlist)'}")
        else:
            if log_lines and "⚡" not in log_lines[-1]: write_to_persistent_log(f"{format_ts_short(now)} ⚡ Systemstart")
            
            waitlist_names = []
            for d in added:
                idx = apollo_drivers.index(d)
                is_wl = idx >= grid_cap
                write_to_persistent_log(f"{format_ts_short(now)} {'🟡' if is_wl else '🟢'} {clean_for_log(d)}{' (Waitlist)' if is_wl else ''}")
                if is_wl: waitlist_names.append(clean_for_log(d))
            
            if config['SW_WAIT'] and waitlist_names:
                k = "MSG_WAITLIST_MULTI" if len(waitlist_names) > 1 else "MSG_WAITLIST_SINGLE"
                send_combined_news(config, k, k+"_EN", driver_names=", ".join(waitlist_names))
            
            for d_name in removed: write_to_persistent_log(f"{format_ts_short(now)} 🔴 {d_name}")

            moved_up = []
            for d in apollo_drivers:
                d_clean = clean_for_log(d)
                if d_clean in logged_drivers:
                    idx_now = apollo_drivers.index(d)
                    last_entry = next((l for l in reversed(log_lines) if d_clean in l), "")
                    if idx_now < grid_cap and "🟡" in last_entry:
                        write_to_persistent_log(f"{format_ts_short(now)} 🟢 {d_clean} (Nachgerückt)")
                        moved_up.append(d_clean)
            
            if config['SW_MOVE'] and moved_up:
                k = "MSG_MOVED_UP_MULTI" if len(moved_up) > 1 else "MSG_MOVED_UP_SINGLE"
                send_combined_news(config, k, k+"_EN", driver_names=", ".join(moved_up))

        count = len(apollo_drivers)
        grids = min(math.ceil(count/config['DRIVERS_PER_GRID']), config['MAX_GRIDS'])
        
        if config['ENABLE_EXTRA_LOGIC'] and count > grid_cap + config['EXTRA_THRESHOLD']:
            grids += 1
            if config['SW_EXTRA'] and not any("🏗️" in l for l in log_lines[-5:]):
                send_combined_news(config, "MSG_EXTRA_GRID_TEXT", "MSG_EXTRA_GRID_TEXT_EN")

        if config['SW_FULL'] and count > 0 and count % config['DRIVERS_PER_GRID'] == 0:
            cur = count // config['DRIVERS_PER_GRID']
            if cur >= config['MIN_GRIDS_NEWS'] and not any(f"Grids: `{cur}`" in l for l in log_lines[-3:]):
                send_combined_news(config, "MSG_GRID_FULL_TEXT", "MSG_GRID_FULL_TEXT_EN", full_grids=cur)

        if config['SW_SUNDAY'] and now.weekday() == 6 and now.hour == 18 and now.minute < 10:
            free = (grids * config['DRIVERS_PER_GRID']) - count
            send_combined_news(config, "MSG_SUNDAY_TEXT", "MSG_SUNDAY_TEXT_EN", driver_count=count, grids=grids, free_slots=max(0, free))

        if config['MAKE_WEBHOOK_URL'] and (added or removed or is_new):
            payload = {
                "type": "event_reset" if is_new else "update",
                "driver_count": count, "drivers": [raw_for_make(d) for d in apollo_drivers],
                "grids": grids, "log_history": "\n".join(read_persistent_log()),
                "timestamp": now.isoformat()
            }
            requests.post(config['MAKE_WEBHOOK_URL'], json=payload)

        send_or_edit_log(count, grids, is_locked, config)
        return "OK"import os
import requests
import json
import re
import math
import datetime
import pytz
import time
import random
import threading
from flask import Flask, request

# ---------- GLOBAL ----------
APOLLO_BOT_ID = "475744554910351370"
LOG_FILE = "event_log.txt"
BERLIN_TZ = pytz.timezone("Europe/Berlin")
LOG_LOCK = threading.Lock()

app = Flask(__name__)

# ---------- CONFIG ----------
def safe_int(val, default):
    try:
        return int(val)
    except (TypeError, ValueError):
        return default

def get_config():
    env = os.environ
    return {
        "DRIVERS_PER_GRID": safe_int(env.get("DRIVERS_PER_GRID"), 15),
        "MAX_GRIDS": safe_int(env.get("MAX_GRIDS"), 4),
        "EXTRA_THRESHOLD": safe_int(env.get("EXTRA_GRID_THRESHOLD"), 10),
        "MIN_GRIDS_NEWS": safe_int(env.get("SET_MIN_GRIDS_MSG"), 2),

        "SW_EXTRA": env.get("SET_MSG_EXTRA_GRID_TEXT") == "1",
        "SW_FULL": env.get("SET_MSG_GRID_FULL_TEXT") == "1",
        "SW_MOVE": env.get("SET_MSG_MOVED_UP_TEXT") == "1",
        "SW_SUNDAY": env.get("ENABLE_SUNDAY_MSG") == "1",
        "SW_WAIT": env.get("ENABLE_WAITLIST_MSG") == "1",
        "ENABLE_EXTRA_LOGIC": env.get("ENABLE_EXTRA_GRID") == "1",

        "CHAN_NEWS": env.get("CHAN_NEWS", ""),
        "CHAN_CODES": env.get("CHAN_CODES", ""),
        "CHAN_APOLLO": env.get("CHAN_APOLLO", ""),
        "CHAN_LOG": env.get("CHAN_LOG", ""),

        "DISCORD_TOKEN_APOLLOGRABBER": env.get("DISCORD_TOKEN_APOLLOGRABBER", ""),
        "DISCORD_TOKEN_LOBBYCODEGRABBER": env.get("DISCORD_TOKEN_LOBBYCODEGRABBER", ""),

        "MAKE_WEBHOOK_URL": env.get("MAKE_WEBHOOK_URL", ""),
        "REGISTRATION_END_TIME": env.get("REGISTRATION_END_TIME", ""),

        "SET_MANUAL_LOG_ID": env.get("SET_MANUAL_LOG_ID"),
        "MSG_LOBBYCODES": env.get("MSG_LOBBYCODES", "")
    }

# ---------- HELPERS ----------
def get_now():
    return datetime.datetime.now(BERLIN_TZ)

def format_ts_short(dt):
    return dt.strftime("%d.%m %H:%M")

def clean_for_log(name):
    return re.sub(r"[>\\]", "", name).strip()

def raw_for_make(name):
    return name.replace(">>>", "").replace(">", "").strip()

def safe_request(method, url, **kwargs):
    try:
        res = requests.request(method, url, timeout=6, **kwargs)
        if not res.ok:
            print("HTTP ERROR:", res.status_code, url)
            return None
        return res
    except requests.RequestException as e:
        print("REQUEST ERROR:", e)
        return None

# ---------- LOGGING ----------
def write_log(line):
    with LOG_LOCK:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")

def read_log():
    with LOG_LOCK:
        if not os.path.exists(LOG_FILE):
            return []
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip()]

# ---------- DISCORD ----------
def send_news(config, key, **fmt):
    msg = os.environ.get(key, "")
    if not msg:
        return

    try:
        msg = msg.format(**fmt)
    except:
        pass

    url = f"https://discord.com/api/v10/channels/{config['CHAN_NEWS']}/messages"
    safe_request(
        "POST",
        url,
        headers={"Authorization": f"Bot {config['DISCORD_TOKEN_APOLLOGRABBER']}"},
        json={"content": msg},
    )

# ---------- DATA PARSE ----------
def extract_data(embed):
    drivers = []
    for f in embed.get("fields", []):
        name = f.get("name", "").lower()
        if any(k in name for k in ("accepted", "confirmed", "anmeldung")):
            for line in f.get("value", "").split("\n"):
                cleaned = re.sub(r"^\d+[.)-]*\s*", "", line).strip()
                if cleaned:
                    drivers.append(cleaned)
    return embed.get("title", "Event"), drivers

# ---------- MAIN ----------
@app.route("/")
def home():
    config = get_config()

    try:
        # Fetch Apollo message
        url = f"https://discord.com/api/v10/channels/{config['CHAN_APOLLO']}/messages?limit=10"
        res = safe_request(
            "GET",
            url,
            headers={"Authorization": f"Bot {config['DISCORD_TOKEN_APOLLOGRABBER']}"},
        )
        if not res:
            return "Discord unavailable"

        data = res.json()

        msg = next(
            (
                m
                for m in data
                if m.get("author", {}).get("id") == APOLLO_BOT_ID
                and m.get("embeds")
            ),
            None,
        )

        if not msg:
            return "Waiting for Apollo..."

        title, drivers = extract_data(msg["embeds"][0])
        drivers_clean = [clean_for_log(d) for d in drivers]

        log = read_log()
        known = set(
            l.split(" ", 2)[-1]
            for l in log
            if "🟢" in l or "🟡" in l
        )

        now = get_now()

        # detect changes
        added = [d for d in drivers_clean if d not in known]
        removed = [d for d in known if d not in drivers_clean]

        cap = config["DRIVERS_PER_GRID"] * config["MAX_GRIDS"]

        # write updates
        for i, d in enumerate(drivers_clean):
            if d in added:
                icon = "🟢" if i < cap else "🟡"
                write_log(f"{format_ts_short(now)} {icon} {d}")

        for d in removed:
            write_log(f"{format_ts_short(now)} 🔴 {d}")

        # grids
        count = len(drivers_clean)
        grids = min(
            math.ceil(count / config["DRIVERS_PER_GRID"]),
            config["MAX_GRIDS"],
        )

        # webhook
        if config["MAKE_WEBHOOK_URL"] and (added or removed):
            payload = {
                "type": "update",
                "drivers": drivers_clean,
                "count": count,
                "grids": grids,
                "timestamp": now.isoformat(),
            }
            safe_request("POST", config["MAKE_WEBHOOK_URL"], json=payload)

        return "OK"

    except Exception as e:
        return f"ERROR: {e}", 500


# ---------- RUN ----------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=safe_int(os.environ.get("PORT"), 5000))

    except Exception as e: return f"Error: {str(e)}", 500

def send_or_edit_log(count, grids, is_locked, config):
    h = {"Authorization": f"Bot {config['DISCORD_TOKEN_APOLLOGRABBER']}", "Content-Type": "application/json"}
    ic = "🔒" if is_locked else "🟢"
    st = "Grids gesperrt / Locked" if is_locked else "Anmeldung geöffnet / Open"
    log_text = "\n".join(read_persistent_log()[-15:])
    legend = "🟢 Angemeldet / Registered\n🟡 Warteliste / Waitlist\n🔴 Abgemeldet / Withdrawn"
    formatted = (f"{ic} **{st}**\nFahrer: `{count}` | Grids: `{grids}`\n\n"
                 f"```\n{log_text or 'Initialisiere...'}```\n"
                 f"*Stand: {format_ts_short(get_now())}*\n\n**Legende:**\n{legend}")
    tid = config.get('SET_MANUAL_LOG_ID')
    url = f"[https://discord.com/api/v10/channels/](https://discord.com/api/v10/channels/){config['CHAN_LOG']}/messages"
    if tid: requests.patch(f"{url}/{tid}", headers=h, json={"content": formatted})
    else: requests.post(url, headers=h, json={"content": formatted})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))