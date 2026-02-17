import os
import json
import asyncio
import aiohttp
import datetime
import threading
import time
from flask import Flask

# --- BLOCK 1 & 12: KONFIGURATION & FLASK ---
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Apollo Grabber V2 is Live!", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# Umgebungsvariablen laden
TOKEN_APOLLO = os.getenv("DISCORD_TOKEN_APOLLOGRABBER")
TOKEN_LOBBY = os.getenv("DISCORD_TOKEN_LOBBYCODEGRABBER")
USER_ID_ORGA = os.getenv("USER_ID_ORGA", "").split(";")
CHAN_APOLLO = os.getenv("CHAN_APOLLO")
CHAN_LOG = os.getenv("CHAN_LOG")
CHAN_NEWS = os.getenv("CHAN_NEWS")
CHAN_CODES = os.getenv("CHAN_CODES")
CHAN_ORDERS = os.getenv("CHAN_ORDERS") or CHAN_LOG
MAKE_WEBHOOK = os.getenv("MAKE_WEBHOOK_URL")

DRIVERS_PER_GRID = int(os.getenv("DRIVERS_PER_GRID", 15))
MAX_GRIDS = int(os.getenv("MAX_GRIDS", 4))
REG_END_TIME = os.getenv("REGISTRATION_END_TIME", "20:00")
MSG_HILFETEXT = os.getenv("MSG_HILFETEXT", "Kein Hilfetext konfiguriert.")

STATE_FILE = "state.json"
LOG_FILE = "event_log.txt"

# --- HILFSFUNKTIONEN ---

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f: return json.load(f)
    return {
        "event_id": None, "event_title": "Kein Event", "event_datetime": "",
        "drivers": [], "driver_status": {}, "manual_grids": None,
        "grids_locked": False, "last_grid_count": 0, "active_log_id": None,
        "last_sync_make": "Nie", "sunday_msg_sent": False
    }

def save_state(state):
    with open(STATE_FILE, "w") as f: json.dump(state, f, indent=4)

def get_timestamp():
    days = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    now = datetime.datetime.now()
    return f"{days[now.weekday()]} {now.strftime('%H:%M')}"

def write_to_log(text):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{get_timestamp()} {text}\n")

async def discord_request(method, url, token, json_data=None):
    url = url.strip().replace("\u200b", "")
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, headers=headers, json=json_data) as resp:
            if resp.status in [200, 201, 204]:
                return await resp.json() if resp.status != 204 else True
            return None

# --- BLOCK 10 & 11: BEFEHLE & CLEANUP ---

async def handle_commands(state):
    url = f"https://discord.com/api/v10/channels/{CHAN_ORDERS}/messages?limit=10"
    messages = await discord_request("GET", url, TOKEN_APOLLO)
    if not messages: return

    for msg in messages:
        content = msg.get('content', '').strip()
        u_id = msg.get('author', {}).get('id')
        u_name = msg.get('author', {}).get('username', 'Unbekannt')

        if content.startswith("!"):
            if u_id in USER_ID_ORGA:
                if content == "!help":
                    await discord_request("POST", url, TOKEN_APOLLO, {"content": MSG_HILFETEXT})
                elif content.startswith("!grids="):
                    try:
                        val = int(content.split("=")[1])
                        state["manual_grids"] = None if val == 0 else min(val, MAX_GRIDS)
                        state["grids_locked"] = val != 0
                        write_to_log(f"{'🔓' if val==0 else '🔒'} Grids auf {val} gesetzt durch {u_name}")
                    except: pass
                elif content == "!cleanlog":
                    if os.path.exists(LOG_FILE): os.remove(LOG_FILE)
                    state["driver_status"] = {}; state["drivers"] = []
                    write_to_log(f"⚠️ Cleanlog durch {u_name}")
                elif content == "!cleancodes":
                    await run_lobby_cleanup(); write_to_log(f"⚙️ Lobby-Bereinigung durch {u_name}")
                elif content == "!cleanchannel":
                    state["active_log_id"] = None; write_to_log(f"🧹 Channel bereinigt von {u_name}")
            
            await discord_request("DELETE", f"{url}/{msg['id']}", TOKEN_APOLLO)

async def run_lobby_cleanup():
    url = f"https://discord.com/api/v10/channels/{CHAN_CODES}/messages"
    msgs = await discord_request("GET", url, TOKEN_LOBBY)
    if msgs:
        for m in msgs: await discord_request("DELETE", f"{url}/{m['id']}", TOKEN_LOBBY)
    await discord_request("POST", url, TOKEN_LOBBY, {"content": os.getenv("MSG_LOBBYCODES", "Lobbycodes bereit.")})

# --- BLOCK 2-9: HAUPTLOGIK ---

async def main_cycle():
    state = load_state()
    await handle_commands(state)
    
    url = f"https://discord.com/api/v10/channels/{CHAN_APOLLO}/messages?limit=5"
    messages = await discord_request("GET", url, TOKEN_APOLLO)
    if not messages: return

    apollo_msg = messages[0]
    embed = apollo_msg.get('embeds', [{}])[0]
    curr_id, curr_title = apollo_msg['id'], embed.get('title', 'Event')
    
    if state["event_id"] != curr_id:
        state.update({"event_id": curr_id, "event_title": curr_title, "manual_grids": None, "grids_locked": False, "sunday_msg_sent": False, "driver_status": {}})
        if os.path.exists(LOG_FILE): os.remove(LOG_FILE)
        write_to_log("New Event")
        await run_lobby_cleanup()

    curr_drivers = []
    for field in embed.get('fields', [])[1:]:
        for l in field['value'].split('\n'):
            name = l.replace(">>>", "").strip()
            if name: curr_drivers.append(name)

    now = datetime.datetime.now()
    if now.weekday() == 6 and now.hour >= 18: state["grids_locked"] = True
    
    grid_count = state["manual_grids"] if state["manual_grids"] is not None else (
        min(-(-len(curr_drivers) // DRIVERS_PER_GRID), MAX_GRIDS) if not state["grids_locked"] else state["last_grid_count"]
    )
    state["last_grid_count"], cap = grid_count, grid_count * DRIVERS_PER_GRID

    added = [d for d in curr_drivers if d not in state["drivers"]]
    removed = [d for d in state["drivers"] if d not in curr_drivers]

    if added or removed or state["event_id"] != curr_id:
        for d in added:
            status = "grid" if len([s for s in state["driver_status"].values() if s == "grid"]) < cap else "waitlist"
            state["driver_status"][d] = status
            write_to_log(f"{'🟢' if status=='grid' else '🟡'} {d.replace('\\','')} {'(auf Warteliste)' if status=='waitlist' else ''}")
        for d in removed:
            write_to_log(f"🔴 {d.replace('\\','')}")
            state["driver_status"].pop(d, None)
            for dr, st in state["driver_status"].items():
                if st == "waitlist" and len([s for s in state["driver_status"].values() if s == "grid"]) < cap:
                    state["driver_status"][dr] = "grid"
                    write_to_log(f"🔵 {dr.replace('\\','')} (zurück von Warteliste)"); break

        payload = {"type": "update", "driver_count": len(curr_drivers), "drivers": curr_drivers, "grids": grid_count, "grid_status": "locked" if state["grids_locked"] else "open", "log_history": open(LOG_FILE, "r").read(), "timestamp": now.isoformat()}
        async with aiohttp.ClientSession() as s: 
            await s.post(MAKE_WEBHOOK, json=payload)
            state["last_sync_make"] = now.strftime("%H:%M")

    await update_discord_display(state, grid_count, cap)
    state["drivers"] = curr_drivers
    save_state(state)

async def update_discord_display(state, grids, cap):
    now = datetime.datetime.now()
    r_h, r_m = map(int, REG_END_TIME.split(":"))
    closed = now.weekday() == 0 and now.time() >= datetime.time(r_h, r_m)
    emoji = "🔴" if closed else ("🟡" if len(state["drivers"]) >= cap else "🟢")
    
    with open(LOG_FILE, "r") as f: log = "".join([l.replace("\\", "") for l in f.readlines()])
    body = f"{emoji} **{'Closed' if closed else 'Open'}**\n**{state['event_title']}**\nFahrer: {len(state['drivers'])} | Grids: {grids}\n```ansi\n{log[-1500:]}```\nStand: {get_timestamp()} | Sync: {state['last_sync_make']}"
    
    l_url = f"https://discord.com/api/v10/channels/{CHAN_LOG}/messages"
    if state["active_log_id"]:
        if not await discord_request("PATCH", f"{l_url}/{state['active_log_id']}", TOKEN_APOLLO, {"content": body}): state["active_log_id"] = None
    if not state["active_log_id"]:
        new_msg = await discord_request("POST", l_url, TOKEN_APOLLO, {"content": body})
        if new_msg: state["active_log_id"] = new_msg['id']

# --- BLOCK 12: EXECUTION LOOP ---

async def run_forever():
    print("Apollo Grabber V2 Loop gestartet...")
    while True:
        try:
            await main_cycle()
        except Exception as e:
            print(f"Fehler im Zyklus: {e}")
        await asyncio.sleep(60)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.run(run_forever())