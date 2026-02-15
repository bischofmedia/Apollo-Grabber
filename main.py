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
    msg = random.choice(raw.split(";")).strip()
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
    url = f"https://discord.com/api/v10/channels/{config['CHAN_LOG']}/messages"
    target_id = config.get('SET_MANUAL_LOG_ID')
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
    if not config["DISCORD_TOKEN_LOBBYCODEGRABBER"] or not config["CHAN_CODES"]: return
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

# --- TEST-LOGIK (V73 ERWEITERT) ---
def run_simulation(config):
    report = ["<html><body style='font-family:sans-serif; padding:20px;'>"]
    report.append("<h1>System-Simulation V73</h1>")
    grid_cap = config['MAX_GRIDS'] * config['DRIVERS_PER_GRID']
    dummy_now = get_now()
    
    report.append("<h3>1. Simulation: Event-Start & Warteliste</h3>")
    fake_log = [f"{format_ts_short(dummy_now)} ✨ Event gestartet (Simulation GP)"]
    drivers = [f"Fahrer_{i}" for i in range(1, grid_cap + 2)]
    
    waitlist_names = []
    for idx, d in enumerate(drivers):
        is_wl = idx >= grid_cap
        fake_log.append(f"{format_ts_short(dummy_now)} {'🟡' if is_wl else '🟢'} {d}{' (Waitlist)' if is_wl else ''}")
        if is_wl: waitlist_names.append(d)
    
    if waitlist_names:
        news = send_combined_news(config, "MSG_WAITLIST_SINGLE", "MSG_WAITLIST_SINGLE_EN", test_mode=True, driver_names=", ".join(waitlist_names))
        report.append(f"<div style='border-left:4px solid blue; padding-left:10px;'>{news.replace(chr(10), '<br>')}</div>")

    report.append("<h3>2. Simulation: Multi-Nachrücken</h3>")
    fake_log.append(f"{format_ts_short(dummy_now)} 🔴 Fahrer_1")
    fake_log.append(f"{format_ts_short(dummy_now)} 🔴 Fahrer_2")
    moved_up = ["Fahrer_61", "Fahrer_62"]
    news_move = send_combined_news(config, "MSG_MOVED_UP_MULTI", "MSG_MOVED_UP_MULTI_EN", test_mode=True, driver_names=", ".join(moved_up))
    report.append(f"<div style='border-left:4px solid green; padding-left:10px;'>{news_move.replace(chr(10), '<br>')}</div>")

    report.append("<h3>3. Simulation: Make.com Webhook Payload</h3>")
    fake_payload = {
        "type": "update",
        "driver_count": len(drivers) - 2 + len(moved_up),
        "drivers": [f"Fahrer_{i}" for i in range(3, grid_cap + 3)],
        "grids": min(math.ceil((len(drivers)-2)/config['DRIVERS_PER_GRID']), config['MAX_GRIDS']),
        "log_history": "\n".join(fake_log),
        "timestamp": dummy_now.isoformat()
    }
    report.append(f"<pre style='background:#f4f4f4; padding:10px; border:1px solid #ccc;'>{json.dumps(fake_payload, indent=4)}</pre>")

    report.append("</body></html>")
    return "".join(report)

# --- MAIN ---
@app.route('/')
def home():
    config = get_config()
    if request.args.get('test') == '1':
        return run_simulation(config)

    try:
        restore_log_from_discord(config)
        h = {"Authorization": f"Bot {config['DISCORD_TOKEN_APOLLOGRABBER']}"}
        res = requests.get(f"[https://discord.com/api/v10/channels/](https://discord.com/api/v10/channels/){config['CHAN_APOLLO']}/messages?limit=10", headers=h)
        apollo_msg = next((m for m in res.json() if m.get("author", {}).get("id") == APOLLO_BOT_ID and m.get("embeds")), None)
        if not apollo_msg: return "Waiting..."

        event_title, apollo_drivers = extract_data(apollo_msg["embeds"][0])
        now = get_now()
        grid_cap = config['MAX_GRIDS'] * config['DRIVERS_PER_GRID']
        
        # Lock Prüfung (Montag 20:45)
        is_locked = (now.weekday() == 6 and now.hour >= 18) or (now.weekday() == 0)
        if not is_locked and now.weekday() == 0 and config['REG_END_TIME']:
            try:
                hl, ml = map(int, config['REG_END_TIME'].split(":"))
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
        return "OK"
    except Exception as e: return f"Error: {str(e)}", 500

def send_or_edit_log(count, grids, is_locked, config):
    h = {"Authorization": f"Bot {config['DISCORD_TOKEN_APOLLOGRABBER']}", "Content-Type": "application/json"}
    ic = "🔒" if is_locked else "🟢"
    st = "Grids gesperrt / Locked" if is_locked else "Anmeldung geöffnet / Open"
    full_log = read_persistent_log()
    log_text = ""
    for entry in reversed(full_log):
        if len(log_text) + len(entry) + 20 > 980:
            log_text = "...\n" + log_text
            break
        log_text = entry + "\n" + log_text
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