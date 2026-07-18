from __future__ import annotations
import os
import json
import secrets
import shutil
import subprocess
import threading
import time
import zipfile
import io
import sys
import signal
import base64
import re
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
import telebot
from telebot import types
from flask import Flask, jsonify

# ================================================================
#  CONFIG & UI GLYPHS
# ================================================================

BOT_TOKEN = "8764301541:AAGvFjYzPOcm47UaKeg1arNefqHanuQxbmc"
OWNER_ID = 8373276191

if not BOT_TOKEN:
    print("❌ BOT_TOKEN set karo!")
    sys.exit(1)

BASE_DIR = Path(__file__).resolve().parent
DIRS = {
    "data": BASE_DIR / "storage" / "data",
    "uploads": BASE_DIR / "storage" / "uploads",
    "sandbox": BASE_DIR / "sandbox",
    "photos": BASE_DIR / "storage" / "photos",
}
for d in DIRS.values():
    d.mkdir(parents=True, exist_ok=True)

DB_FILE = DIRS["data"] / "db.json"
PORT = int(os.environ.get("PORT", 10000))

# ================================================================
#  ✅ BOT INSTANCE - YAHAN ADD KIYA
# ================================================================

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ── Glyphs ──
G = {
    "ok": "✅", "no": "❌", "warn": "⚠️",
    "arrow": "➡️", "bullet": "•", "back": "◀️",
    "play": "▶️", "stop": "⏹", "refresh": "🔄",
    "running": "🟢", "stopped": "🔴",
    "lock": "🔒", "shield": "🛡️", "key": "🔑",
    "user": "👤", "users": "👥", "crown": "👑",
    "upload": "📤", "download": "📥", "folder": "📁",
    "settings": "⚙️", "cog": "🔧", "bolt": "⚡",
    "stats": "📊", "graph": "📈",
    "broadcast": "📢", "ticket": "🎫",
    "cloud": "☁️", "trash": "🗑️", "eye": "👁️",
    "plus": "➕", "minus": "➖", "div": "━━━━━━━━━━━━━━━━━━",
}

BRAND = "🤖 Simran Hosting Bot"
FOOTER = f"\n\n{BRAND}"

# ── Photos ──
PHOTOS: Dict[str, str] = {}

def _build_photos():
    try:
        from PIL import Image, ImageDraw, ImageFont
    except:
        return
    out_dir = DIRS["photos"]
    out_dir.mkdir(parents=True, exist_ok=True)
    colors = {
        "main": "#1E1B4B", "admin": "#7C2D12", "bots": "#0E7490",
        "upload": "#4338CA", "profile": "#1E3A8A", "help": "#334155",
    }
    for key, color in colors.items():
        out = out_dir / f"{key}.png"
        if out.exists():
            PHOTOS[key] = str(out)
            continue
        try:
            img = Image.new("RGB", (800, 300), color)
            draw = ImageDraw.Draw(img)
            draw.rectangle([(0, 270), (800, 300)], fill="#FFFFFF")
            try:
                font = ImageFont.load_default()
                draw.text((50, 50), f"✨ {key.upper()}", fill="#FFFFFF", font=font)
            except:
                pass
            img.save(out, "PNG")
            PHOTOS[key] = str(out)
        except:
            pass

_build_photos()

# ================================================================
#  DATABASE
# ================================================================

def db_load():
    if not DB_FILE.exists():
        return {"users": {}, "bots": {}, "admins": {}, "audit": [], "deleted_bots": []}
    try:
        return json.loads(DB_FILE.read_text())
    except:
        return {"users": {}, "bots": {}, "admins": {}, "audit": [], "deleted_bots": []}

def db_save(d):
    DB_FILE.write_text(json.dumps(d, indent=2, default=str))

def get_user(uid):
    return db_load()["users"].get(str(uid))

def create_user(uid, name, username=""):
    d = db_load()
    if str(uid) not in d["users"]:
        d["users"][str(uid)] = {
            "id": uid, "name": name, "username": username,
            "joined": str(datetime.now(timezone.utc)),
            "plan": "free", "banned": False
        }
        db_save(d)
    return d["users"][str(uid)]

def is_admin(uid):
    return uid == OWNER_ID or str(uid) in db_load().get("admins", {})

def is_owner(uid):
    return uid == OWNER_ID

def audit(uid, action, detail=""):
    d = db_load()
    d["audit"].append({
        "ts": str(datetime.now(timezone.utc)),
        "uid": uid, "action": action, "detail": detail
    })
    d["audit"] = d["audit"][-200:]
    db_save(d)

def notify_owner(text):
    try:
        bot.send_message(OWNER_ID, text, parse_mode="HTML")
    except:
        pass

# ================================================================
#  BOT FUNCTIONS
# ================================================================

RUNNING: Dict[str, Dict] = {}

def find_bot(bot_id):
    return db_load()["bots"].get(bot_id)

def save_bot(doc):
    d = db_load()
    d["bots"][doc["_id"]] = doc
    db_save(d)

def delete_bot_doc(bot_id):
    d = db_load()
    b = d["bots"].get(bot_id)
    if b:
        d["deleted_bots"].append({
            "bot_id": bot_id,
            "name": b.get("name"),
            "owner": b.get("owner"),
            "deleted_at": str(datetime.now(timezone.utc))
        })
    d["bots"].pop(bot_id, None)
    db_save(d)

def user_bots(uid):
    return [b for b in db_load()["bots"].values() if b.get("owner") == uid]

def all_bots():
    return list(db_load()["bots"].values())

def all_users():
    return list(db_load()["users"].values())

def detect_entry(bot_dir):
    for f in ["bot.py", "main.py", "app.py", "run.py", "index.js", "bot.js"]:
        if (bot_dir / f).exists():
            return "python" if f.endswith(".py") else "node", f
    for f in bot_dir.glob("*.py"):
        return "python", f.name
    return None, None

def install_deps(bot_dir, kind):
    if kind == "python":
        req = bot_dir / "requirements.txt"
        if req.exists():
            try:
                subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(req)],
                             cwd=str(bot_dir), capture_output=True, timeout=120)
            except:
                pass

# ================================================================
#  SANDBOX RUNNER
# ================================================================

def start_child(b):
    bid = b["_id"]
    bot_dir = Path(b["dir"])
    if not bot_dir.exists():
        return {"ok": False, "error": "Folder missing"}
    
    kind, entry = detect_entry(bot_dir)
    if not kind:
        return {"ok": False, "error": "No entry file (bot.py / main.py)"}
    
    install_deps(bot_dir, kind)
    
    cmd = [sys.executable, "-u", entry] if kind == "python" else ["node", entry]
    env = {**os.environ, "HOME": str(bot_dir), "TMPDIR": str(bot_dir / "tmp")}
    (bot_dir / "tmp").mkdir(exist_ok=True)
    
    try:
        proc = subprocess.Popen(cmd, cwd=str(bot_dir), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              preexec_fn=os.setsid if os.name == "posix" else None)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    
    RUNNING[bid] = {"proc": proc, "log": [], "started": time.time(), "name": b["name"]}
    
    threading.Thread(target=_drain_proc, args=(bid, proc), daemon=True).start()
    
    b["status"] = "running"
    save_bot(b)
    return {"ok": True, "pid": proc.pid}

def _drain_proc(bid, proc):
    for line in iter(proc.stdout.readline, b""):
        try:
            txt = line.decode("utf-8", "replace").strip()
            if bid in RUNNING:
                RUNNING[bid]["log"].append(txt)
                if len(RUNNING[bid]["log"]) > 200:
                    RUNNING[bid]["log"] = RUNNING[bid]["log"][-200:]
        except:
            pass
    RUNNING.pop(bid, None)
    b = find_bot(bid)
    if b:
        b["status"] = "stopped"
        save_bot(b)

def stop_child(bid):
    info = RUNNING.get(bid)
    if not info:
        b = find_bot(bid)
        if b:
            b["status"] = "stopped"
            save_bot(b)
        return {"ok": True}
    
    proc = info["proc"]
    try:
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except:
                proc.terminate()
        else:
            proc.terminate()
        proc.wait(timeout=3)
    except:
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
        except:
            pass
    
    RUNNING.pop(bid, None)
    b = find_bot(bid)
    if b:
        b["status"] = "stopped"
        save_bot(b)
    return {"ok": True}

def restart_child(b):
    stop_child(b["_id"])
    time.sleep(1)
    return start_child(b)

def child_status(bid):
    info = RUNNING.get(bid)
    running = bool(info and info["proc"].poll() is None)
    logs = info.get("log", []) if info else []
    return {
        "running": running,
        "logs": logs[-50:],
        "uptime": int(time.time() - info["started"]) if running else 0
    }

# ================================================================
#  SECURITY SCAN
# ================================================================

def security_scan(content: bytes) -> Dict:
    threats = []
    try:
        text = content.decode("utf-8", errors="ignore")
    except:
        return {"safe": True, "threats": []}
    
    dangerous = [
        "os.system", "subprocess.call", "eval(", "exec(", "__import__('os')",
        "open('/etc/passwd'", "/proc/self/environ", "base64.b64decode",
        "marshal.loads", "zipfile.ZipFile", "shutil.rmtree"
    ]
    
    for pat in dangerous:
        if pat in text:
            threats.append(f"Found: {pat}")
    
    token_pattern = r'\d{8,10}:[A-Za-z0-9_-]{35}'
    if re.search(token_pattern, text):
        threats.append("⚠️ Bot token found in code!")
    
    return {
        "safe": len(threats) == 0,
        "threats": threats,
        "score": min(len(threats) * 20, 100)
    }

# ================================================================
#  GITHUB BACKUP
# ================================================================

GITHUB_ENABLED = False
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GH_REPO = os.environ.get("GITHUB_REPO", "")
GH_BRANCH = os.environ.get("GITHUB_BRANCH", "main")

if GH_TOKEN and GH_REPO:
    GITHUB_ENABLED = True

def gh_backup_now():
    if not GITHUB_ENABLED:
        return {"ok": False, "error": "GitHub not configured"}
    
    try:
        import requests
        d = db_load()
        data = json.dumps(d, indent=2)
        
        url = f"https://api.github.com/repos/{GH_REPO}/contents/backup.json"
        headers = {
            "Authorization": f"token {GH_TOKEN}",
            "Accept": "application/vnd.github+json"
        }
        
        sha = None
        r = requests.get(url, headers=headers)
        if r.status_code == 200:
            sha = r.json().get("sha")
        
        payload = {
            "message": f"Backup {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}",
            "content": base64.b64encode(data.encode()).decode(),
            "branch": GH_BRANCH
        }
        if sha:
            payload["sha"] = sha
        
        r = requests.put(url, headers=headers, json=payload, timeout=30)
        if r.status_code in (200, 201):
            return {"ok": True, "size": len(data)}
        else:
            return {"ok": False, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def gh_auto_loop():
    while True:
        time.sleep(21600)
        if GITHUB_ENABLED:
            res = gh_backup_now()
            if res.get("ok"):
                print(f"[backup] ok: {res.get('size')} bytes")
            else:
                print(f"[backup] failed: {res.get('error')}")

# ================================================================
#  KEYBOARDS
# ================================================================

class Btn(types.InlineKeyboardButton):
    def __init__(self, *args, style: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        if style:
            self.style = style

    def to_dict(self):
        d = super().to_dict()
        if getattr(self, "style", ""):
            d["style"] = self.style
        return d

def main_kb(admin=False):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        Btn(f"{G['upload']} Upload Bot", callback_data="upload", style="primary"),
        Btn(f"{G['folder']} My Bots", callback_data="my_bots", style="primary"),
    )
    kb.add(
        Btn(f"{G['user']} Profile", callback_data="profile", style="primary"),
        Btn(f"❓ Help", callback_data="help", style="primary"),
    )
    if admin:
        kb.add(Btn(f"{G['shield']} Admin Panel", callback_data="admin", style="danger"))
    return kb

def back_kb(target="menu_main"):
    kb = types.InlineKeyboardMarkup()
    kb.add(Btn(f"{G['back']} Back", callback_data=target, style="danger"))
    return kb

def bot_kb(bid, running=False):
    kb = types.InlineKeyboardMarkup(row_width=2)
    if running:
        kb.add(
            Btn(f"{G['stop']} Stop", callback_data=f"stop_{bid}", style="danger"),
            Btn(f"{G['refresh']} Restart", callback_data=f"restart_{bid}", style="success"),
        )
    else:
        kb.add(
            Btn(f"{G['play']} Start", callback_data=f"start_{bid}", style="success"),
        )
    kb.add(
        Btn(f"{G['eye']} Logs", callback_data=f"logs_{bid}", style="primary"),
        Btn(f"{G['trash']} Delete", callback_data=f"delete_{bid}", style="danger"),
    )
    kb.add(Btn(f"{G['back']} Back", callback_data="my_bots", style="primary"))
    return kb

def admin_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        Btn(f"{G['stats']} All Bots", callback_data="adm_bots", style="primary"),
        Btn(f"{G['users']} All Users", callback_data="adm_users", style="primary"),
    )
    kb.add(
        Btn(f"🚫 Ban/Unban", callback_data="adm_ban", style="danger"),
        Btn(f"{G['broadcast']} Broadcast", callback_data="adm_broadcast", style="success"),
    )
    kb.add(
        Btn(f"{G['shield']} Security", callback_data="adm_scan", style="primary"),
        Btn(f"{G['cloud']} GitHub Backup", callback_data="adm_github", style="primary"),
    )
    kb.add(
        Btn(f"{G['settings']} Settings", callback_data="adm_settings", style="primary"),
        Btn(f"{G['back']} Main", callback_data="menu_main", style="primary"),
    )
    return kb

def photo_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    for key in ["main", "admin", "bots", "upload", "profile", "help"]:
        kb.add(Btn(f"📸 {key.title()}", callback_data=f"photo_{key}", style="primary"))
    kb.add(Btn(f"{G['back']} Admin", callback_data="admin", style="primary"))
    return kb

# ================================================================
#  UI HELPERS
# ================================================================

_PHOTO_FILE_IDS: Dict[str, str] = {}

def resolve_photo(ref):
    if _PHOTO_FILE_IDS.get(ref):
        return _PHOTO_FILE_IDS[ref]
    if ref in PHOTOS and PHOTOS[ref]:
        try:
            return open(PHOTOS[ref], "rb")
        except:
            pass
    return None

def remember_photo(ref, msg):
    try:
        if msg and getattr(msg, "photo", None):
            _PHOTO_FILE_IDS[ref] = msg.photo[-1].file_id
    except:
        pass

def show_menu(chat_id, photo_key, caption, kb=None, call=None):
    photo = PHOTOS.get(photo_key, PHOTOS.get("main", ""))
    if call and call.message:
        try:
            if photo:
                bot.edit_message_media(
                    media=types.InputMediaPhoto(resolve_photo(photo_key) or photo,
                                                caption=caption, parse_mode="HTML"),
                    chat_id=chat_id,
                    message_id=call.message.message_id,
                    reply_markup=kb,
                )
                return
        except:
            pass
        try:
            bot.edit_message_text(caption, chat_id, call.message.message_id,
                                 reply_markup=kb, parse_mode="HTML")
            return
        except:
            pass
    try:
        if photo:
            m = bot.send_photo(chat_id, resolve_photo(photo_key) or photo,
                              caption=caption, reply_markup=kb, parse_mode="HTML")
            remember_photo(photo_key, m)
        else:
            m = bot.send_message(chat_id, caption, reply_markup=kb, parse_mode="HTML")
    except:
        bot.send_message(chat_id, caption, reply_markup=kb, parse_mode="HTML")

def show_text(chat_id, text, kb=None, call=None):
    if call and call.message:
        try:
            bot.edit_message_text(text, chat_id, call.message.message_id,
                                 reply_markup=kb, parse_mode="HTML")
            return
        except:
            pass
    bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")

# ================================================================
#  MENU RENDERS
# ================================================================

def render_main(chat_id, uid, call=None):
    u = get_user(uid) or {}
    bot_count = len(user_bots(uid))
    running = sum(1 for b in user_bots(uid) if b["_id"] in RUNNING and RUNNING[b["_id"]]["proc"].poll() is None)
    caption = f"""
<b>{BRAND}</b>
{G['div']}
👋 Welcome, {u.get('name', 'User')}!
📌 Plan: {u.get('plan', 'free')}
🤖 Bots: {bot_count} ({G['running'] if running else G['stopped']} {running} running)
{G['div']}
Choose an option below:
{FOOTER}
"""
    show_menu(chat_id, "main", caption, main_kb(is_admin(uid)), call)

def render_my_bots(chat_id, uid, call=None):
    bots = user_bots(uid)
    if not bots:
        caption = f"🤖 <b>No bots uploaded yet!</b>\n\nUpload your first bot using Upload Bot."
        show_menu(chat_id, "bots", caption, back_kb("menu_main"), call)
        return
    kb = types.InlineKeyboardMarkup()
    for b in bots:
        running = b["_id"] in RUNNING and RUNNING[b["_id"]]["proc"].poll() is None
        icon = G["running"] if running else G["stopped"]
        kb.add(Btn(f"{icon} {b.get('name', '?')}", callback_data=f"view_{b['_id']}", style="primary"))
    kb.add(Btn(f"{G['back']} Main Menu", callback_data="menu_main", style="primary"))
    caption = f"🤖 <b>Your Bots</b> ({len(bots)})\n{G['div']}"
    show_menu(chat_id, "bots", caption, kb, call)

def render_bot_view(chat_id, bid, call=None):
    b = find_bot(bid)
    if not b:
        show_menu(chat_id, "bots", "❌ Bot not found.", back_kb("my_bots"), call)
        return
    st = child_status(bid)
    caption = f"""
<b>🤖 {b.get('name', '?')}</b>
{G['div']}
📌 Status: {G['running'] if st['running'] else G['stopped']}
⏱️ Uptime: {st['uptime']}s
📅 Created: {b.get('created', '?')[:10]}
{G['div']}
"""
    show_menu(chat_id, "bots", caption, bot_kb(bid, st['running']), call)

def render_profile(chat_id, uid, call=None):
    u = get_user(uid) or {}
    caption = f"""
{G['user']} <b>Profile</b>
{G['div']}
🆔 ID: {uid}
👤 Name: {u.get('name', '?')}
📌 Plan: {u.get('plan', 'free')}
🤖 Bots: {len(user_bots(uid))}
📅 Joined: {u.get('joined', '?')[:10]}
{G['div']}
"""
    show_menu(chat_id, "profile", caption, back_kb("menu_main"), call)

def render_help(chat_id, call=None):
    caption = f"""
❓ <b>Help Guide</b>
{G['div']}
{G['upload']} <b>Upload Bot</b>
Send .py or .zip file

{G['folder']} <b>My Bots</b>
Start/Stop/Logs/Delete

{G['user']} <b>Profile</b>
Your account info

{G['div']}
<b>Commands:</b>
/start - Main menu
/menu - Menu
/help - Help
/cancel - Cancel
{FOOTER}
"""
    show_menu(chat_id, "help", caption, back_kb("menu_main"), call)

# ================================================================
#  ADMIN RENDERS
# ================================================================

def render_admin(chat_id, uid, call=None):
    if not is_admin(uid):
        show_menu(chat_id, "admin", "❌ Admin only.", back_kb("menu_main"), call)
        return
    d = db_load()
    bots = d.get("bots", {})
    running = sum(1 for bid in bots if bid in RUNNING and RUNNING[bid]["proc"].poll() is None)
    stopped = len(bots) - running
    caption = f"""
{G['shield']} <b>Admin Panel</b>
{G['div']}
👥 Users: {len(d['users'])}
🤖 Total Bots: {len(bots)}
{G['running']} Running: {running}
{G['stopped']} Stopped: {stopped}
🗑️ Deleted: {len(d.get('deleted_bots', []))}
{G['div']}
"""
    show_menu(chat_id, "admin", caption, admin_kb(), call)

def render_adm_bots(chat_id, call=None):
    d = db_load()
    bots = list(d["bots"].values())
    running = sum(1 for b in bots if b["_id"] in RUNNING and RUNNING[b["_id"]]["proc"].poll() is None)
    rows = []
    for b in bots[:30]:
        is_running = b["_id"] in RUNNING and RUNNING[b["_id"]]["proc"].poll() is None
        icon = G["running"] if is_running else G["stopped"]
        rows.append(f"{icon} {b.get('name', '?')} | Owner: {b.get('owner')}")
    caption = f"""
{G['stats']} <b>All Bots</b>
{G['div']}
📊 Total: {len(bots)}
{G['running']} Running: {running}
{G['stopped']} Stopped: {len(bots) - running}
{G['div']}
"""
    if rows:
        caption += "\n" + "\n".join(rows[:20])
    else:
        caption += "\nNo bots found."
    kb = types.InlineKeyboardMarkup()
    kb.add(Btn(f"{G['refresh']} Refresh", callback_data="adm_bots", style="primary"))
    kb.add(Btn(f"{G['back']} Admin", callback_data="admin", style="primary"))
    show_menu(chat_id, "admin", caption, kb, call)

def render_adm_users(chat_id, call=None):
    d = db_load()
    users = list(d["users"].values())
    rows = []
    for u in users[:30]:
        uid = str(u["id"])
        bc = sum(1 for b in d["bots"].values() if str(b.get("owner")) == uid)
        rows.append(f"👤 {u.get('name', '?')} | @{u.get('username', '—')} | ID: {uid} | Bots: {bc}")
    caption = f"""
{G['users']} <b>All Users</b>
{G['div']}
📊 Total: {len(users)}
{G['div']}
"""
    caption += "\n".join(rows) if rows else "No users found."
    kb = types.InlineKeyboardMarkup()
    kb.add(Btn(f"{G['refresh']} Refresh", callback_data="adm_users", style="primary"))
    kb.add(Btn(f"{G['back']} Admin", callback_data="admin", style="primary"))
    show_menu(chat_id, "admin", caption, kb, call)

def render_adm_ban(chat_id, call=None):
    USER_STATES[chat_id] = {"flow": "await_ban"}
    caption = f"""
🚫 <b>Ban / Unban</b>
{G['div']}
Format:
<code>ban USER_ID REASON</code>
<code>unban USER_ID</code>
Example:
<code>ban 123456789 Spamming</code>
"""
    show_menu(chat_id, "admin", caption, back_kb("admin"), call)

def render_adm_broadcast(chat_id, call=None):
    USER_STATES[chat_id] = {"flow": "await_broadcast"}
    caption = f"""
{G['broadcast']} <b>Broadcast</b>
{G['div']}
Send message to all users.
Type your message below:
"""
    show_menu(chat_id, "admin", caption, back_kb("admin"), call)

def render_adm_scan(chat_id, call=None):
    caption = f"""
{G['shield']} <b>Security Scan</b>
{G['div']}
Scans uploaded files for:
• Malicious code
• Bot tokens
• System commands
• Suspicious patterns
✅ Auto-scan enabled on upload.
"""
    show_menu(chat_id, "admin", caption, back_kb("admin"), call)

def render_adm_github(chat_id, call=None):
    status = G["ok"] if GITHUB_ENABLED else G["no"]
    caption = f"""
{G['cloud']} <b>GitHub Backup</b>
{G['div']}
Status: {status} {'Enabled' if GITHUB_ENABLED else 'Disabled'}
Repo: {GH_REPO if GH_REPO else 'Not set'}
Branch: {GH_BRANCH}
{G['div']}
Auto backup every 6 hours.
"""
    kb = types.InlineKeyboardMarkup()
    if GITHUB_ENABLED:
        kb.add(Btn(f"{G['cloud']} Backup Now", callback_data="adm_backup_now", style="success"))
    kb.add(Btn(f"{G['back']} Admin", callback_data="admin", style="primary"))
    show_menu(chat_id, "admin", caption, kb, call)

def render_adm_settings(chat_id, call=None):
    caption = f"""
{G['settings']} <b>Settings</b>
{G['div']}
🔐 Maintenance Mode: OFF
{G['cloud']} GitHub Backup: {'ON' if GITHUB_ENABLED else 'OFF'}
📸 Menu Photos: Change banners
{G['div']}
"""
    kb = types.InlineKeyboardMarkup()
    kb.add(Btn(f"📸 Menu Photos", callback_data="adm_photos", style="primary"))
    kb.add(Btn(f"{G['back']} Admin", callback_data="admin", style="primary"))
    show_menu(chat_id, "admin", caption, kb, call)

def render_adm_photos(chat_id, call=None):
    caption = f"""
📸 <b>Menu Photos</b>
{G['div']}
Tap a menu below to change its banner.
Send a photo after tapping.
{G['div']}
"""
    show_menu(chat_id, "admin", caption, photo_kb(), call)

# ================================================================
#  UPLOAD HANDLER
# ================================================================

USER_STATES: Dict[int, Dict] = {}

def handle_upload(m):
    uid = m.from_user.id
    doc = m.document
    if not doc:
        return
    fname = doc.file_name or "bot.py"
    try:
        f = bot.get_file(doc.file_id)
        raw = bot.download_file(f.file_path)
    except Exception as e:
        bot.reply_to(m, f"❌ Download failed: {e}")
        return
    scan = security_scan(raw)
    if not scan["safe"]:
        threats = "\n".join(scan["threats"])
        bot.reply_to(m, f"🚫 <b>Security Threat Detected!</b>\n\n{threats}\n\nFile blocked.", parse_mode="HTML")
        notify_owner(f"🚨 Security threat in {fname} from {uid}\n{threats}")
        return
    bot_id = secrets.token_hex(8)
    bot_dir = DIRS["sandbox"] / f"{uid}_{bot_id}"
    bot_dir.mkdir(parents=True, exist_ok=True)
    files_added = []
    if fname.endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                for member in zf.infolist():
                    if member.is_dir():
                        continue
                    rel = member.filename.replace("\\", "/")
                    if rel.startswith("/") or ".." in rel.split("/"):
                        continue
                    data = zf.read(member)
                    (bot_dir / Path(rel).name).write_bytes(data)
                    files_added.append(rel)
        except Exception as e:
            bot.reply_to(m, f"❌ Invalid ZIP: {e}")
            shutil.rmtree(bot_dir, ignore_errors=True)
            return
    else:
        (bot_dir / fname).write_bytes(raw)
        files_added.append(fname)
    name = Path(fname).stem[:30]
    if len(user_bots(uid)) >= 3:
        bot.reply_to(m, "❌ Free plan: max 3 bots. Delete old ones first.")
        shutil.rmtree(bot_dir, ignore_errors=True)
        return
    doc_db = {
        "_id": bot_id, "owner": uid, "name": name,
        "dir": str(bot_dir), "created": str(datetime.now(timezone.utc)),
        "files": files_added, "status": "stopped"
    }
    save_bot(doc_db)
    notify_owner(f"📤 New bot: {name} by {uid}")
    bot.reply_to(m, f"✅ <b>{name}</b> uploaded!\nBot ID: <code>{bot_id}</code>", parse_mode="HTML")
    start_child(doc_db)

# ================================================================
#  ADMIN HANDLERS (Text)
# ================================================================

def handle_ban(m):
    if not is_admin(m.from_user.id):
        return
    parts = m.text.strip().split()
    if len(parts) < 2:
        bot.reply_to(m, "❌ Format: ban USER_ID REASON")
        return
    op = parts[0].lower()
    try:
        uid = int(parts[1])
    except:
        bot.reply_to(m, "❌ Invalid user ID.")
        return
    d = db_load()
    if str(uid) not in d["users"]:
        bot.reply_to(m, "❌ User not found.")
        return
    if op == "ban":
        reason = " ".join(parts[2:]) or "No reason"
        d["users"][str(uid)]["banned"] = True
        d["users"][str(uid)]["ban_reason"] = reason
        db_save(d)
        audit(m.from_user.id, "ban", f"uid={uid} reason={reason}")
        bot.reply_to(m, f"✅ Banned {uid}")
        try:
            bot.send_message(uid, f"🚫 You have been banned. Reason: {reason}")
        except:
            pass
    elif op == "unban":
        d["users"][str(uid)]["banned"] = False
        d["users"][str(uid)]["ban_reason"] = ""
        db_save(d)
        audit(m.from_user.id, "unban", f"uid={uid}")
        bot.reply_to(m, f"✅ Unbanned {uid}")
        try:
            bot.send_message(uid, "✅ You have been unbanned.")
        except:
            pass
    else:
        bot.reply_to(m, "❌ Use 'ban' or 'unban'")

def handle_broadcast(m):
    if not is_admin(m.from_user.id):
        return
    text = m.text.strip()
    USER_STATES.pop(m.chat.id, None)
    if not text:
        bot.reply_to(m, "❌ Empty message.")
        return
    d = db_load()
    sent = 0
    for uid, u in d["users"].items():
        if u.get("banned"):
            continue
        try:
            bot.send_message(int(uid), f"📢 <b>Admin Broadcast</b>\n\n{text}", parse_mode="HTML")
            sent += 1
            time.sleep(0.05)
        except:
            pass
    audit(m.from_user.id, "broadcast", f"sent={sent}")
    bot.reply_to(m, f"✅ Broadcast sent to {sent} users.")

# ================================================================
#  PHOTO REPLACE
# ================================================================

def replace_menu_photo(key, file_bytes):
    if key not in ["main", "admin", "bots", "upload", "profile", "help"]:
        return False
    out_dir = DIRS["photos"]
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{key}.png"
    try:
        out.write_bytes(file_bytes)
        PHOTOS[key] = str(out)
        _PHOTO_FILE_IDS.pop(key, None)
        return True
    except:
        return False

# ================================================================
#  FLASK KEEP-ALIVE
# ================================================================

app = Flask(__name__)

@app.route("/")
def root():
    return jsonify({"status": "ok", "bots": len(RUNNING), "users": len(db_load()["users"])})

@app.route("/health")
def health():
    return jsonify({"status": "alive"})

def start_keepalive():
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT, debug=False), daemon=True).start()

# ================================================================
#  COMMAND HANDLERS
# ================================================================

def is_private(m):
    return m.chat.type == "private"

@bot.message_handler(commands=["start", "menu"])
def cmd_start(m):
    if not is_private(m):
        return
    uid = m.from_user.id
    create_user(uid, m.from_user.first_name, m.from_user.username or "")
    render_main(m.chat.id, uid)

@bot.message_handler(commands=["help"])
def cmd_help(m):
    if not is_private(m):
        return
    render_help(m.chat.id)

@bot.message_handler(commands=["cancel"])
def cmd_cancel(m):
    if not is_private(m):
        return
    USER_STATES.pop(m.chat.id, None)
    bot.reply_to(m, "✅ Cancelled.")

@bot.message_handler(content_types=["document"])
def on_doc(m):
    if not is_private(m):
        return
    uid = m.from_user.id
    create_user(uid, m.from_user.first_name, m.from_user.username or "")
    u = get_user(uid)
    if u and u.get("banned"):
        bot.reply_to(m, "🚫 You are banned.")
        return
    handle_upload(m)

@bot.message_handler(content_types=["photo"])
def on_photo(m):
    if not is_private(m):
        return
    uid = m.from_user.id
    st = USER_STATES.get(uid, {})
    if st.get("flow") == "await_photo" and is_admin(uid):
        key = st.get("photo_key")
        if key:
            try:
                ph = m.photo[-1]
                f = bot.get_file(ph.file_id)
                raw = bot.download_file(f.file_path)
                ok = replace_menu_photo(key, raw)
                USER_STATES.pop(uid, None)
                if ok:
                    bot.reply_to(m, f"✅ Banner updated for {key.title()}!")
                else:
                    bot.reply_to(m, "❌ Failed to update banner.")
            except Exception as e:
                bot.reply_to(m, f"❌ Error: {e}")

@bot.message_handler(func=lambda m: True, content_types=["text"])
def on_text(m):
    if not is_private(m):
        return
    uid = m.from_user.id
    create_user(uid, m.from_user.first_name, m.from_user.username or "")
    u = get_user(uid)
    if u and u.get("banned"):
        bot.reply_to(m, "🚫 You are banned.")
        return
    state = USER_STATES.get(m.chat.id, {}).get("flow", "")
    if state == "await_broadcast" and is_admin(uid):
        handle_broadcast(m)
        USER_STATES.pop(m.chat.id, None)
    elif state == "await_ban" and is_admin(uid):
        handle_ban(m)
        USER_STATES.pop(m.chat.id, None)

# ================================================================
#  CALLBACK HANDLERS
# ================================================================

@bot.callback_query_handler(func=lambda c: True)
def cb_handler(call):
    uid = call.from_user.id
    chat_id = call.message.chat.id
    data = call.data
    try:
        bot.answer_callback_query(call.id)
    except:
        pass
    
    if data == "menu_main":
        render_main(chat_id, uid, call)
        return
    if data == "my_bots":
        render_my_bots(chat_id, uid, call)
        return
    if data == "upload":
        show_text(chat_id, "📤 Send your .py or .zip file now.", back_kb("menu_main"), call)
        return
    if data == "profile":
        render_profile(chat_id, uid, call)
        return
    if data == "help":
        render_help(chat_id, call)
        return
    if data == "admin":
        render_admin(chat_id, uid, call)
        return
    if data == "adm_bots":
        render_adm_bots(chat_id, call)
        return
    if data == "adm_users":
        render_adm_users(chat_id, call)
        return
    if data == "adm_ban":
        render_adm_ban(chat_id, call)
        return
    if data == "adm_broadcast":
        render_adm_broadcast(chat_id, call)
        return
    if data == "adm_scan":
        render_adm_scan(chat_id, call)
        return
    if data == "adm_github":
        render_adm_github(chat_id, call)
        return
    if data == "adm_settings":
        render_adm_settings(chat_id, call)
        return
    if data == "adm_photos":
        render_adm_photos(chat_id, call)
        return
    
    if data.startswith("photo_"):
        key = data[6:]
        if is_admin(uid):
            USER_STATES[uid] = {"flow": "await_photo", "photo_key": key}
            show_text(chat_id, f"📸 Send a photo for <b>{key.title()}</b> banner.", back_kb("adm_photos"), call)
        else:
            show_text(chat_id, "❌ Admin only.", back_kb("admin"), call)
        return
    
    if data == "adm_backup_now":
        if not is_admin(uid):
            show_text(chat_id, "❌ Admin only.", back_kb("admin"), call)
            return
        show_text(chat_id, "⏳ Backing up...", back_kb("admin"), call)
        threading.Thread(target=lambda: do_backup(chat_id), daemon=True).start()
        return
    
    if data.startswith("view_"):
        bid = data[5:]
        render_bot_view(chat_id, bid, call)
        return
    
    if data.startswith("start_"):
        bid = data[6:]
        b = find_bot(bid)
        if b and (b.get("owner") == uid or is_admin(uid)):
            res = start_child(b)
            if res.get("ok"):
                show_text(chat_id, f"✅ Bot started.", back_kb(f"view_{bid}"), call)
            else:
                show_text(chat_id, f"❌ {res.get('error')}", back_kb(f"view_{bid}"), call)
        else:
            show_text(chat_id, "❌ Not yours.", back_kb("my_bots"), call)
        return
    
    if data.startswith("stop_"):
        bid = data[5:]
        b = find_bot(bid)
        if b and (b.get("owner") == uid or is_admin(uid)):
            stop_child(bid)
            show_text(chat_id, "✅ Bot stopped.", back_kb(f"view_{bid}"), call)
        else:
            show_text(chat_id, "❌ Not yours.", back_kb("my_bots"), call)
        return
    
    if data.startswith("restart_"):
        bid = data[8:]
        b = find_bot(bid)
        if b and (b.get("owner") == uid or is_admin(uid)):
            res = restart_child(b)
            if res.get("ok"):
                show_text(chat_id, "✅ Bot restarted.", back_kb(f"view_{bid}"), call)
            else:
                show_text(chat_id, f"❌ {res.get('error')}", back_kb(f"view_{bid}"), call)
        else:
            show_text(chat_id, "❌ Not yours.", back_kb("my_bots"), call)
        return
    
    if data.startswith("logs_"):
        bid = data[5:]
        b = find_bot(bid)
        if b and (b.get("owner") == uid or is_admin(uid)):
            st = child_status(bid)
            logs = "\n".join(st["logs"][-30:]) or "(no logs)"
            show_text(chat_id, f"📋 <b>Logs</b>\n{G['div']}\n<pre>{logs[:3000]}</pre>", 
                     back_kb(f"view_{bid}"), call)
        else:
            show_text(chat_id, "❌ Not yours.", back_kb("my_bots"), call)
        return
    
    if data.startswith("delete_"):
        bid = data[7:]
        b = find_bot(bid)
        if b and (b.get("owner") == uid or is_admin(uid)):
            stop_child(bid)
            try:
                shutil.rmtree(Path(b.get("dir", "")), ignore_errors=True)
            except:
                pass
            delete_bot_doc(bid)
            audit(uid, "delete_bot", f"bot={bid}")
            show_text(chat_id, "🗑️ Bot deleted.", back_kb("my_bots"), call)
        else:
            show_text(chat_id, "❌ Not yours.", back_kb("my_bots"), call)
        return
    
    show_text(chat_id, "❓ Unknown option.", back_kb("menu_main"), call)

def do_backup(chat_id):
    res = gh_backup_now()
    if res.get("ok"):
        bot.send_message(chat_id, f"✅ Backup done! Size: {res.get('size')} bytes")
    else:
        bot.send_message(chat_id, f"❌ Backup failed: {res.get('error')}")

# ================================================================
#  MAIN
# ================================================================

def banner():
    print("=" * 50)
    print(f"   {BRAND}")
    print(f"   Owner ID: {OWNER_ID}")
    print(f"   GitHub: {'ON' if GITHUB_ENABLED else 'OFF'}")
    print("=" * 50)

def main():
    banner()
    if GITHUB_ENABLED:
        threading.Thread(target=gh_auto_loop, daemon=True).start()
    start_keepalive()
    try:
        bot.set_my_commands([
            types.BotCommand("start", "Main menu"),
            types.BotCommand("menu", "Menu"),
            types.BotCommand("help", "Help"),
            types.BotCommand("cancel", "Cancel"),
        ])
    except:
        pass
    notify_owner("✅ Bot started!")
    print("[bot] polling...")
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=30)
        except Exception as e:
            print(f"[bot] error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
