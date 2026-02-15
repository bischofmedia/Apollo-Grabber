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

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f: return json.load(f)
        except: pass
    return {"event_id": None, "drivers": [], "last_make_sync": None, "sun_msg_sent": False, "extra_msg_sent": False}

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
    msg_de = os.environ.get(key_base, "")
    msg_en = os.environ.get(key_base + "_EN", "")
    if not msg_de: return
    def pick(t): 
        opts = [o.strip() for o in t.split(";") if o.strip()]
        return random.choice(opts) if opts else t
    full_text = f"🇩🇪 {pick(msg_de).format(**kwargs)}"
    if msg_en: full_text += f"\n\n🇬🇧 {pick(msg_en).format(**kwargs)}"
    requests.post(f"https://discord.com/api/v10/channels/{conf['CHAN_NEWS']}/messages", 
                  headers={"Authorization": f"Bot {conf['TOKEN_APOLLO']}"}, json={"content": full_text})

def lobby_cleanup(conf):
    if not conf["TOKEN_LOBBY"] or not conf["CHAN_CODES"]: return
    h = {"Authorization": f"Bot {conf['TOKEN_LOBBY']}"}
    url = f"https://discord.com/api/v10/channels/{conf['CHAN_CODES']}/messages"
    res = requests.get(f"{url}?limit=100", headers=h)
    if res.ok:
        for m in res.json(): requests.delete(f"{url}/{m['id']}", headers=h)
        time.sleep(0.4)
    if conf["MSG_LOBBY"]: requests.post(url, headers=h, json={"content": conf["MSG_LOBBY"]})

# ---------- MAIN ----------
@app.route('/')
def home():
    conf = get_env_config()
    try:
        api_url = f"https://discord.com/api/v10/channels/{conf['CHAN_APOLLO']}/messages?limit=10"
        res = requests.get(api_url, headers={"Authorization": f"Bot {conf['TOKEN_APOLLO']}"}, timeout=10)
        apollo_msg = next((m for m in res.json() if str(m.get("author", {}).get("id")) == APOLLO_BOT_ID and m.get("embeds")), None)
        if not apollo_msg: return "Warte auf Apollo..."

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
        
        # Lock Logik
        is_sun_18 = (now.weekday() == 6 and now.hour >= 18)
        is_locked = (now.weekday() == 0)
        if not is_locked and now.weekday() == 0:
            try:
                hl, ml = map(int, conf["REG_END_TIME"].split(":"))
                if now >= now.replace(hour=hl, minute=ml, second=0, microsecond=0): is_locked = True
            except: pass

        if is_new or not os.path.exists(LOG_FILE):
            if os.path.exists(LOG_FILE): os.remove(LOG_FILE)
            with open(LOG_FILE, "w", encoding="utf-8") as f: f.write(f"{format_ts_short(now)} ✨ Event: {event_title}\n")
            lobby_cleanup(conf)
            state = {"event_id": apollo_msg["id"], "drivers": [], "last_make_sync": None, "sun_msg_sent": False, "extra_msg_sent": False}

        # DOPPEL-EINTRAG SCHUTZ: Namen aus aktuellem Log ziehen
        log_content = "\n".join(read_persistent_log())
        added = [d for d in drivers if clean_for_log(d) not in log_content]
        removed = [d for d in state.get("drivers", []) if d not in drivers]
        moved_up = [d for d in drivers if d in state.get("drivers", []) and drivers.index(d) < grid_cap and state.get("drivers", []).index(d) >= grid_cap]

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
                wl = [clean_for_log(d) for d in added if drivers.index(d) >= grid_cap]
                send_combined_news(conf, "MSG_WAITLIST_MULTI" if len(wl) > 1 else "MSG_WAITLIST_SINGLE", driver_names=", ".join(wl))
            if conf["SW_MOVE"] and moved_up:
                send_combined_news(conf, "MSG_MOVED_UP_MULTI" if len(moved_up) > 1 else "MSG_MOVED_UP_SINGLE", driver_names=", ".join([clean_for_log(d) for d in moved_up]))

        # Sonntag & Extra Grid News
        if conf["SW_SUN"] and is_sun_18 and not state.get("sun_msg_sent"):
            grids_now = min(math.ceil(len(drivers) / conf["DRIVERS_PER_GRID"]), conf["MAX_GRIDS"])
            free = max(0, (grids_now * conf["DRIVERS_PER_GRID"]) - len(drivers))
            send_combined_news(conf, "MSG_SUNDAY_TEXT", driver_count=len(drivers), grids=grids_now, free_slots=free)
            state["sun_msg_sent"] = True

        count, grids = len(drivers), min(math.ceil(len(drivers) / conf["DRIVERS_PER_GRID"]), conf["MAX_GRIDS"])
        if conf["ENABLE_EXTRA"] and count > grid_cap + conf["EXTRA_THRESH"]:
            grids += 1
            if conf["SW_EXTRA"] and not state.get("extra_msg_sent"):
                send_combined_news(conf, "SET_MSG_EXTRA_GRID_TEXT")
                state["extra_msg_sent"] = True

        if conf["MAKE_WEBHOOK"] and (added or removed or is_new):
            payload = {"type": "event_reset" if is_new else "update", "driver_count": count, "drivers": [raw_for_make(d) for d in drivers], "grids": grids, "log_history": "\n".join(read_persistent_log()), "timestamp": now.isoformat()}
            requests.post(conf["MAKE_WEBHOOK"], json=payload)
            state["last_make_sync"] = now.isoformat()

        send_or_edit_log(conf, state, count, grids, is_locked, is_sun_18, grid_cap)
        state["drivers"] = drivers
        save_state(state)
        return "OK"
    except Exception as e: return f"Error: {str(e)}", 500

def send_or_edit_log(conf, state, count, grids, is_locked, is_sun, grid_cap):
    if not conf["CHAN_LOG"]: return
    log_text = "\n".join(read_persistent_log()[-15:])
    if is_locked: icon, status = "🔒", "Grids gesperrt / Locked"
    elif is_sun and count >= grid_cap: icon, status = "🟡", "Anmeldung auf Warteliste / Waitlist registration"
    else: icon, status = "🟢", "Anmeldung geöffnet / Open"
    
    sync_ts = format_ts_short(datetime.datetime.fromisoformat(state["last_make_sync"]).astimezone(BERLIN_TZ)) if state.get("last_make_sync") else "--"
    legend = "🟢 Angemeldet / Registered\n🟡 Warteliste / Waitlist\n🔴 Abgemeldet / Withdrawn"
    
    content = (f"{icon} **{status}**\nFahrer: `{count}` | Grids: `{grids}`\n\n"
               f"```\n{log_text}```\n"
               f"*Stand: {format_ts_short(get_now())}* | *Sync: {sync_ts}*\n\n"
               f"**Legende:**\n{legend}")
    
    h = {"Authorization": f"Bot {conf['TOKEN_APOLLO']}"}
    if conf["MANUAL_LOG_ID"]: requests.patch(f"https://discord.com/api/v10/channels/{conf['CHAN_LOG']}/messages/{conf['MANUAL_LOG_ID']}", headers=h, json={"content": content})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))