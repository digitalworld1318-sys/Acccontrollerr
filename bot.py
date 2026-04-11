import os
import psycopg2
from fastapi import FastAPI
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError
import cloudinary
import cloudinary.uploader

app = FastAPI()

# ================= CONFIG =================

API_ID = 30567132
API_HASH = "1f3e675de52fcfe4762e3ad5015f4ebc"

OWNER_KEY = "cyberxowner"
USER_KEY = "cyberx"

DATABASE_URL = "postgresql://password:0pJzUJ2gatvAR1fz7CQvDqH6GzZA7EWn@dpg-d7agi5udqaus73ctagrg-a.oregon-postgres.render.com/z4x_all_in_one_api"

cloudinary.config(
    cloud_name="dwsyry63w",
    api_key="862783951224336",
    api_secret="uBiYjcfhNsIgEjrH_Iqw6dB190I"
)

# ================= DB =================

conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS sessions (
    phone TEXT PRIMARY KEY,
    session TEXT
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS cache (
    username TEXT PRIMARY KEY,
    data TEXT
)
""")

conn.commit()

clients = {}

# ================= LOGIN =================

@app.get("/login")
async def login(key: str, num: str):
    if key != OWNER_KEY:
        return {"error": "Invalid key"}

    phone = "+91" + num

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    await client.send_code_request(phone)

    clients[num] = client

    return {"status": "OTP sent"}

# ================= VERIFY =================

@app.get("/verify")
async def verify(key: str, num: str, otp: str):
    if key != OWNER_KEY:
        return {"error": "Invalid key"}

    phone = "+91" + num
    client = clients.get(num)

    if not client:
        return {"error": "Login first"}

    try:
        await client.sign_in(phone, otp)
    except SessionPasswordNeededError:
        return {"error": "2FA enabled"}

    session_str = client.session.save()

    cur.execute(
        "INSERT INTO sessions (phone, session) VALUES (%s, %s) ON CONFLICT (phone) DO UPDATE SET session=%s",
        (num, session_str, session_str)
    )
    conn.commit()

    return {"status": "Login successful"}

# ================= GET CLIENT =================

async def get_client():
    cur.execute("SELECT session FROM sessions LIMIT 1")
    row = cur.fetchone()

    if not row:
        return None

    session_str = row[0]

    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await client.start()

    return client

# ================= SEARCH =================

@app.get("/search")
async def search(key: str, username: str):
    if key != USER_KEY:
        return {"error": "Invalid key"}

    username = username.replace("@", "").strip()

    # cache check
    cur.execute("SELECT data FROM cache WHERE username=%s", (username,))
    row = cur.fetchone()

    if row:
        return eval(row[0])

    client = await get_client()

    if not client:
        return {"error": "No logged-in account"}

    try:
        user = await client.get_entity(username)

        full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()

        # total DP count
        total_dp_obj = await client.get_profile_photos(user)
        total_dp = total_dp_obj.total

        # current DP
        photos = await client.get_profile_photos(user, limit=1)

        dp_url = None

        if photos:
            file_path = f"{username}_dp"

            await client.download_media(photos[0], file_path)

            res = cloudinary.uploader.upload(
                file_path,
                resource_type="auto"
            )

            dp_url = res["secure_url"]

            if os.path.exists(file_path):
                os.remove(file_path)

        response = {
            "id": user.id,
            "username": user.username,
            "full_name": full_name,
            "total_dp": total_dp,
            "current_dp": dp_url
        }

        # save cache
        cur.execute(
            "INSERT INTO cache (username, data) VALUES (%s, %s) ON CONFLICT (username) DO UPDATE SET data=%s",
            (username, str(response), str(response))
        )
        conn.commit()

        return response

    except Exception as e:
        return {"error": str(e)}
