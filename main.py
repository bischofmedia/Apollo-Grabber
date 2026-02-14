import os, requests, json, re, math, datetime, pytz, random
from flask import Flask, request

# --- KONFIGURATION ---
# Wir laden diese nun in einer Funktion, um besser prüfen zu können
def get_env_config():
    return {
        "DISCORD_TOKEN": os.environ.get("DISCORD_TOKEN"),
        "CHAN_APOLLO": os.environ.get("CHAN_APOLLO"),
        "CHAN_LOG": os.environ.get("CHAN_LOG"),
        "CHAN_NEWS": os.environ.get("CHAN_NEWS"),
        "MAKE_WEBHOOK_URL": os.environ.get("MAKE_WEBHOOK_URL")
    }

# Optionale Variablen mit Standardwerten
DELETE_OLD_EVENT = os.environ.get("DELETE_OLD_EVENT", "0") == "1"
EXTRA_GRID_THRESHOLD = int(os.environ.get("EXTRA_GRID_THRESHOLD", 10))
SET_MIN_GRIDS_MSG = int(os.environ.get("SET_MIN_GRIDS_MSG", 1))
SET_MANUAL_LOG_ID = os.environ.get("SET_MANUAL_LOG_ID", "").strip()
LOG_TIME_SETTING = os.environ.get("LOG_TIME", "24h")
REG_END_TIME = os.environ.get("REGISTRATION_END_TIME", "").strip()
DRIVERS_PER_GRID = int(os.environ.get("DRIVERS_PER_GRID", 15))
MAX_GRIDS = int(os.environ.get("MAX_GRIDS", 4))

APOLLO_BOT_ID = "475744554910351370"
STATE_FILE = "state.json"
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
    txt_de = os.environ.get(env_de)
    txt_en = os.environ.get(env_en)
    
    if not txt_de or not txt_en:
        return f"⚠️ Fehler: Variablen `{env_de}` oder `{env_en}` fehlen in Render!"
        
    opts_de = [o.strip() for o in txt_de.split(";")]
    opts_en = [o.strip() for o in txt_en.split(";")]
    idx = random.randrange(len(opts_de))
    
    try:
        res_de = opts_de[idx].format(**kwargs)
        res_en = opts_en[idx].format(**kwargs) if idx < len(opts_en) else random.choice(opts_en).format(**kwargs)
        return f"🇩🇪 {res_de}\n🇬🇧 {res_en}"
    except KeyError as e:
        return f"⚠️ Format-Fehler: Platzhalter {e} fehlt in `{env_de}` oder `{env_en}`!"

# --- LOGIK ---
@app.route('/')
def home():
    config = get_env_config()
    
    # Check für kritische Variablen
    missing_critical = [k for k, v in config.items() if not v]
    if missing_critical:
        return f"<h3>❌ Konfigurations-Fehler</h3>Der Bot kann nicht starten. Folgende Variablen fehlen in Render: <br><ul><li><b>" + "</b></li><li><b>".join(missing_critical) + "</b></li></ul>", 500

    try:
        # Ab hier normale Logik
        url_grid_param = request.args.get('grids', type=int)
        do_test = request.args.get('texttest') == '1'
        
        headers = {"Authorization": f"Bot {config['DISCORD_TOKEN']}"}
        res = requests.get(f"https://discord.com/api/v10/channels/{config['CHAN_APOLLO']}/messages?limit=10", headers=headers)
        
        if res.status_code != 200:
            return f"Fehler bei Discord-Abfrage: {res.status_code} - Bitte DISCORD_TOKEN und CHAN_APOLLO prüfen."

        apollo_msg = next((m for m in res.json() if m.get("author", {}).get("id") == APOLLO_BOT_ID and m.get("embeds")), None)
        if not apollo_msg: return "Keine Apollo-Nachricht gefunden."

        drivers = extract_data(apollo_msg["embeds"][0])
        
        # Text-Test Modus
        if do_test:
            return run_text_test(drivers, config)

        state = load_state()
        now = get_now()
        now_iso = now.isoformat()
        wd = now.weekday()
        grid_cap = MAX_GRIDS * DRIVERS_PER_GRID
        
        # Sperr-Prüfung
        is_locked = (wd == 6 and now.hour >= 18) or (wd == 0)
        if not is_locked and wd == 0 and REG_END_TIME:
            try:
                h, m = map(int, REG_END_TIME.split(":"))
                if now >= now.replace(hour=h, minute=m, second=0, microsecond=0): is_locked = True
            except: pass
        if wd == 1 and now.hour < 10: is_locked = True

        if url_grid_param is not None:
            state["grid_override"] = min(url_grid_param, MAX_GRIDS) if url_grid_param > 0 else None

        # Event Erkennung
        is_new = (state.get("event_id") and state["event_id"] != apollo_msg["id"])
        if is_new or state.get("event_id") is None:
            state.update({"event_id": apollo_msg["id"], "sent_grids": [], "log_v2": [], "drivers": drivers, "grid_override": None, "extra_grid_active": False})
            state["log_v2"].append(f"{now_iso}|✨ Neues Event / Systemstart")
            for idx, d in enumerate(drivers):
                icon = "🟢" if idx < grid_cap else "🟡"
                suffix = "" if idx < grid_cap else " (Warteliste / Waitlist)"
                state["log_v2"].append(f"{now_iso}|{icon} {clean_name(d)}{suffix}")
            added, removed = [], [] 
        else:
            old = state.get("drivers", [])
            added = [d for d in drivers if d not in old]
            removed = [d for d in old if d not in drivers]
            
            for d in added:
                idx = drivers.index(d)
                icon = "🟢" if idx < grid_cap else "🟡"
                suffix = "" if idx < grid_cap else " (Warteliste / Waitlist)"
                state["log_v2"].append(f"{now_iso}|{icon} {clean_name(d)}{suffix}")
            for d in removed:
                state["log_v2"].append(f"{now_iso}|🔴 {clean_name(d)}")
            for d in drivers:
                if d in old and drivers.index(d) < grid_cap and old.index(d) >= grid_cap:
                    state["log_v2"].append(f"{now_iso}|🟢 {clean_name(d)} (Nachgerückt / Moved up)")

        driver_count = len(drivers)
        override_active = state.get("grid_override") is not None
        grid_count = state["grid_override"] if override_active else min(math.ceil(driver_count/DRIVERS_PER_GRID), MAX_GRIDS)

        # News Versand
        news_msg = None
        if not state.get("extra_grid_active") and (wd in [6,0,1]) and (driver_count - (MAX_GRIDS*DRIVERS_PER_GRID) >= EXTRA_GRID_THRESHOLD):
            state["extra_grid_active"] = True
            news_msg = pick_bilingual_text("MSG_EXTRA_GRID_TEXT", "MSG_EXTRA_GRID_TEXT_EN", waitlist_count=driver_count-grid_cap)
        elif driver_count > 0 and driver_count % DRIVERS_PER_GRID == 0 and (driver_count // DRIVERS_PER_GRID) <= MAX_GRIDS and (driver_count // DRIVERS_PER_GRID) not in state.get("sent_grids", []):
            news_msg = pick_bilingual_text("MSG_GRID_FULL_TEXT", "MSG_GRID_FULL_TEXT_EN", full_grids=driver_count//DRIVERS_PER_GRID)
            state.setdefault("sent_grids", []).append(driver_count // DRIVERS_PER_GRID)

        if news_msg:
            requests.post(f"https://discord.com/api/v10/channels/{config['CHAN_NEWS']}/messages", headers=headers, json={"content": news_msg})

        # Webhook
        if config['MAKE_WEBHOOK_URL'] and (added or removed or url_grid_param is not None or is_new) and (not is_locked or is_new):
            state["last_make_sync"] = now_iso
            requests.post(config['MAKE_WEBHOOK_URL'], json={"type": "update", "driver_count": driver_count, "drivers": drivers, "grids": grid_count, "override": override_active})
        
        state["log_msg_id"] = send_or_edit_log(state, driver_count, grid_count, is_locked, override_active, config)
        state["drivers"] = drivers
        save_state(state)
        
        return f"OK - Grids: {grid_count}"
    except Exception as e:
        return f"Kritischer Fehler: {str(e)}", 500

# --- WEITERE FUNKTIONEN (Extract, Load, Save, Log) ---
def extract_data(embed):
    drivers = []
    for field in embed.get("fields", []):
        if any(kw in field.get("name", "") for kw in ["Accepted", "Anmeldung", "Teilnehmer", "Confirmed", "Zusagen"]):
            for line in field.get("value", "").split("\n"):
                c = re.sub(r"^\d+[\s.)-]*", "", line.replace(">>>", "").replace(">", "")).strip()
                if c and "Grid" not in c and len(c) > 1: drivers.append(c)
    return drivers

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f: return json.load(f)
        except: pass
    return {"event_id": None, "drivers": [], "log_v2": [], "sent_grids": [], "extra_grid_active": False, "log_msg_id": None, "grid_override": None, "last_make_sync": None}

def save_state(s):
    with open(STATE_FILE, "w") as f: json.dump(s, f)

def send_or_edit_log(state, driver_count, grid_count, is_locked, override_active, config):
    headers = {"Authorization": f"Bot {config['DISCORD_TOKEN']}", "Content-Type": "application/json"}
    now = get_now()
    grid_cap = MAX_GRIDS * DRIVERS_PER_GRID
    
    if is_locked:
        icon, status = ("🟡", "Grids gesperrt & voll (Warteliste) / Grids locked & full (Waitlist)") if driver_count >= grid_cap else ("🔴", "Grids gesperrt / Grids locked")
    else:
        icon, status = "🟢", "Anmeldung geöffnet / Registration open"
    
    log_content = "\n".join(filter_log_by_time(state.get("log_v2", []), parse_log_time(LOG_TIME_SETTING))) or "Keine Änderungen."
    formatted = f"{icon} **{status}**\nFahrer / Drivers: `{driver_count}` | Grids: `{grid_count}{' (Override)' if override_active else ''}`\n\n*Änderungen der letzten {LOG_TIME_SETTING}:*\n```\n{log_content}\n```\n*Stand: {format_ts_short(now)}*\n*Letzte Übertragung: {format_ts_short(datetime.datetime.fromisoformat(state['last_make_sync']).astimezone(BERLIN_TZ)) if state['last_make_sync'] else '-- --:--'}*"
    
    tid = SET_MANUAL_LOG_ID or state.get("log_msg_id")
    if tid:
        requests.patch(f"https://discord.com/api/v10/channels/{config['CHAN_LOG']}/messages/{tid}", headers=headers, json={"content": formatted})
        return tid
    res = requests.post(f"https://discord.com/api/v10/channels/{config['CHAN_LOG']}/messages", headers=headers, json={"content": formatted})
    return res.json().get("id")

def run_text_test(drivers, config):
    sample = [clean_name(d) for d in (drivers[:3] if drivers else ["Test1", "Test2", "Test3"])]
    test_cases = [("Meilenstein", "MSG_GRID_FULL_TEXT", "MSG_GRID_FULL_TEXT_EN", {"full_grids": 2}),
                  ("Warteliste", "MSG_WAITLIST_SINGLE", "MSG_WAITLIST_SINGLE_EN", {"driver_names": sample[0]}),
                  ("Zusatzgrid", "MSG_EXTRA_GRID_TEXT", "MSG_EXTRA_GRID_TEXT_EN", {"waitlist_count": 5})]
    for label, de, en, args in test_cases:
        requests.post(f"https://discord.com/api/v10/channels/{config['CHAN_NEWS']}/messages", 
                      headers={"Authorization": f"Bot {config['DISCORD_TOKEN']}"}, 
                      json={"content": f"__Test {label}:__\n{pick_bilingual_text(de, en, **args)}"})
    return "Test-Nachrichten gesendet. Prüfe den News-Kanal."

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))