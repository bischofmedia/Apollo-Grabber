import os, requests, json, re, math, datetime, pytz, threading, time, random
from flask import Flask, request

# ---------- KONFIGURATION ----------
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
        "EXTRA_THRESH": int(e.get("EXTRA_GRID_THRESHOLD", 10)),
        "REG_END_TIME": e.get("REGISTRATION_END_TIME", "20:45").strip(),
        "MANUAL_LOG_ID": c_id(e.get("SET_MANUAL_LOG_ID")),
        "SW_EXTRA": e.get("SET_MSG_EXTRA_GRID_TEXT") == "1",
        "SW_MOVE": e.get("SET_MSG_MOVED_UP_TEXT") == "1",
        "SW_SUN": e.get("ENABLE_SUNDAY_MSG") == "1",
        "SW_WAIT": e.get("ENABLE_WAITLIST_MSG") == "1",
        "ENABLE_EXTRA": e.get("ENABLE_EXTRA_GRID") == "1",
        "ENABLE_NEWS_CLEAN": e.get("ENABLE_NEWS_CLEANUP") == "1",
        "MSG_LOBBY": e.get("MSG_LOBBYCODES", "")
    }

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

def get_bot_user_id(token):
    try:
        res = requests.get("https://discord.com/api/v10/users/@me", headers={"Authorization": f"Bot {token}"})
        if res.ok: return str(res.json().get("id"))
    except: pass
    return None

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f: return json.load(f)
        except: pass
    return {"event_id": None, "drivers": [], "last_make_sync": None, "sun_msg_sent": False, "extra_msg_sent": False, "event_title": "Unbekannt", "manual_grids": None, "frozen_grids": None, "active_log_id": None}

def save_state(s):
    with open(STATE_FILE, "w") as f: json.dump(s, f)

def read_persistent_log():
    if not os.path.exists(LOG_FILE): return []
    with LOG_LOCK:
        if not os.path.exists(LOG_FILE): return []
        with open(LOG_FILE, "r", encoding="utf-8") as f: return [l.strip() for l in f if l.strip()]

def send_order_feedback(conf, text):
    if not conf["CHAN_ORDERS"]: return
    requests.post(f"https://discord.com/api/v10/channels/{conf['CHAN_ORDERS']}/messages", 
                  headers={"Authorization": f"Bot {conf['TOKEN_APOLLO']}"}, json={"content": text})

# --- CLEANUP FEATURES ---
def news_cleanup(conf, current_log_id):
    if not conf["ENABLE_NEWS_CLEAN"] or not conf["CHAN_NEWS"]: return
    my_id = get_bot_user_id(conf["TOKEN_APOLLO"])
    if not my_id: return
    h = {"Authorization": f"Bot {conf['TOKEN_APOLLO']}"}
    url = f"https://discord.com/api/v10/channels/{conf['CHAN_NEWS']}/messages"
    res = requests.get(f"{url}?limit=100", headers=h)
    if res.ok:
        for m in res.json():
            mid = str(m.get("id"))
            # STRIKTER SCHUTZ: Log-Nachricht niemals löschen
            if mid == conf["MANUAL_LOG_ID"] or mid == current_log_id: continue
            if str(m.get("author", {}).get("id")) == my_id:
                requests.delete(f"{url}/{mid}", headers=h)
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

# --- DISCORD COMMANDS LOGIK ---
def process_discord_commands(conf, state):
    target_chan = conf["CHAN_ORDERS"] or conf["CHAN_LOG"]
    if not target_chan: return False
    h = {"Authorization": f"Bot {conf['TOKEN_APOLLO']}"}
    url = f"https://discord.com/api/v10/channels/{target_chan}/messages?limit=10"
    res = requests.get(url, headers=h)
    force_reset = False
    if res.ok:
        for m in res.json():
            content = m.get("content", "").strip().lower()
            author_id = str(m.get("author", {}).get("id"))
            if content.startswith("/") and author_id in conf["USER_ORGA"]:
                requests.delete(f"https://discord.com/api/v10/channels/{target_chan}/messages/{m['id']}", headers=h)
                if content == "/clean":
                    news_cleanup(conf, state.get("active_log_id"))
                    lobby_cleanup(conf)
                    force_reset = True
                elif content == "/newevent":
                    force_reset = True
                elif content.startswith("/grids="):
                    try:
                        val = int(content.split("=")[1])
                        state["manual_grids"] = val if val > 0 else None
                        if val == 0: state["frozen_grids"] = None
                        save_state(state)
                        send_order_feedback(conf, f"🔒 Grids manuell auf `{val}` gesetzt.")
                    except: pass
    return force_reset

# --- MAIN ---
@app.route('/')
def home():
    conf = get_env_config()
    state = load_state()
    now = get_now()
    
    target_log_id = conf["MANUAL_LOG_ID"] or state.get("active_log_id")
    log_exists = False
    if target_log_id:
        res_log = requests.get(f"https://discord.com/api/v10/channels/{conf['CHAN_LOG']}/messages/{target_log_id}", 
                               headers={"Authorization": f"Bot {conf['TOKEN_APOLLO']}"})
        if res_log.ok: log_exists = True

    is_tuesday_reset = (now.weekday() == 1 and now.hour == 9 and now.minute == 59)
    force_reset = process_discord_commands(conf, state)
    should_reset = is_tuesday_reset or force_reset or not log_exists

    try:
        api_url = f"https://discord.com/api/v10/channels/{conf['CHAN_APOLLO']}/messages?limit=10"
        res = requests.get(api_url, headers={"Authorization": f"Bot {conf['TOKEN_APOLLO']}"}, timeout=10)
        apollo_msg = next((m for m in res.json() if m.get("embeds")), None)
        if not apollo_msg: return "Warte auf Apollo..."
        
        embed = apollo_msg["embeds"][0]
        event_title = embed.get("title", "Event")

        if should_reset:
            news_cleanup(conf, target_log_id)
            lobby_cleanup(conf)
            if os.path.exists(LOG_FILE): os.remove(LOG_FILE)
            with open(LOG_FILE, "w", encoding="utf-8") as f: f.write(f"{format_ts_short(now)} Event gestartet\n")
            if conf["MAKE_WEBHOOK"]:
                requests.post(conf["MAKE_WEBHOOK"], json={"type": "event_reset", "event_title": event_title})
            state = {"event_id": apollo_msg["id"], "event_title": event_title, "drivers": [], "last_make_sync": now.isoformat(), "sun_msg_sent": False, "extra_msg_sent": False, "manual_grids": None, "frozen_grids": None, "active_log_id": target_log_id}
            save_state(state)

        drivers = []
        for f in embed.get("fields", []):
            if any(k in f.get("name", "").lower() for k in ["accepted", "confirmed", "anmeldung"]):
                for line in f.get("value", "").split("\n"):
                    c = re.sub(r"^\d+[\s.)-]*", "", line).strip()
                    if c: drivers.append(c)

        count = len(drivers)
        grids = state.get("manual_grids") or state.get("frozen_grids") or min(math.ceil(count / conf["DRIVERS_PER_GRID"]), conf["MAX_GRIDS"])
        current_cap = grids * conf["DRIVERS_PER_GRID"]
        
        added = [d for d in drivers if d not in state.get("drivers", [])]
        removed = [d for d in state.get("drivers", []) if d not in drivers]
        if (added or removed) and not should_reset:
            with LOG_LOCK:
                with open(LOG_FILE, "a", encoding="utf-8") as f:
                    for d in added: f.write(f"{format_ts_short(now)} 🟢 {clean_for_log(d)}\n")
                    for d in removed: f.write(f"{format_ts_short(now)} 🔴 {clean_for_log(d)}\n")

        icon, status = ("🟢", "Anmeldung geöffnet")
        log_content = f"**{event_title}**\n{icon} **{status}**\nFahrer: `{count}` | Grids: `{grids}`\n\n```\n" + "\n".join(read_persistent_log()[-15:]) + "```"
        
        if not log_exists:
            new_log = requests.post(f"https://discord.com/api/v10/channels/{conf['CHAN_LOG']}/messages", 
                                   headers={"Authorization": f"Bot {conf['TOKEN_APOLLO']}"}, json={"content": log_content})
            if new_log.ok:
                new_id = new_log.json()['id']
                state["active_log_id"] = new_id
                save_state(state)
                send_order_feedback(conf, f"🆕 Neues Log erstellt! ID: `{new_id}` (Bitte in Render eintragen!)")
        else:
            requests.patch(f"https://discord.com/api/v10/channels/{conf['CHAN_LOG']}/messages/{target_log_id}", 
                           headers={"Authorization": f"Bot {conf['TOKEN_APOLLO']}"}, json={"content": log_content})

        state["drivers"] = drivers
        save_state(state)
        return render_dashboard(state, count, grids, False, (state.get("manual_grids") is not None), current_cap)
    except Exception as e: return f"Error: {str(e)}", 500

def render_dashboard(state, count, grids, is_final, is_locked, cap):
    log_entries = read_persistent_log()[-15:]
    log_html = "".join([f"<div style='border-bottom:1px solid #eee; padding:2px;'>{l}</div>" for l in reversed(log_entries)])
    s_col = "#4CAF50"
    ov_tag = " <span style='color:red;'>🔒</span>" if is_locked else ""
    return f"""
    <html><head><title>Apollo Monitor</title><meta http-equiv="refresh" content="30"></head>
    <body style="font-family:sans-serif; background:#f0f2f5; padding:20px;">
        <div style="max-width:800px; margin:auto; background:white; padding:20px; border-radius:10px; box-shadow:0 2px 10px rgba(0,0,0,0.1);">
            <h2 style="margin-top:0;">🏁 Apollo Event Monitor V109</h2>
            <div style="padding:15px; background:#fafafa; border-left:5px solid {s_col}; margin-bottom:20px;">
                <b>Event:</b> {state.get('event_title', 'Unbekannt')}
            </div>
            <div style="display:grid; grid-template-columns: repeat(3, 1fr); gap:10px; margin-bottom:20px; text-align:center;">
                <div style="background:#e3f2fd; padding:15px; border-radius:8px;">Fahrer: <b>{count}</b></div>
                <div style="background:#e8f5e9; padding:15px; border-radius:8px;">Grids: <b>{grids}{ov_tag}</b></div>
                <div style="background:#fff3e0; padding:15px; border-radius:8px;">Log-ID: <b style="font-size:0.7em;">{state.get('active_log_id','--')}</b></div>
            </div>
            <div style="background:#1e1e1e; color:#d4d4d4; padding:15px; border-radius:8px; font-family:monospace; height:250px; overflow-y:auto;">{log_html}</div>
        </div></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))