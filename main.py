import discord
import requests
import os
import hashlib

TOKEN = os.getenv("DISCORD_TOKEN")
WEBHOOK = os.getenv("MAKE_WEBHOOK")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)

tracked_message_id = None
last_hash = None


def normalize(text):
    lines = [l.strip() for l in text.split("\n")]
    lines = [l for l in lines if l and "Grid" not in l]
    return "\n".join(lines)


def hash_text(text):
    return hashlib.sha256(text.encode()).hexdigest()


def is_apollo_message(message):
    if not message.embeds:
        return False
    text = message.embeds[0].description or ""
    return "Grid" in text


async def find_apollo():
    global tracked_message_id
    channel = client.get_channel(CHANNEL_ID)

    async for msg in channel.history(limit=20):
        if is_apollo_message(msg):
            tracked_message_id = msg.id
            print("Tracking:", tracked_message_id)
            return


@client.event
async def on_ready():
    print("Connected")
    await find_apollo()


@client.event
async def on_message(message):
    global tracked_message_id

    if message.channel.id != CHANNEL_ID:
        return

    if is_apollo_message(message):
        tracked_message_id = message.id
        print("Switched tracking")


@client.event
async def on_raw_message_edit(payload):
    global last_hash

    if payload.message_id != tracked_message_id:
        return

    channel = client.get_channel(CHANNEL_ID)
    message = await channel.fetch_message(tracked_message_id)

    text = message.embeds[0].description or ""
    normalized = normalize(text)
    new_hash = hash_text(normalized)

    if new_hash == last_hash:
        print("No change")
        return

    last_hash = new_hash

    try:
        requests.post(WEBHOOK, json={"apollo": normalized}, timeout=5)
        print("Webhook sent")
    except Exception as e:
        print("Webhook error:", e)


client.run(TOKEN)