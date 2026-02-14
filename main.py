import os, requests, json, re, math, datetime, pytz, random
from flask import Flask, request

# --- KONFIGURATION ---
def get_env_config():
    return {
        "DISCORD_TOKEN": os.environ.get("DISCORD_TOKEN"),
        "CHAN_APOLLO": os.environ.get("CHAN_APOLLO"),
        "CHAN_LOG": os.environ.get("CHAN_LOG"),
        "CHAN_NEWS": os.environ.get("CHAN_NEWS"),
        "MAKE_WEBHOOK_URL": os.environ.get("MAKE_WEBHOOK_URL")
    }

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
    if not txt_de or not txt_en: return f"⚠️ Variable {env_de}/{env_en} fehlt!"
    opts_de, opts_en = [o.strip() for o in txt_de.split(";")], [o.strip() for o in txt_en.split(";")]
    idx = random.randrange(len(opts_de))
    try:
        return f"🇩🇪 {opts_de[idx].format(**kwargs)}\n🇬🇧 {opts_en[idx if idx < len(opts_en) else 0].format(**kwargs)}"
    except KeyError as e: return f"⚠️ Platzhalter {e} fehlt in {env_de}!"

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
    config = get_env_config()
    missing_critical = [k for k, v in config.items() if not v]
    if missing_critical:
        return f"<h3>❌ Konfigurations-Fehler</h3>Fehlende Variablen: {', '.join(missing_critical)}", 500

    try:
        url_grid_param = request.args.get('grids', type=int)
        do_test = request.args.get('texttest') == '1'
        
        headers = {"Authorization": f"Bot {config['DISCORD_TOKEN']}"}
        res = requests.get(f"https://discord.com/api/v10/channels/{config['CHAN_APOLLO']}/messages?limit=10", headers=headers)
        if res.status_code != 200: return f"Discord Error: {res.status_code}", 500

        apollo_msg = next((m for m in res.json() if m.get("author", {}).get("id") == APOLLO_BOT_ID and m.get("embeds")), None)
        if not apollo_msg: return "Keine Apollo-Nachricht gefunden."

        drivers = extract_data(apollo_msg["embeds"][0])
        if do_test:
            # Test-Funktion (siehe V41) hier integriert
            sample = [clean_name(d) for d in (drivers[:3] if drivers else ["Test1", "Test2", "Test3"])]
            msg = pick_bilingual_text("MSG_GRID_FULL_TEXT", "MSG_GRID_FULL_TEXT_EN", full_grids=2)
            requests.post(f"https://discord.com/api/v10/channels/{config['CHAN_NEWS']}/messages", headers=headers, json={"content": f"__Test Meilenstein:__\n{msg}"})
            return "Test-Modus: Nachricht an News-Kanal gesendet."

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

        # Protokoll für Browser-Ausgabe
        report = []
        is_new = (state.get("event_id") and state["event_id"] != apollo_msg["id"])
        
        if is_new or state.get("event_id") is None:
            state.update({"event_id": apollo_msg["id"], "sent_grids": [], "log_v2": [], "drivers": drivers, "grid_override": None, "extra_grid_active": False})
            state["log_v2"].append(f"{now_iso}|✨ Neues Event / Systemstart")
            for idx, d in enumerate(drivers):
                icon = "🟢" if idx < grid_cap else "🟡"
                state["log_v2"].append(f"{now_iso}|{icon} {clean_name(d)}{'' if idx < grid_cap else ' (Warteliste)'}")
            report.append("✨ <b>Neues Event erkannt.</b> Roster wurde initialisiert.")
            added, removed = [], []
        else:
            old = state.get("drivers", [])
            added = [d for d in drivers if d not in old]
            removed = [d for d in old if d not in drivers]
            
            for d in added:
                idx = drivers.index(d)
                icon = "🟢" if idx < grid_cap else "🟡"
                state["log_v2"].append(f"{now_iso}|{icon} {clean_name(d)}{'' if idx < grid_cap else ' (Warteliste)'}")
                report.append(f"🟢 Hinzugefügt: {clean_name(d)}")
            for d in removed:
                state["log_v2"].append(f"{now_iso}|🔴 {clean_name(d)}")
                report.append(f"🔴 Entfernt: {clean_name(d)}")
            for d in drivers:
                if d in old and drivers.index(d) < grid_cap and old.index(d) >= grid_cap:
                    state["log_v2"].append(f"{now_iso}|🟢 {clean_name(d)} (Nachgerückt)")
                    report.append(f"🆙 Nachgerückt: {clean_name(d)}")

        driver_count = len(drivers)
        grid_count = state["grid_override"] if state.get("grid_override") else min(math.ceil(driver_count/DRIVERS_PER_GRID), MAX_GRIDS)

        # News
        news_sent = False
        if not state.get("extra_grid_active") and (wd in [6,0,1]) and (driver_count - grid_cap >= EXTRA_GRID_THRESHOLD):
            state["extra_grid_active"] = True
            requests.post(f"https://discord.com/api/v10/channels/{config['CHAN_NEWS']}/messages", headers=headers, json={"content": pick_bilingual_text("MSG_EXTRA_GRID_TEXT", "MSG_EXTRA_GRID_TEXT_EN", waitlist_count=driver_count-grid_cap)})
            news_sent = True
        
        # Webhook
        webhook_status = "Keine Änderung"
        if config['MAKE_WEBHOOK_URL'] and (added or removed or url_grid_param is not None or is_new):
            if not is_locked or is_new:
                state["last_make_sync"] = now_iso
                requests.post(config['MAKE_WEBHOOK_URL'], json={"type": "update", "driver_count": driver_count, "drivers": drivers, "grids": grid_count})
                webhook_status = "✅ Übertragen"
            else:
                webhook_status = "🚫 Blockiert (Anmeldeschluss)"

        # Finale Log-Aktualisierung
        state["log_msg_id"] = send_or_edit_log(state, driver_count, grid_count, is_locked, state.get("grid_override") is not None, config)
        state["drivers"] = drivers
        save_state(state)
        
        # Browser HTML Output
        res_html = f"<h2>Apollo-Grabber V2 Status</h2>"
        res_html += f"<b>Status:</b> {'Gesperrt' if is_locked else 'Offen'}<br>"
        res_html += f"<b>Fahrer:</b> {driver_count} | <b>Grids:</b> {grid_count}<br>"
        res_html += f"<b>Grid-Sync (Make):</b> {webhook_status}<br><br>"
        res_html += "<b>Aktivitäten:</b><br>" + ("<br>".join(report) if report else "Keine Änderungen am Roster.")
        return res_html

    except Exception as e: return f"Fehler: {str(e)}", 500

def send_or_edit_log(state, driver_count, grid_count, is_locked, override_active, config):
    headers = {"Authorization": f"Bot {config['DISCORD_TOKEN']}", "Content-Type": "application/json"}
    grid_cap = MAX_GRIDS * DRIVERS_PER_GRID
    icon = "🟡" if is_locked and driver_count >= grid_cap else ("🔴" if is_locked else "🟢")
    status = "Anmeldung geöffnet" if not is_locked else ("Grids voll" if driver_count >= grid_cap else "Grids gesperrt")
    
    log_entries = filter_log_by_time(state.get("log_v2", []), datetime.timedelta(hours=24))
    log_text = "\n".join(log_entries) if log_entries else "Keine Änderungen."
    
    sync_ts = format_ts_short(datetime.datetime.fromisoformat(state['last_make_sync']).astimezone(BERLIN_TZ)) if state.get('last_make_sync') else "-- --:--"
    
    formatted = (f"{icon} **{status} / {'Open' if not is_locked else 'Locked'}**\n"
                 f"Fahrer: `{driver_count}` | Grids: `{grid_count}{' (Override)' if override_active else ''}`\n\n"
                 f"*Änderungen der letzten {LOG_TIME_SETTING}:*\n```\n{log_text}\n```\n"
                 f"*Stand: {format_ts_short(get_now())}*\n"
                 f"*Grid-Sync: {sync_ts}*")

    tid = SET_MANUAL_LOG_ID or state.get("log_msg_id")
    if tid:
        requests.patch(f"https://discord.com/api/v10/channels/{config['CHAN_LOG']}/messages/{tid}", headers=headers, json={"content": formatted})
        return tid
    res = requests.post(f"https://discord.com/api/v10/channels/{config['CHAN_LOG']}/messages", headers=headers, json={"content": formatted})
    return res.json().get("id")

def filter_log_by_time(log_entries, duration):
    now = get_now()
    filtered = []
    for entry in log_entries:
        if "|" in entry:
            ts_str, content = entry.split("|", 1)
            ts = datetime.datetime.fromisoformat(ts_str)
            if now - ts <= duration:
                filtered.append(f"{format_ts_short(ts.astimezone(BERLIN_TZ))} {content.strip()}")
    return filtered

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))