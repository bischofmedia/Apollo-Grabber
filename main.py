import requests
import hashlib
import os
import json

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
MESSAGE_ID = os.getenv("MESSAGE_ID")
WEBHOOK = os.getenv("MAKE_WEBHOOK")

HASH_FILE = "last_hash.txt"


def normalize(text):
    lines = [l.strip() for l in text.split("\n")]
    lines = [l for l in lines if l and "Grid" not in l]
    return "\n".join(lines)


def load_hash():
    if os.path.exists(HASH_FILE):
        return open(HASH_FILE).read()
    return None


def save_hash(h):
    with open(HASH_FILE, "w") as f:
        f.write(h)


def get_message():
    url = f"https://discord.com/api/v10/channels/{CHANNEL_ID}/messages/{MESSAGE_ID}"

    headers = {
        "Authorization": f"Bot {TOKEN}"
    }

    r = requests.get(url, headers=headers)
    r.raise_for_status()
    data = r.json()

    embed = ""
    if data.get("embeds"):
        embed = data["embeds"][0].get("description", "")

    return embed


def main():
    text = get_message()
    normalized = normalize(text)

    new_hash = hashlib.sha256(normalized.encode()).hexdigest()
    old_hash = load_hash()

    if new_hash == old_hash:
        print("No change")
        return

    save_hash(new_hash)

    requests.post(WEBHOOK, json={"apollo": normalized})
    print("Change detected → webhook sent")


if __name__ == "__main__":
    main()