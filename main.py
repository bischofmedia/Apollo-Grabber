import os, requests, json, re, math, datetime, pytz, threading, time, random
from flask import Flask, request

# ---------- CONFIG & URL-SCHUTZ ----------
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
        "MAKE_WEBHOOK": e.get("MAKE_WEBHOOK_URL"),
        "DRIVERS_PER_GRID": int(e.get("DRIVERS_PER_GRID", 15)),
        "MAX_GRIDS": int(e.get("MAX_GRIDS", 4)),
        "EXTRA_THRESH": int(e.get("EXTRA_GRID_THRESHOLD", 10)),
        "REG_END_TIME": e.get("REGISTRATION_END_TIME", "20:45").strip(),
        "MANUAL_LOG_ID": c_id(e.get("SET_MANUAL_LOG_ID")),
        "SW_EXTRA": e.get("SET_MSG_EXTRA_GRID_TEXT") == "1",
        "SW_FULL": e.get("SET_MSG_GRID_FULL_TEXT") == "1",
        "SW_MOVE": e.get("SET_MSG_MOVED_UP_TEXT") == "1",
        "SW_SUN": e.get("ENABLE_SUNDAY_MSG") == "1",
        "SW_WAIT": e.get("ENABLE_WAITLIST_MSG") == "1",
        "ENABLE_EXTRA": e.get("ENABLE_EXTRA_GRID") == "1",
        "MSG_LOBBY": e.get("MSG_LOBBYCODES", "")
    }

APOLLO_BOT_ID = "475744554910351370"
LOG_FILE = "event_log.txt"
STATE_FILE = "state.json"
BERLIN_TZ = pytz.timezone("Europe/Berlin")
LOG_LOCK = threading.Lock()

app = Flask(__name__)

# ---------- HELFER ----------
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
    msg = random.choice(msgs) if msgs else raw
    try: return msg.format(**kwargs)
    except: return msg

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f: return json.load(f)
        except: pass
    return {"event_id": None, "drivers": [], "last_make_sync": None, "sun_msg_sent": False, "event_title": "Unbekannt"}

def save_state(s):
    with open(STATE_FILE, "w") as f: json.dump(s, f)

def read_persistent_log():
    if not os.path.exists(LOG_FILE): return []
    with LOG_LOCK:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip()]

# ---------- FEATURES ----------
def send_combined_news(conf, key_base, **kwargs):
    if not conf["CHAN_NEWS"]: return
    msg_de = get_random_msg(key_base, **kwargs)
    msg_en = get_random_msg(key_base + "_EN", **kwargs)
    if not msg_de: return
    full_text = f"🇩🇪 {msg_de}" + (f"\n\n🇬🇧 {msg_en}" if msg_en else "")
    h = {"Authorization": f"Bot {conf['TOKEN_APOLLO']}"}
    requests.post(f"https://discord.com/api/v10/channels/{conf['CHAN_NEWS']}/messages", headers=h, json={"content": full_text})

def lobby_cleanup(conf):
    if not conf["TOKEN_LOBBY"] or not conf["CHAN_CODES"]: return
    h = {"Authorization": f"Bot {conf['TOKEN_LOBBY']}"}
    url = f"https://discord.com/api/v10/channels/{conf['CHAN_CODES']}/messages"
    res = requests.get(f"{url}?limit=100", headers=h)
    if res.ok:
        for m in res.json():
            requests.delete(f"{url}/{m['id']}", headers=h)
            time.sleep(0.4)
    if conf["MSG_LOBBY"]:
        requests.post(url, headers=h, json={"content": conf["MSG_LOBBY"]})

# ---------- MAIN ----------
@app.route('/')
def home():
    conf = get_env_config()
    if not conf["TOKEN_APOLLO"] or not conf["CHAN_APOLLO"]: return "Config Error", 500

    try:
        api_url = "https://discord.com/api/v10/channels/" + conf["CHAN_APOLLO"] + "/messages?limit=10"
        h_apollo = {"Authorization": "Bot " + conf["TOKEN_APOLLO"]}
        res = requests.get(api_url, headers=h_apollo, timeout=10)
        
        apollo_msg = next((m for m in res.json() if str(m.get("author", {}).get("id")) == APOLLO_BOT_ID and m.get("embeds")), None)
        if not apollo_msg: return "Warte auf Apollo Nachricht..."

        embed = apollo_msg["embeds"][0]
        event_title, drivers = embed.get("title", "Event"), []
        for f in embed.get("fields", []):
            if any(k in f.get("name", "").lower() for k in ["accepted", "confirmed", "anmeldung"]):
                for line in f.get("value", "").split("\n"):
                    c = re.sub(r"^\d+[\s.)-]*", "", line).strip()
                    if c: drivers.append(c)

        state, now = load_state(), get_now()
        grid_cap = conf["MAX_GRIDS"] * conf["DRIVERS_PER_GRID"]
        is_new = (state.get("event_id") and state["event_id"] != apollo_msg["id"])
        
        # Zeit-Logik
        is_sun_18 = (now.weekday() == 6 and now.hour >= 18)
        is_locked = is_sun_18 or (now.weekday() == 0)
        if not is_locked and now.weekday() == 0:
            try:
                hl, ml = map(int, conf["REG_END_TIME"].split(":"))
                if now >= now.replace(hour=hl, minute=ml, second=0, microsecond=0): is_locked = True
            except: pass

        if is_new or not os.path.exists(LOG_FILE):
            if os.path.exists(LOG_FILE): os.remove(LOG_FILE)
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.write(f"{format_ts_short(now)} ✨ Event: {event_title}\n")
            lobby_cleanup(conf)
            state = {"event_id": apollo_msg["id"], "event_title": event_title, "drivers": [], "last_make_sync": None, "sun_msg_sent": False}

        old_drivers = state.get("drivers", [])
        added = [d for d in drivers if d not in old_drivers]
        removed = [d for d in old_drivers if d not in drivers]
        moved_up = [d for d in drivers if d in old_drivers and drivers.index(d) < grid_cap and old_drivers.index(d) >= grid_cap]

        if added or removed or moved_up:
            with LOG_LOCK:
                with open(LOG_FILE, "a", encoding="utf-8") as f:
                    for d in added:
                        idx = drivers.index(d)
                        icon = "🟡" if idx >= grid_cap else "🟢"
                        f.write(f"{format_ts_short(now)} {icon} {clean_for_log(d)}{' (Waitlist)' if idx >= grid_cap else ''}\n")
                    for d in moved_up:
                        f.write(f"{format_ts_short(now)} 🟢 {clean_for_log(d)} (Nachgerückt)\n")
                    for d in removed:
                        f.write(f"{format_ts_short(now)} 🔴 {clean_for_log(d)}\n")

            if conf["SW_WAIT"] and any(drivers.index(d) >= grid_cap for d in added):
                wl_names = [clean_for_log(d) for d in added if drivers.index(d) >= grid_cap]
                send_combined_news(conf, "MSG_WAITLIST_MULTI" if len(wl_names) > 1 else "MSG_WAITLIST_SINGLE", driver_names=", ".join(wl_names))

            if conf["SW_MOVE"] and moved_up:
                send_combined_news(conf, "MSG_MOVED_UP_MULTI" if len(moved_up) > 1 else "MSG_MOVED_UP_SINGLE", driver_names=", ".join([clean_for_log(d) for d in moved_up]))

        # Sonntag 18:00 News
        if conf["SW_SUN"] and is_sun_18 and not state.get("sun_msg_sent", False):
            count = len(drivers)
            grids = min(math.ceil(count / conf["DRIVERS_PER_GRID"]), conf["MAX_GRIDS"])
            free = (grids * conf["DRIVERS_PER_GRID"]) - count
            send_combined_news(conf, "MSG_SUNDAY_TEXT", driver_count=count, grids=grids, free_slots=max(0, free))
            state["sun_msg_sent"] = True

        # Webhook & Sync
        count = len(drivers)
        grids = min(math.ceil(count / conf["DRIVERS_PER_GRID"]), conf["MAX_GRIDS"])
        if conf["ENABLE_EXTRA"] and count > grid_cap + conf["EXTRA_THRESH"]: grids += 1

        if conf["MAKE_WEBHOOK"] and (added or removed or is_new):
            payload = {"type": "event_reset" if is_new else "update", "driver_count": count, "drivers": [raw_for_make(d) for d in drivers], "grids": grids, "log_history": "\n".join(read_persistent_log()), "timestamp": now.isoformat()}
            requests.post(conf["MAKE_WEBHOOK"], json=payload)
            state["last_make_sync"] = now.isoformat()

        send_or_edit_log(conf, state, count, grids, is_locked, grid_cap)
        state["drivers"] = drivers
        save_state(state)
        
        # BROWSER AUSGABE (DASHBOARD)
        return render_browser_dashboard(conf, state, count, grids, is_locked)
    except Exception as e: return f"Systemfehler: {str(e)}", 500

def render_browser_dashboard(conf, state, count, grids, is_locked):
    log_entries = read_persistent_log()[-20:]
    log_html = "".join([f"<div style='border-bottom:1px solid #eee; padding:2px;'>{l}</div>" for l in reversed(log_entries)])
    
    status_color = "#f44336" if is_locked else "#4CAF50"
    status_text = "GESPERRT (Locked)" if is_locked else "OFFEN (Open)"
    sync_time = state.get('last_make_sync', 'Noch nie').split('T')[-1][:8] if state.get('last_make_sync') else '--'

    html = f"""
    <html>
    <head><title>Apollo Monitor</title><meta http-equiv="refresh" content="30"></head>
    <body style="font-family:sans-serif; background:#f0f2f5; padding:20px;">
        <div style="max-width:800px; margin:auto; background:white; padding:20px; border-radius:10px; box-shadow:0 2px 10px rgba(0,0,0,0.1);">
            <h2 style="margin-top:0;">🏁 Apollo Event Monitor</h2>
            <div style="display:flex; justify-content:space-between; margin-bottom:20px; padding:15px; background:#fafafa; border-radius:8px;">
                <div><b>Event:</b> {state.get('event_title', 'Lade...')}</div>
                <div style="color:{status_color}; font-weight:bold;">● {status_text}</div>
            </div>
            <div style="display:grid; grid-template-columns: repeat(3, 1fr); gap:10px; margin-bottom:20px; text-align:center;">
                <div style="background:#e3f2fd; padding:15px; border-radius:8px;">Fahrer: <br><b style="font-size:1.5em;">{count}</b></div>
                <div style="background:#e8f5e9; padding:15px; border-radius:8px;">Grids: <br><b style="font-size:1.5em;">{grids}</b></div>
                <div style="background:#fff3e0; padding:15px; border-radius:8px;">Letzter Sync: <br><b style="font-size:1.2em;">{sync_time}</b></div>
            </div>
            <h3>Verlauf (Letzte 20):</h3>
            <div style="background:#1e1e1e; color:#d4d4d4; padding:15px; border-radius:8px; font-family:monospace; font-size:0.9em; height:300px; overflow-y:auto;">
                {log_html}
            </div>
            <div style="margin-top:20px; font-size:0.8em; color:#888; text-align:center;">
                Build V88 | Automatische Aktualisierung alle 30s
            </div>
        </div>
    </body>
    </html>
    """
    return html

def send_or_edit_log(conf, state, count, grids, is_locked, grid_cap):
    if not conf["CHAN_LOG"]: return
    log_text = "\n".join(read_persistent_log()[-15:])
    if is_locked:
        icon, status = ("🟡", "Anmeldung auf Warteliste / Waitlist registration") if count >= grid_cap else ("🔒", "Grids gesperrt / Locked")
    else:
        icon, status = "🟢", "Anmeldung geöffnet / Open"

    sync_ts = format_ts_short(datetime.datetime.fromisoformat(state["last_make_sync"]).astimezone(BERLIN_TZ)) if state.get("last_make_sync") else "--"
    content = (f"{icon} **{status}**\nFahrer: `{count}` | Grids: `{grids}`\n\n"
               f"```\n{log_text}```\n"
               f"*Stand: {format_ts_short(get_now())}* | *Sync: {sync_ts}*")
    
    h = {"Authorization": "Bot " + conf["TOKEN_APOLLO"]}
    mid = conf["MANUAL_LOG_ID"]
    if mid: requests.patch(f"https://discord.com/api/v10/channels/{conf['CHAN_LOG']}/messages/{mid}", headers=h, json={"content": content})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))