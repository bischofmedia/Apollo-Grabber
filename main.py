import os, requests, json, re, math, datetime, pytz, threading, time
from flask import Flask

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
        "MAX_GRIDS": int(e.get("MAX_GRIDS", 4)), # Hier ist die Grenze hinterlegt
        "MANUAL_LOG_ID": c_id(e.get("SET_MANUAL_LOG_ID")),
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
    return {"event_id": None, "drivers": [], "last_make_sync": None, "event_title": "Unbekannt", "manual_grids": None, "active_log_id": None}

def save_state(s):
    with open(STATE_FILE, "w") as f: json.dump(s, f)

def read_persistent_log():
    with LOG_LOCK:
        if not os.path.exists(LOG_FILE): return []
        with open(LOG_FILE, "r", encoding="utf-8") as f: 
            return [l.strip() for l in f if l.strip()]

def send_order_feedback(conf, text):
    if not conf["CHAN_ORDERS"]: return
    requests.post(f"https://discord.com/api/v10/channels/{conf['CHAN_ORDERS']}/messages", 
                  headers={"Authorization": f"Bot {conf['TOKEN_APOLLO']}"}, json={"content": text})

# --- CLEANUP ---
def news_cleanup(conf, state_log_id):
    if not conf["ENABLE_NEWS_CLEAN"] or not conf["CHAN_NEWS"]: return
    my_id = get_bot_user_id(conf["TOKEN_APOLLO"])
    if not my_id: return
    h = {"Authorization": f"Bot {conf['TOKEN_APOLLO']}"}
    url = f"https://discord.com/api/v10/channels/{conf['CHAN_NEWS']}/messages"
    res = requests.get(f"{url}?limit=100", headers=h)
    protected = [str(conf["MANUAL_LOG_ID"]), str(state_log_id)]
    if res.ok:
        for m in res.json():
            mid = str(m.get("id"))
            if mid in protected: continue
            if str(m.get("author", {}).get("id")) == my_id:
                requests.delete(f"{url}/{mid}", headers=h)
                time.sleep(0.3)

# --- COMMANDS ---
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
            if content.startswith("!") and author_id in conf["USER_ORGA"]:
                requests.delete(f"https://discord.com/api/v10/channels/{target_chan}/messages/{m['id']}", headers=h)
                
                if content == "!help":
                    help_msg = (
                        "**🛠️ RTC Apollo Grabber V2 - Befehlsübersicht**\n\n"
                        "`!grids=X` : Setzt die Gridanzahl manuell fest (z.B. `!grids=3`).\n"
                        "            *Hinweis: `!grids=0` hebt die Sperre auf und aktiviert die Automatik.*\n"
                        "`!clean`   : Löscht eigene Nachrichten im News-Kanal, bereinigt die Lobby-Codes und setzt die Make.com-Tabelle zurück.\n"
                        "`!newevent`: Erzwingt einen Reset und startet die Protokollierung für das aktuelle Apollo-Event neu."
                    )
                    send_order_feedback(conf, help_msg)
                
                elif content == "!clean":
                    news_cleanup(conf, state.get("active_log_id"))
                    force_reset = True
                    send_order_feedback(conf, "🧹 **Manuelle Säuberung:** Eigene News-Beiträge werden entfernt, Lobby-Codes bereinigt und Make.com für den Tabellen-Reset getriggert.")
                
                elif content == "!newevent":
                    force_reset = True
                    send_order_feedback(conf, "🔄 **Manueller Event-Neustart:** Die bisherigen Fahrerdaten und das Log wurden zurückgesetzt. Ein neues Event-Protokoll wird initialisiert.")
                
                elif content.startswith("!grids="):
                    try:
                        val = int(content.split("=")[1])
                        if val == 0:
                            state["manual_grids"] = None
                            save_state(state)
                            send_order_feedback(conf, "🔓 **Grid-Sperre aufgehoben:** Die Anzahl der Grids wird nun wieder automatisch berechnet.")
                        else:
                            # Berücksichtigung von MAX_GRIDS
                            final_val = min(val, conf["MAX_GRIDS"])
                            state["manual_grids"] = final_val
                            save_state(state)
                            limit_info = f" (begrenzt auf das Maximum von {conf['MAX_GRIDS']})" if val > conf["MAX_GRIDS"] else ""
                            send_order_feedback(conf, f"🔒 **Grid-Sperre aktiv:** Die Anzahl wurde fest auf `{final_val}` Grids gesetzt{limit_info}.")
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

    is_tuesday = (now.weekday() == 1 and now.hour == 9 and now.minute == 59)
    force_reset = process_discord_commands(conf, state)
    should_reset_data = is_tuesday or force_reset

    try:
        api_url = f"https://discord.com/api/v10/channels/{conf['CHAN_APOLLO']}/messages?limit=10"
        res = requests.get(api_url, headers={"Authorization": f"Bot {conf['TOKEN_APOLLO']}"})
        apollo_msg = next((m for m in res.json() if m.get("embeds")), None)
        if not apollo_msg: return "Warte auf Apollo..."
        
        embed = apollo_msg["embeds"][0]
        event_title = embed.get("title", "Event")

        if should_reset_data:
            news_cleanup(conf, target_log_id)
            if os.path.exists(LOG_FILE): os.remove(LOG_FILE)
            with open(LOG_FILE, "w", encoding="utf-8") as f: 
                f.write(f"{format_ts_short(now)} Event gestartet\n")
            state = {"event_id": apollo_msg["id"], "event_title": event_title, "drivers": [], "last_make_sync": None, "manual_grids": None, "active_log_id": target_log_id}
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
        
        added = [d for d in drivers if d not in state.get("drivers", [])]
        removed = [d for d in state.get("drivers", []) if d not in drivers]
        
        if (added or removed) and not should_reset_data:
            with LOG_LOCK:
                with open(LOG_FILE, "a", encoding="utf-8") as f:
                    for d in added: f.write(f"{format_ts_short(now)} 🟢 {clean_for_log(d)}\n")
                    for d in removed: f.write(f"{format_ts_short(now)} 🔴 {clean_for_log(d)}\n")

        if conf["MAKE_WEBHOOK"] and (added or removed or should_reset_data):
            payload = {
                "type": "event_reset" if should_reset_data else "update",
                "driver_count": count,
                "drivers": [raw_for_make(d) for d in drivers],
                "grids": grids,
                "log_history": "\n".join(read_persistent_log()),
                "timestamp": now.isoformat()
            }
            try:
                m_res = requests.post(conf["MAKE_WEBHOOK"], json=payload, timeout=10)
                if m_res.ok:
                    state["last_make_sync"] = now.isoformat()
            except: pass

        sync_time = format_ts_short(datetime.datetime.fromisoformat(state['last_make_sync'])) if state.get('last_make_sync') else "--"
        grid_display = f"{grids} 🔒" if is_locked else f"{grids}"
        
        log_content = (
            f"**{event_title}**\n🟢 **Anmeldung geöffnet**\n"
            f"Fahrer: `{count}` | Grids: `{grid_display}`\n"
            f"```\n" + "\n".join(read_persistent_log()[-15:]) + "```\n"
            f"*Stand: {format_ts_short(now)} | Sync: {sync_time}*"
        )
        
        if not log_exists:
            new_log = requests.post(f"https://discord.com/api/v10/channels/{conf['CHAN_LOG']}/messages", 
                                   headers={"Authorization": f"Bot {conf['TOKEN_APOLLO']}"}, json={"content": log_content})
            if new_log.ok:
                state["active_log_id"] = new_log.json()['id']
        else:
            requests.patch(f"https://discord.com/api/v10/channels/{conf['CHAN_LOG']}/messages/{target_log_id}", 
                           headers={"Authorization": f"Bot {conf['TOKEN_APOLLO']}"}, json={"content": log_content})

        state["drivers"] = drivers
        save_state(state)
        return render_dashboard(state, count, grids, is_locked)
    except Exception as e: return f"Error: {str(e)}", 500

def render_dashboard(state, count, grids, is_locked):
    log_entries = read_persistent_log()[-50:]
    log_html = "".join([f"<div style='border-bottom:1px solid #333; padding:4px 2px;'>{l}</div>" for l in reversed(log_entries)])
    ov_tag = " <span style='color:red;'>🔒</span>" if is_locked else ""
    return f"""
    <html><head><title>Apollo Grabber V2</title><meta http-equiv="refresh" content="30"></head>
    <body style="font-family:sans-serif; background:#f0f2f5; padding:20px;">
        <div style="max-width:900px; margin:auto; background:white; padding:20px; border-radius:10px;">
            <h2 style="margin-top:0;">🏁 Apollo Grabber V2</h2>
            <div style="padding:10px; background:#eee; margin-bottom:15px;"><b>Event:</b> {state.get('event_title')}</div>
            <div style="display:grid; grid-template-columns:1fr 1fr 1fr; gap:10px; text-align:center;">
                <div style="background:#e3f2fd; padding:10px;">Fahrer: <b>{count}</b></div>
                <div style="background:#e8f5e9; padding:10px;">Grids: <b>{grids}{ov_tag}</b></div>
                <div style="background:#fff3e0; padding:10px;">ID: {state.get('active_log_id','--')}</div>
            </div>
            <div style="background:#1e1e1e; color:#00ff00; padding:15px; margin-top:20px; height:450px; overflow-y:auto; font-family:monospace;">{log_html}</div>
        </div></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))