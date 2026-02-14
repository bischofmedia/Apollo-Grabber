import os, requests, json, re, math, datetime, pytz, random
from flask import Flask, request

# --- KONFIGURATION ---
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
CHAN_APOLLO = os.environ.get("CHAN_APOLLO") 
CHAN_LOG = os.environ.get("CHAN_MAIN_LOG")    
CHAN_NEWS = os.environ.get("CHAN_NEWS_FLASH")  
MAKE_WEBHOOK_URL = os.environ.get("MAKE_WEBHOOK_URL")

DELETE_OLD_EVENT = os.environ.get("SET_DELETE_OLD_EVENT", "0") == "1"
EXTRA_GRID_THRESHOLD = int(os.environ.get("SET_EXTRA_GRID_THRESHOLD", 10))
MIN_GRIDS_FOR_MESSAGE = int(os.environ.get("SET_MIN_GRIDS_MSG", 1))
MANUAL_LOG_ID = os.environ.get("SET_MANUAL_LOG_ID", "").strip()
LOG_TIME_SETTING = os.environ.get("LOG_TIME", "24h")
REG_END_TIME = os.environ.get("REGISTRATION_END_TIME", "").strip()

MAX_GRIDS = 4 
DRIVERS_PER_GRID = 15
STATE_FILE = "state.json"
APOLLO_BOT_ID = "475744554910351370"
BERLIN_TZ = pytz.timezone("Europe/Berlin")

app = Flask(__name__)

# --- HELFER ---
def get_now(): return datetime.datetime.now(BERLIN_TZ)

def format_ts_short(dt_obj):
    days = {"Mon":"Mo", "Tue":"Di", "Wed":"Mi", "Thu":"Do", "Fri":"Fr", "Sat":"Sa", "Sun":"So"}
    raw = dt_obj.strftime("%a %H:%M")
    for en, de in days.items(): raw = raw.replace(en, de)
    return raw

def clean_name(n): return n.replace("\\_", "_").replace("\\*", "*").replace("*", "").strip()

def pick_bilingual_text(env_de, env_en, **kwargs):
    opts_de = [o.strip() for o in os.environ.get(env_de, "Text fehlt").split(";")]
    opts_en = [o.strip() for o in os.environ.get(env_en, "Text missing").split(";")]
    idx = random.randrange(len(opts_de))
    txt_en = opts_en[idx].format(**kwargs) if idx < len(opts_en) else random.choice(opts_en).format(**kwargs)
    txt_de = opts_de[idx].format(**kwargs)
    return f"🇩🇪 {txt_de}\n🇬🇧 {txt_en}"

def parse_log_time(setting):
    unit = setting[-1].lower()
    try:
        val = int(setting[:-1])
        if unit == 'h': return datetime.timedelta(hours=val)
        if unit == 'd': return datetime.timedelta(days=val)
    except: pass
    return datetime.timedelta(hours=24)

def filter_log_by_time(log_entries, duration):
    now = get_now()
    filtered = []
    for entry in log_entries:
        if "|" in entry:
            ts_str, content = entry.split("|", 1)
            try:
                ts = datetime.datetime.fromisoformat(ts_str)
                if now - ts <= duration:
                    filtered.append(f"{format_ts_short(ts.astimezone(BERLIN_TZ))} {content.strip()}")
            except: pass
    return filtered

# --- TEST FUNKTION ---
def run_text_test(drivers):
    sample_drivers = random.sample(drivers, min(len(drivers), 3)) if drivers else ["Test_Fahrer_1", "Test_Fahrer_2"]
    d_names = ", ".join([clean_name(d) for d in sample_drivers])
    test_cases = [
        ("Grid Meilenstein", "MSG_GRID_FULL_TEXT", "MSG_GRID_FULL_TEXT_EN", {"full_grids": 3}),
        ("Warteliste Einzel", "MSG_WAITLIST_SINGLE", "MSG_WAITLIST_SINGLE_EN", {"driver_names": clean_name(sample_drivers[0])}),
        ("Warteliste Multi", "MSG_WAITLIST_MULTI", "MSG_WAITLIST_MULTI_EN", {"driver_names": d_names}),
        ("Nachrücker Einzel", "MSG_MOVED_UP_SINGLE", "MSG_MOVED_UP_SINGLE_EN", {"driver_names": clean_name(sample_drivers[0])}),
        ("Nachrücker Multi", "MSG_MOVED_UP_MULTI", "MSG_MOVED_UP_MULTI_EN", {"driver_names": d_names}),
        ("Zusatzgrid", "MSG_EXTRA_GRID_TEXT", "MSG_EXTRA_GRID_TEXT_EN", {"waitlist_count": 12})
    ]
    for label, env_de, env_en, args in test_cases:
        msg = pick_bilingual_text(env_de, env_en, **args)
        requests.post(f"https://discord.com/api/v10/channels/{CHAN_NEWS}/messages", headers={"Authorization": f"Bot {DISCORD_TOKEN}"}, json={"content": f"__Test: {label}__\n{msg}"})
    return "Test-Nachrichten gesendet."

# --- DISCORD LOG ---
def send_or_edit_log(state, driver_count, grid_count, is_locked, override_active):
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"}
    grid_cap = MAX_GRIDS * DRIVERS_PER_GRID
    now = get_now()
    
    if is_locked:
        icon, status = ("🟡", "Grids gesperrt & voll (Warteliste) / Grids locked & full (Waitlist)") if driver_count >= grid_cap else ("🔴", "Grids gesperrt / Grids locked")
    else:
        icon, status = "🟢", "Anmeldung geöffnet / Registration open"
    
    duration = parse_log_time(LOG_TIME_SETTING)
    filtered = filter_log_by_time(state.get("log_v2", []), duration)
    log_content = "\n".join(filtered) if filtered else "Keine Änderungen / No changes."
    ov = " (Override)" if override_active else ""
    
    update_ts = format_ts_short(now)
    make_raw = state.get("last_make_sync")
    make_ts = format_ts_short(datetime.datetime.fromisoformat(make_raw).astimezone(BERLIN_TZ)) if make_raw else "-- --:--"

    formatted = (
        f"{icon} **{status}**\n"
        f"Fahrer / Drivers: `{driver_count}` | Grids: `{grid_count}{ov}` ({'gesperrt / locked' if is_locked else 'offen / open'})\n\n"
        f"*Änderungen der letzten / Changes last {LOG_TIME_SETTING}:*\n```\n{log_content}\n```\n"
        f"*Stand: {update_ts}*\n"
        f"*Letzte Übertragung ins Grid / Last grid sync: {make_ts}*"
    )
    
    target_id = MANUAL_LOG_ID if MANUAL_LOG_ID else state.get("log_msg_id")
    if target_id:
        url = f"https://discord.com/api/v10/channels/{CHAN_LOG}/messages/{target_id}"
        requests.patch(url, headers=headers, json={"content": formatted})
        return target_id
    
    res = requests.post(f"https://discord.com/api/v10/channels/{CHAN_LOG}/messages", headers=headers, json={"content": formatted})
    return res.json().get("id") if res.status_code == 200 else None

# --- STATE ---
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f: return json.load(f)
        except: pass
    return {"event_id": None, "drivers": [], "log_v2": [], "sent_grids": [], "extra_grid_active": False, "log_msg_id": None, "grid_override": None, "last_make_sync": None}

def save_state(s):
    with open(STATE_FILE, "w") as f: json.dump(s, f)

def extract_data(embed):
    drivers = []
    for field in embed.get("fields", []):
        if any(kw in field.get("name", "") for kw in ["Accepted", "Anmeldung", "Teilnehmer", "Confirmed", "Zusagen"]):
            for line in field.get("value", "").split("\n"):
                c = re.sub(r"^\d+[\s.)-]*", "", line.replace(">>>", "").replace(">", "")).strip()
                if c and "Grid" not in c and len(c) > 1: drivers.append(c)
    return drivers

# --- MAIN ---
@app.route('/')
def home():
    if not all([DISCORD_TOKEN, CHAN_APOLLO, CHAN_LOG, CHAN_NEWS]): return "Config Error"
    try:
        url_grid_param = request.args.get('grids', type=int)
        do_test = request.args.get('texttest') == '1'
        
        res = requests.get(f"https://discord.com/api/v10/channels/{CHAN_APOLLO}/messages?limit=10", headers={"Authorization": f"Bot {DISCORD_TOKEN}"})
        apollo_msg = next((m for m in res.json() if m.get("author", {}).get("id") == APOLLO_BOT_ID and m.get("embeds")), None)
        if not apollo_msg: return "No Apollo message."

        drivers = extract_data(apollo_msg["embeds"][0])
        if do_test: return run_text_test(drivers)

        state = load_state()
        now = get_now()
        now_iso = now.isoformat()
        wd = now.weekday()
        
        # Sperr-Logik
        is_locked = (wd == 6 and now.hour >= 18) or (wd == 0)
        if not is_locked and wd == 0 and REG_END_TIME:
            try:
                h, m = map(int, REG_END_TIME.split(":"))
                if now >= now.replace(hour=h, minute=m, second=0, microsecond=0): is_locked = True
            except: pass
        if wd == 1 and now.hour < 10: is_locked = True

        if url_grid_param is not None:
            state["grid_override"] = min(url_grid_param, MAX_GRIDS) if url_grid_param > 0 else None

        is_new = (state.get("event_id") and state["event_id"] != apollo_msg["id"])
        
        # Daten-Handling & Log
        if is_new or state.get("event_id") is None:
            state.update({"event_id": apollo_msg["id"], "sent_grids": [], "log_v2": [], "drivers": drivers, "grid_override": None, "extra_grid_active": False})
            state["log_v2"].append(f"{now_iso}|✨ Neues Event / Systemstart")
            for d in drivers: state["log_v2"].append(f"{now_iso}|🟢 {clean_name(d)}")
            added, removed = [], [] 
        else:
            old = state.get("drivers", [])
            added, removed = [d for d in drivers if d not in old], [d for d in old if d not in drivers]
            for d in added: state["log_v2"].append(f"{now_iso}|🟢 {clean_name(d)}")
            for d in removed: state["log_v2"].append(f"{now_iso}|🔴 {clean_name(d)}")

        driver_count, grid_cap = len(drivers), MAX_GRIDS * DRIVERS_PER_GRID
        override_active = state.get("grid_override") is not None
        grid_count = state["grid_override"] if override_active else min(math.ceil(driver_count/15), MAX_GRIDS)

        # News Logik
        news_msg = None
        if not state.get("extra_grid_active") and (wd in [6,0,1]) and (driver_count - 60 >= EXTRA_GRID_THRESHOLD):
            state["extra_grid_active"] = True
            news_msg = pick_bilingual_text("MSG_EXTRA_GRID_TEXT", "MSG_EXTRA_GRID_TEXT_EN", waitlist_count=driver_count-60)
        elif driver_count > 0 and driver_count % 15 == 0 and (driver_count // 15) <= MAX_GRIDS and (driver_count // 15) not in state.get("sent_grids", []):
            news_msg = pick_bilingual_text("MSG_GRID_FULL_TEXT", "MSG_GRID_FULL_TEXT_EN", full_grids=driver_count//15)
            state.setdefault("sent_grids", []).append(driver_count // 15)

        if is_locked and (added or removed):
            wait_list = [clean_name(d) for d in added if drivers.index(d) >= grid_cap]
            if wait_list: news_msg = pick_bilingual_text("MSG_WAITLIST_SINGLE" if len(wait_list)==1 else "MSG_WAITLIST_MULTI", "MSG_WAITLIST_SINGLE_EN" if len(wait_list)==1 else "MSG_WAITLIST_MULTI_EN", driver_names=", ".join(wait_list))
            up = [clean_name(d) for i, d in enumerate(drivers) if i < grid_cap and d in old and old.index(d) >= grid_cap]
            if up: news_msg = pick_bilingual_text("MSG_MOVED_UP_SINGLE" if len(up)==1 else "MSG_MOVED_UP_SINGLE_EN", "MSG_MOVED_UP_MULTI" if len(up)>1 else "MSG_MOVED_UP_MULTI_EN", driver_names=", ".join(up))

        if news_msg:
            requests.post(f"https://discord.com/api/v10/channels/{CHAN_NEWS}/messages", headers={"Authorization": f"Bot {DISCORD_TOKEN}"}, json={"content": news_msg})

        # --- MAKE WEBHOOK LOGIK ---
        # Senden nur wenn: (Änderung vorhanden ODER neues Event ODER Override) UND (Anmeldung NICHT gesperrt ODER es ist ein brandneues Event)
        should_send = (added or removed or url_grid_param is not None or is_new)
        can_send = (not is_locked or is_new)
        
        if MAKE_WEBHOOK_URL and should_send and can_send:
            state["last_make_sync"] = now_iso
            requests.post(MAKE_WEBHOOK_URL, json={"type": "update", "driver_count": driver_count, "drivers": drivers, "grids": grid_count, "override": override_active, "timestamp": now_iso})
        
        state["log_msg_id"] = send_or_edit_log(state, driver_count, grid_count, is_locked, override_active)
        state["drivers"] = drivers
        save_state(state)
        
        return f"OK - Grids: {grid_count}"
    except Exception as e: return str(e)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))