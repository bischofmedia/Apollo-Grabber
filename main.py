import requests
import hashlib
import os

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
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


def is_apollo(msg):
    if not msg.get("embeds"):
        return False

    text = msg["embeds"][0].get("description", "")

    keywords = ["Grid", "Driver", "Anmeldung"]

    return any(k in text for k in keywords)


def find_apollo_message():
    url = f"https://discord.com/api/v10/channels/{CHANNEL_ID}/messages?limit=20"

    headers = {"Authorization": f"Bot {TOKEN}"}

    r = requests.get(url, headers=headers)
    r.raise_for_status()

    for msg in r.json():
        if is_apollo(msg):
            return msg["embeds"][0].get("description", "")

    return ""


def main():
    text = find_apollo_message()

    if not text:
        print("Apollo message not found")
        return

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