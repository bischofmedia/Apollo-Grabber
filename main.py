import os, requests, json, re, math, datetime, pytz, threading, time, random
from flask import Flask

# ==============================================================================
# BLOCK 1: KONFIGURATION
# ==============================================================================
def get_env_config():
    def c_id(v): return re.sub(r'[^0-9]', '', str(v)) if v else ""
    e = os.environ
    return {
        "TOKEN_APOLLO": e.get("DISCORD_TOKEN_APOLLOGRABBER"),
        "TOKEN_LOBBY": e.get("DISCORD_TOKEN_LOBBYCODEGRABBER"),
        "CHAN_APOLLO": c_id(e.get("CHAN_APOLLO")),
        "CHAN_LOG": c_id(e.get("CHAN_LOG")),
        "CHAN_NEWS": c_id(e.get("CHAN_NEWS")),
        "CHAN_CODES": c_id(e.get("CHAN_CODES")),
        "CHAN_ORDERS": c_id(e.get("CHAN_ORDERS")),
        "MAKE_WEBHOOK": e.get("MAKE_WEBHOOK_URL"),
        "USER_ORGA": [c_id(u) for u in e.get("USER_ID_ORGA", "").split(";") if u.strip()],
        "DRIVERS_PER_GRID": int(e.get("DRIVERS_PER_GRID", 15)),
        "MAX_GRIDS": int(e.get("MAX_GRIDS", 4)),
        "ENABLE_NEWS_CLEAN": e.get("ENABLE_NEWS_CLEANUP") == "1",
        "SW_MOVE": e.get("SET_MSG_MOVED_UP_TEXT") == "1",
        "SW_WAIT": e.get("ENABLE_WAITLIST_MSG") == "1",
        "MSG_LOBBY": e.get("MSG_LOBBYCODES", "")
    }

LOG_FILE = "event_log.txt"
STATE_FILE = "state.json"
BERLIN_TZ = pytz.timezone("Europe/Berlin")
LOG_LOCK = threading.Lock()

app = Flask(__name__)

# ==============================================================================
# BLOCK 2: HILFSFUNKTIONEN
# ==============================================================================
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
    return {"event_id": None, "drivers": [], "last_make_sync": None, "event_title": "Unbekannt", "manual_grids": None, "active_log_id": None, "last_cap": 0}

def save_state(s):
    with open(STATE_FILE, "w") as f: json.dump(s, f)

def read_persistent_log():
    with LOG_LOCK:
        if not os.path.exists(LOG_FILE): return []
        with open(LOG_FILE, "r", encoding="utf-8") as f: 
            return [l.strip() for l in f if l.strip()]

def get_bot_user_id(token):
    try:
        res = requests.get("https://discord.com/api/v10/users/@me", headers={"Authorization": f"Bot {token}"})
        if res.ok: return str(res.json().get("id"))
    except: pass
    return None

# ==============================================================================
# BLOCK 3: NEWS-SYSTEM
# ==============================================================================
def send_combined_news(conf, key_base, **kwargs):
    if not conf["CHAN_NEWS"]: return
    msg_de = os.environ.get(key_base, "")
    msg_en = os.environ.get(key_base.replace("MSG_", "MSG_EN_"), "")
    if not msg_de: return
    def pick(t):
        opts = [o.strip() for o in t.split(";") if o.strip()]
        return random.choice(opts) if opts else t
    full_text = f"🇩🇪 {pick(msg_de).format(**kwargs)}"
    if msg_en: full_text += f"\n\n🇬🇧 {pick(msg_en).format(**kwargs)}"
    requests.post(f"https://discord.com/api/v10/channels/{conf['CHAN_NEWS']}/messages", 
                  headers={"Authorization": f"Bot {conf['TOKEN_APOLLO']}"}, json={"content": full_text})

# ==============================================================================
# BLOCK 4: CLEANUP
# ==============================================================================
def news_cleanup(conf):
    if not conf["ENABLE_NEWS_CLEAN"] or not conf["CHAN_NEWS"]: return
    my_id = get_bot_user_id(conf["TOKEN_APOLLO"])
    if not my_id: return
    h = {"Authorization": f"Bot {conf['TOKEN_APOLLO']}"}
    url = f"https://discord.com/api/v10/channels/{conf['CHAN_NEWS']}/messages"
    res = requests.get(f"{url}?limit=100", headers=h)
    if res.ok:
        for m in res.json():
            if str(m.get("author", {}).get("id")) == my_id:
                requests.delete(f"{url}/{m['id']}", headers=h)
                time.sleep(0.3)

def lobby_cleanup(conf):
    if not conf["TOKEN_LOBBY"] or not conf["CHAN_CODES"]: return
    my_id = get_bot_user_id(conf["TOKEN_LOBBY"])
    if not my_id: return
    h = {"Authorization": f"Bot {conf['TOKEN_LOBBY']}"}
    url = f"https://discord.com/api/v10/channels/{conf['CHAN_CODES']}/messages"
    res = requests.get(f"{url}?limit=50", headers=h)
    if res.ok:
        for m in res.json():
            if str(m.get("author", {}).get("id")) == my_id:
                requests.delete(f"{url}/{m['id']}", headers=h)
        time.sleep(0.2)
    if conf["MSG_LOBBY"]: requests.post(url, headers=h, json={"content": conf["MSG_LOBBY"]})

# ==============================================================================
# BLOCK 5: COMMANDS
# ==============================================================================
def process_discord_commands(conf, state):
    target_chan = conf["CHAN_ORDERS"] or conf["CHAN_LOG"]
    if not target_chan: return False, False
    h = {"Authorization": f"Bot {conf['TOKEN_APOLLO']}"}
    url = f"https://discord.com/api/v10/channels/{target_chan}/messages?limit=10"
    res = requests.get(url, headers=h)
    force_reset = False
    force_sync = False
    if res.ok:
        for m in res.json():
            content = m.get("content", "").strip().lower()
            author = m.get("author", {})
            author_id = str(author.get("id"))
            user_name = author.get("global_name") or author.get("username") or "Unbekannt"
            
            if content.startswith("!") and author_id in conf["USER_ORGA"]:
                requests.delete(f"https://discord.com/api/v10/channels/{target_chan}/messages/{m['id']}", headers=h)
                
                if content == "!help":
                    help_msg = (
                        f"**🛠️ RTC Apollo Grabber V2 - Befehlsübersicht** (Aufruf durch {user_name})\n\n"
                        "`!grids=X` : Setzt die Gridanzahl manuell fest (z.B. `!grids=3`).\n"
                        "`!clean`   : KOMPLETTER RESET. Löscht Logs und State (wie Deploy).\n"
                        "`!sync`    : Erzwingt sofortige Übertragung an Make.com.\n"
                        "`!newevent`: Startet das Protokoll für das aktuelle Event neu."
                    )
                    send_order_feedback(conf, help_msg)
                elif content == "!clean":
                    news_cleanup(conf)
                    lobby_cleanup(conf)
                    if os.path.exists(STATE_FILE): os.remove(STATE_FILE)
                    if os.path.exists(LOG_FILE): os.remove(LOG_FILE)
                    force_reset = True
                    send_order_feedback(conf, f"🧹 **Kaltstart:** {user_name} hat alle Daten gelöscht.")
                elif content == "!sync":
                    force_sync = True
                    send_order_feedback(conf, f"🔄 **Sync:** {user_name} hat Übertragung ausgelöst.")
                elif content == "!newevent":
                    force_reset = True
                    send_order_feedback(conf, f"🔄 **Reset:** {user_name} hat einen Reset erzwungen.")
                elif content.startswith("!grids="):
                    try:
                        val = int(content.split("=")[1])
                        state["manual_grids"] = None if val == 0 else min(val, conf["MAX_GRIDS"])
                        save_state(state)
                        send_order_feedback(conf, f"🔒 **Grids:** Auf {state['manual_grids'] or 'Auto'} gesetzt.")
                    except: pass
    return force_reset, force_sync

def send_order_feedback(conf, text):
    if not conf["CHAN_ORDERS"]: return
    requests.post(f"https://discord.com/api/v10/channels/{conf['CHAN_ORDERS']}/messages", 
                  headers={"Authorization": f"Bot {conf['TOKEN_APOLLO']}"}, json={"content": text})

# ==============================================================================
# BLOCK 6: HAUPT-LOGIK
# ==============================================================================
@app.route('/')
def home():
    conf = get_env_config()
    state = load_state()
    now = get_now()
    
    is_tuesday = (now.weekday() == 1 and now.hour == 9 and now.minute == 59)
    force_reset, force_sync = process_discord_commands(conf, state)
    
    # Echter Reset wie nach Deploy
    if force_reset:
        state = {"event_id": None, "drivers": [], "last_make_sync": None, "event_title": "Unbekannt", "manual_grids": None, "active_log_id": state.get("active_log_id"), "last_cap": 0}

    should_reset_data = is_tuesday or force_reset

    try:
        api_url = f"https://discord.com/api/v10/channels/{conf['CHAN_APOLLO']}/messages?limit=10"
        res = requests.get(api_url, headers={"Authorization": f"Bot {conf['TOKEN_APOLLO']}"})
        apollo_msg = next((m for m in res.json() if m.get("embeds")), None)
        if not apollo_msg: return "Warte auf Apollo..."
        
        embed = apollo_msg["embeds"][0]
        event_title = embed.get("title", "Event")

        # Log-Datei Check
        if should_reset_data or not os.path.exists(LOG_FILE):
            if os.path.exists(LOG_FILE): os.remove(LOG_FILE)
            with open(LOG_FILE, "w", encoding="utf-8") as f: 
                f.write(f"{format_ts_short(now)} Event gestartet\n")
            state["event_id"] = apollo_msg["id"]
            state["event_title"] = event_title
            state["drivers"] = []
            save_state(state)

        drivers = []
        for f in embed.get("fields", []):
            if any(k in f.get("name", "").lower() for k in ["accepted", "confirmed", "anmeldung"]):
                for line in f.get("value", "").split("\n"):
                    c = re.sub(r"^\d+[\s.)-]*", "", line).strip()
                    if c: drivers.append(c)

        count = len(drivers)
        is_locked = state.get("manual_grids") is not None
        grids = state.get("manual_grids") if is_locked else min(math.ceil(count / conf["DRIVERS_PER_GRID"]), conf["MAX_GRIDS"])
        current_cap = grids * conf["DRIVERS_PER_GRID"]
        
        old_drivers = state.get("drivers", [])
        new_log_entries = []

        if not old_drivers and drivers:
            for d in drivers:
                idx = drivers.index(d)
                icon = "🟡" if idx >= current_cap else "🟢"
                new_log_entries.append(f"{format_ts_short(now)} {icon} {clean_for_log(d)}{' (Waitlist)' if idx >= current_cap else ''}")
        else:
            added = [d for d in drivers if d not in old_drivers]
            removed = [d for d in old_drivers if d not in drivers]
            for d in added:
                idx = drivers.index(d)
                icon = "🟡" if idx >= current_cap else "🟢"
                new_log_entries.append(f"{format_ts_short(now)} {icon} {clean_for_log(d)}{' (Waitlist)' if idx >= current_cap else ''}")
                if idx >= current_cap and conf["SW_WAIT"]:
                    send_combined_news(conf, "MSG_WAITLIST_SINGLE", driver_names=clean_for_log(d))
            for d in removed:
                new_log_entries.append(f"{format_ts_short(now)} 🔴 {clean_for_log(d)}")

            old_cap = state.get("last_cap", 0)
            if current_cap != old_cap and old_cap > 0:
                moved_to_wait, moved_to_grids = [], []
                if current_cap < old_cap:
                    for i, d in enumerate(drivers):
                        if current_cap <= i < old_cap:
                            moved_to_wait.append(clean_for_log(d))
                            new_log_entries.append(f"{format_ts_short(now)} 🟠 Warteliste: {clean_for_log(d)}")
                    if moved_to_wait and conf["SW_WAIT"]:
                        send_combined_news(conf, "MSG_WAITLIST_MULTI" if len(moved_to_wait)>1 else "MSG_WAITLIST_SINGLE", driver_names=", ".join(moved_to_wait))
                else:
                    for i, d in enumerate(drivers):
                        if old_cap <= i < current_cap:
                            moved_to_grids.append(clean_for_log(d))
                            new_log_entries.append(f"{format_ts_short(now)} 🔵 Nachgerückt: {clean_for_log(d)}")
                    if moved_to_grids and conf["SW_MOVE"]:
                        send_combined_news(conf, "MSG_MOVED_UP_MULTI" if len(moved_to_grids)>1 else "MSG_MOVED_UP_SINGLE", driver_names=", ".join(moved_to_grids))

        if new_log_entries:
            with LOG_LOCK:
                with open(LOG_FILE, "a", encoding="utf-8") as f:
                    for entry in new_log_entries: f.write(entry + "\n")

        if conf["MAKE_WEBHOOK"] and (new_log_entries or should_reset_data or current_cap != state.get("last_cap") or force_sync):
            payload = {"type": "event_reset" if should_reset_data else "update", "driver_count": count, "drivers": [raw_for_make(d) for d in drivers], "grids": grids, "log_history": "\n".join(read_persistent_log()), "timestamp": now.isoformat()}
            try:
                m_res = requests.post(conf["MAKE_WEBHOOK"], json=payload, timeout=10)
                if m_res.ok: state["last_make_sync"] = now.isoformat()
            except: pass

        # --- DISPLAY LOG ---
        full_log = read_persistent_log()
        display_log = ""
        max_chars = 1300
        for entry in reversed(full_log):
            if len(display_log) + len(entry) + 5 > max_chars:
                display_log = "[...]\n" + display_log
                break
            display_log = entry + "\n" + display_log

        icon_stat, txt_stat = ("🟢", "Anmeldung geöffnet") if count < current_cap else ("🟡", "Warteliste aktiv")
        sync_time = format_ts_short(datetime.datetime.fromisoformat(state['last_make_sync'])) if state.get('last_make_sync') else "--"
        grid_display = f"{grids} 🔒" if is_locked else f"{grids}"
        
        log_content = (f"**{event_title}**\n{icon_stat} **{txt_stat}**\nFahrer: `{count}` | Grids: `{grid_display}`\n"
                       f"```\n{display_log}```\n"
                       f"*Stand: {format_ts_short(now)} | Sync: {sync_time}*")
        
        active_id = state.get("active_log_id")
        log_url = f"https://discord.com/api/v10/channels/{conf['CHAN_LOG']}/messages"
        log_reachable = False
        if active_id:
            res_check = requests.get(f"{log_url}/{active_id}", headers={"Authorization": f"Bot {conf['TOKEN_APOLLO']}"})
            if res_check.ok: log_reachable = True

        if not log_reachable:
            new_log = requests.post(log_url, headers={"Authorization": f"Bot {conf['TOKEN_APOLLO']}"}, json={"content": log_content})
            if new_log.ok: state["active_log_id"] = new_log.json()['id']
        else:
            requests.patch(f"{log_url}/{active_id}", headers={"Authorization": f"Bot {conf['TOKEN_APOLLO']}"}, json={"content": log_content})

        state["drivers"] = drivers
        state["last_cap"] = current_cap
        save_state(state)
        return render_dashboard(state, count, grids, is_locked)
    except Exception as e: return f"Error: {str(e)}", 500

# ==============================================================================
# BLOCK 7: DASHBOARD
# ==============================================================================
def render_dashboard(state, count, grids, is_locked):
    log_entries = read_persistent_log()[-50:]
    log_html = "".join([f"<div style='border-bottom:1px solid #333; padding:4px 2px;'>{l}</div>" for l in reversed(log_entries)])
    ov_tag = " <span style='color:red;'>🔒</span>" if is_locked else ""
    return f"""<html><head><title>Apollo Grabber V2</title><meta http-equiv="refresh" content="30"></head>
    <body style="font-family:sans-serif; background:#f0f2f5; padding:20px;">
        <div style="max-width:900px; margin:auto; background:white; padding:20px; border-radius:10px;">
            <h2 style="margin-top:0;">🏁 Apollo Grabber V2</h2>
            <div style="padding:10px; background:#eee; margin-bottom:15px;"><b>Event:</b> {state.get('event_title')}</div>
            <div style="display:grid; grid-template-columns: 1fr 1fr 1fr; gap:10px; text-align:center;">
                <div style="background:#e3f2fd; padding:10px;">Fahrer: <b>{count}</b></div>
                <div style="background:#e8f5e9; padding:10px;">Grids: <b>{grids}{ov_tag}</b></div>
                <div style="background:#fff3e0; padding:10px;">ID: {state.get('active_log_id','--')}</div>
            </div>
            <div style="background:#1e1e1e; color:#00ff00; padding:15px; margin-top:20px; height:450px; overflow-y:auto; font-family:monospace;">{log_html}</div>
        </div></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))