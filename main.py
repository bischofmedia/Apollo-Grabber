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
        "ENABLE_NEWS_CLEAN": e.get("ENABLE_NEWS_CLEANUP") == "1", # Neue Variable
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
    return {"event_id": None, "drivers": [], "last_make_sync": None, "sun_msg_sent": False, "extra_msg_sent": False, "event_title": "Unbekannt", "manual_grids": None, "frozen_grids": None}

def save_state(s):
    with open(STATE_FILE, "w") as f: json.dump(s, f)

def read_persistent_log():
    if not os.path.exists(LOG_FILE): return []
    with LOG_LOCK:
        with open(LOG_FILE, "r", encoding="utf-8") as f: return [l.strip() for l in f if l.strip()]

def send_order_feedback(conf, text):
    if not conf["CHAN_ORDERS"]: return
    requests.post(f"https://discord.com/api/v10/channels/{conf['CHAN_ORDERS']}/messages", 
                  headers={"Authorization": f"Bot {conf['TOKEN_APOLLO']}"}, json={"content": text})

# --- DISCORD COMMANDS LOGIK ---
def process_discord_commands(conf, state):
    target_chan = conf["CHAN_ORDERS"] or conf["CHAN_LOG"]
    if not target_chan: return ""
    h = {"Authorization": f"Bot {conf['TOKEN_APOLLO']}"}
    url = f"https://discord.com/api/v10/channels/{target_chan}/messages?limit=10"
    res = requests.get(url, headers=h)
    test_output = ""
    if res.ok:
        for m in res.json():
            content = m.get("content", "").strip().lower()
            author_id = str(m.get("author", {}).get("id"))
            if content.startswith("/") and author_id in conf["USER_ORGA"]:
                requests.delete(f"https://discord.com/api/v10/channels/{target_chan}/messages/{m['id']}", headers=h)
                
                if content == "/help":
                    help_text = (
                        "**🛠️ Bot-Steuerung Hilfe**\n\n"
                        "`/grids=X` - Setzt die Grid-Anzahl fest (0 zum Entsperren).\n"
                        "`/clean` - Löscht eigene News, Lobby-Codes und leert Make-Tabelle.\n"
                        "`/newevent` - Erzwingt die Erkennung eines neuen Events.\n"
                        "`/test` - Dashboard-Check."
                    )
                    send_order_feedback(conf, help_text)
                elif content == "/clean":
                    news_cleanup(conf)
                    lobby_cleanup(conf)
                    if conf["MAKE_WEBHOOK"]:
                        requests.post(conf["MAKE_WEBHOOK"], json={"type": "event_reset", "timestamp": get_now().isoformat()})
                    send_order_feedback(conf, "✅ **Manuelle Säuberung:** Eigene News (falls aktiviert), Lobby-Codes und Make-Tabelle wurden zurückgesetzt.")
                elif content == "/newevent":
                    state["event_id"] = None
                    save_state(state)
                    send_order_feedback(conf, "🔄 **Manueller Reset:** Das aktuelle Event wird beim nächsten Scan neu eingelesen.")
                elif content.startswith("/grids="):
                    try:
                        val = int(content.split("=")[1])
                        state["manual_grids"] = val if val > 0 else None
                        if val == 0: state["frozen_grids"] = None
                        save_state(state)
                        msg = f"🔒 **Grid-Lock:** Festgelegt auf `{val}` Grids." if val > 0 else "🔓 **Grid-Lock:** Automatik aktiv."
                        send_order_feedback(conf, msg)
                    except: pass
                elif content == "/test":
                    test_output = "Befehl erkannt."
    return test_output

# --- CLEANUP FEATURES ---
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

# --- MAIN ---
@app.route('/')
def home():
    conf = get_env_config()
    state = load_state()
    now = get_now()
    test_msg = process_discord_commands(conf, state)
    
    try:
        api_url = f"https://discord.com/api/v10/channels/{conf['CHAN_APOLLO']}/messages?limit=10"
        res = requests.get(api_url, headers={"Authorization": f"Bot {conf['TOKEN_APOLLO']}"}, timeout=10)
        # Suchen nach der Event-Nachricht (Embed)
        apollo_msg = next((m for m in res.json() if m.get("embeds")), None)
        if not apollo_msg: return "Warte auf Apollo..."

        embed = apollo_msg["embeds"][0]
        event_title = embed.get("title", "Event")
        is_new = (state["event_id"] is None or state["event_id"] != apollo_msg["id"])
        
        if is_new:
            # Cleanup nur am Dienstag ODER wenn manuell /newevent (event_id is None) getriggert wurde
            if now.weekday() == 1 or state["event_id"] is None:
                news_cleanup(conf)
                lobby_cleanup(conf)
            
            if os.path.exists(LOG_FILE): os.remove(LOG_FILE)
            with open(LOG_FILE, "w", encoding="utf-8") as f: 
                f.write(f"{format_ts_short(now)} Event gestartet\n")
            
            if conf["MAKE_WEBHOOK"]:
                requests.post(conf["MAKE_WEBHOOK"], json={"type": "event_reset", "event_title": event_title, "timestamp": now.isoformat()})

            state = {
                "event_id": apollo_msg["id"], "event_title": event_title, "drivers": [], 
                "last_make_sync": now.isoformat(), "sun_msg_sent": False, "extra_msg_sent": False, 
                "manual_grids": None, "frozen_grids": None
            }
            save_state(state)
            return render_dashboard(state, 0, 0, False, False, 0, test_msg)

        # --- NORMALE VERARBEITUNG ---
        drivers = []
        for f in embed.get("fields", []):
            if any(k in f.get("name", "").lower() for k in ["accepted", "confirmed", "anmeldung"]):
                for line in f.get("value", "").split("\n"):
                    c = re.sub(r"^\d+[\s.)-]*", "", line).strip()
                    if c: drivers.append(c)

        is_sun_18 = (now.weekday() == 6 and now.hour >= 18)
        is_final_lock = False
        if now.weekday() == 0:
            try:
                hl, ml = map(int, conf["REG_END_TIME"].split(":"))
                if now >= now.replace(hour=hl, minute=ml, second=0, microsecond=0): is_final_lock = True
            except: pass

        count = len(drivers)
        grid_cap_base = conf["MAX_GRIDS"] * conf["DRIVERS_PER_GRID"]
        
        if is_sun_18 and state.get("frozen_grids") is None and state.get("manual_grids") is None:
            calc_grids = min(math.ceil(count / conf["DRIVERS_PER_GRID"]), conf["MAX_GRIDS"])
            if conf["ENABLE_EXTRA"] and count > grid_cap_base + conf["EXTRA_THRESH"]: calc_grids += 1
            state["frozen_grids"] = calc_grids
            save_state(state)

        is_locked = (state.get("manual_grids") is not None or state.get("frozen_grids") is not None)
        grids = state.get("manual_grids") or state.get("frozen_grids") or min(math.ceil(count / conf["DRIVERS_PER_GRID"]), conf["MAX_GRIDS"])
        if not is_locked and conf["ENABLE_EXTRA"] and count > grid_cap_base + conf["EXTRA_THRESH"]:
            grids += 1
            if conf["SW_EXTRA"] and not state.get("extra_msg_sent"):
                send_combined_news(conf, "SET_MSG_EXTRA_GRID_TEXT")
                state["extra_msg_sent"] = True

        current_cap = grids * conf["DRIVERS_PER_GRID"]
        
        log_lines = read_persistent_log()
        def get_last_action(name):
            clean_name = clean_for_log(name)
            for line in reversed(log_lines):
                if clean_name in line:
                    if "🔴" in line: return "abgemeldet"
                    if "🟢" in line or "🟡" in line: return "angemeldet"
            return "nie_gesehen"

        added = [d for d in drivers if get_last_action(d) != "angemeldet"]
        removed = [d for d in state.get("drivers", []) if d not in drivers]
        moved_up = [d for d in drivers if d in state.get("drivers", []) and drivers.index(d) < current_cap and state.get("drivers", []).index(d) >= current_cap]

        if added or removed or moved_up:
            with LOG_LOCK:
                with open(LOG_FILE, "a", encoding="utf-8") as f:
                    for d in added:
                        idx = drivers.index(d)
                        icon = "🟡" if idx >= current_cap else "🟢"
                        f.write(f"{format_ts_short(now)} {icon} {clean_for_log(d)}{' (Waitlist)' if idx >= current_cap else ''}\n")
                        if idx >= current_cap and conf["SW_WAIT"]:
                            send_combined_news(conf, "MSG_WAITLIST_SINGLE", driver_names=clean_for_log(d))
                    for d in moved_up:
                        f.write(f"{format_ts_short(now)} 🟢 {clean_for_log(d)} (Nachgerückt)\n")
                        if conf["SW_MOVE"]: send_combined_news(conf, "MSG_MOVED_UP_SINGLE", driver_names=clean_for_log(d))
                    for d in removed: f.write(f"{format_ts_short(now)} 🔴 {clean_for_log(d)}\n")

        if conf["SW_SUN"] and is_sun_18 and not state.get("sun_msg_sent"):
            free = max(0, current_cap - count)
            send_combined_news(conf, "MSG_SUNDAY_TEXT", driver_count=count, grids=grids, free_slots=free)
            state["sun_msg_sent"] = True

        if conf["MAKE_WEBHOOK"] and (added or removed or is_new):
            payload = {"type": "update", "driver_count": count, "drivers": [raw_for_make(d) for d in drivers], "grids": grids, "log_history": "\n".join(read_persistent_log()), "timestamp": now.isoformat()}
            requests.post(conf["MAKE_WEBHOOK"], json=payload)
            state["last_make_sync"] = now.isoformat()

        send_or_edit_log(conf, state, count, grids, is_final_lock, is_locked, current_cap)
        state["drivers"] = drivers
        save_state(state)
        return render_dashboard(state, count, grids, is_final_lock, is_locked, current_cap, test_msg)
    except Exception as e: return f"Error: {str(e)}", 500

def render_dashboard(state, count, grids, is_final, is_locked, cap, test_msg=""):
    log_entries = read_persistent_log()[-20:]
    log_html = "".join([f"<div style='border-bottom:1px solid #eee; padding:2px;'>{l}</div>" for l in reversed(log_entries)])
    s_txt, s_col = ("GESCHLOSSEN", "#f44336") if is_final else (("WARTELISTE", "#ff9800") if count >= cap else ("OFFEN", "#4CAF50"))
    ov_tag = " <span style='font-size:0.6em; color:red;'>(LOCK 🔒)</span>" if is_locked else ""
    t_msg = f"<div style='background:yellow; padding:5px; margin-bottom:10px;'>{test_msg}</div>" if test_msg else ""
    return f"""
    <html><head><title>Apollo Monitor</title><meta http-equiv="refresh" content="30"></head>
    <body style="font-family:sans-serif; background:#f0f2f5; padding:20px;">
        <div style="max-width:800px; margin:auto; background:white; padding:20px; border-radius:10px; box-shadow:0 2px 10px rgba(0,0,0,0.1);">
            {t_msg}<h2 style="margin-top:0;">🏁 Apollo Event Monitor</h2>
            <div style="padding:15px; background:#fafafa; border-left:5px solid {s_col}; margin-bottom:20px;">
                <b>Event:</b> {state.get('event_title', 'Unbekannt')} | <span style="color:{s_col}; font-weight:bold;">● {s_txt}</span>
            </div>
            <div style="display:grid; grid-template-columns: repeat(3, 1fr); gap:10px; margin-bottom:20px; text-align:center;">
                <div style="background:#e3f2fd; padding:15px; border-radius:8px;">Fahrer: <br><b>{count}</b></div>
                <div style="background:#e8f5e9; padding:15px; border-radius:8px;">Grids: <br><b>{grids}{ov_tag}</b></div>
                <div style="background:#fff3e0; padding:15px; border-radius:8px;">Sync: <br><b>{state.get('last_make_sync','--').split('T')[-1][:5]}</b></div>
            </div>
            <div style="background:#1e1e1e; color:#d4d4d4; padding:15px; border-radius:8px; font-family:monospace; font-size:0.9em; height:300px; overflow-y:auto;">{log_html}</div>
        </div></body></html>"""

def send_combined_news(conf, key_base, **kwargs):
    if not conf["CHAN_NEWS"]: return
    msg_de, msg_en = os.environ.get(key_base, ""), os.environ.get(key_base + "_EN", "")
    if not msg_de: return
    def pick(t):
        opts = [o.strip() for o in t.split(";") if o.strip()]
        return random.choice(opts) if opts else t
    full_text = f"🇩🇪 {pick(msg_de).format(**kwargs)}"
    if msg_en: full_text += f"\n\n🇬🇧 {pick(msg_en).format(**kwargs)}"
    requests.post(f"https://discord.com/api/v10/channels/{conf['CHAN_NEWS']}/messages", 
                  headers={"Authorization": f"Bot {conf['TOKEN_APOLLO']}"}, json={"content": full_text})

def send_or_edit_log(conf, state, count, grids, is_final, is_locked, cap):
    if not conf["CHAN_LOG"]: return
    icon, status = ("🔴", "Anmeldung geschlossen") if is_final else (("🟡", "Warteliste aktiv") if count >= cap else ("🟢", "Anmeldung geöffnet"))
    sync_ts = format_ts_short(datetime.datetime.fromisoformat(state["last_make_sync"]).astimezone(BERLIN_TZ)) if state.get("last_make_sync") else "--"
    grid_display = f"{grids} 🔒" if is_locked else f"{grids}"
    content = (f"**{state.get('event_title', 'Event')}**\n{icon} **{status}**\nFahrer: `{count}` | Grids: `{grid_display}`\n\n"
               f"```\n" + "\n".join(read_persistent_log()[-15:]) + "```\n"
               f"*Stand: {format_ts_short(get_now())}* | *Sync: {sync_ts}*")
    requests.patch(f"https://discord.com/api/v10/channels/{conf['CHAN_LOG']}/messages/{conf['MANUAL_LOG_ID']}", headers={"Authorization": f"Bot {conf['TOKEN_APOLLO']}"}, json={"content": content})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))