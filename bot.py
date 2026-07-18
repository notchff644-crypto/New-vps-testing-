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
import hashlib
from collections import deque
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Tuple
from functools import lru_cache
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException
from flask import Flask, jsonify
import requests

# ================================================================
#  ═══════════════════════════════════════════════════════════════
#  CONFIG
#  ═══════════════════════════════════════════════════════════════
# ================================================================

BOT_TOKEN = "8764301541:AAGvFjYzPOcm47UaKeg1arNefqHanuQXbWc"
OWNER_ID = "8373276191"

if not BOT_TOKEN:
    print("❌ BOT_TOKEN set karo!")
    sys.exit(1)

BASE_DIR = Path(__file__).resolve().parent
DIRS = {
    "data": BASE_DIR / "storage" / "data",
    "uploads": BASE_DIR / "storage" / "uploads",
    "sandbox": BASE_DIR / "sandbox",
    "logs": BASE_DIR / "storage" / "logs",
}
for d in DIRS.values():
    d.mkdir(parents=True, exist_ok=True)

DB_FILE = DIRS["data"] / "db.json"
CACHE_FILE = DIRS["data"] / "cache.json"
PORT = int(os.environ.get("PORT", 10000))

# Rate limits
RATE_LIMIT = {"messages": 30, "uploads": 5, "window": 60}
_USER_RATE: Dict[int, Dict[str, Any]] = {}
_RATE_LOCK = threading.Lock()

# Bot instance
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True, num_threads=20)

# Running bots
RUNNING: Dict[str, Dict] = {}
_RUNNING_LOCK = threading.Lock()

# Database cache
_DB_CACHE: Dict[str, Any] = {}
_CACHE_TIME: Dict[str, float] = {}
_CACHE_LOCK = threading.Lock()
CACHE_TTL = 30  # seconds

# ================================================================
#  ═══════════════════════════════════════════════════════════════
#  RATE LIMITER
#  ═══════════════════════════════════════════════════════════════
# ================================================================

def rate_check(uid: int, action: str = "message") -> bool:
    """Check if user is rate limited"""
    with _RATE_LOCK:
        now = time.time()
        key = f"{uid}_{action}"
        
        if key not in _USER_RATE:
            _USER_RATE[key] = {"count": 0, "reset": now + RATE_LIMIT["window"]}
        
        data = _USER_RATE[key]
        if now > data["reset"]:
            data["count"] = 0
            data["reset"] = now + RATE_LIMIT["window"]
        
        limit = RATE_LIMIT["uploads"] if action == "upload" else RATE_LIMIT["messages"]
        
        if data["count"] >= limit:
            return False
        
        data["count"] += 1
        return True

def cleanup_rates():
    """Clean old rate limit entries"""
    while True:
        time.sleep(300)
        now = time.time()
        with _RATE_LOCK:
            to_delete = [k for k, v in _USER_RATE.items() if now > v["reset"]]
            for k in to_delete:
                del _USER_RATE[k]

# ================================================================
#  ═══════════════════════════════════════════════════════════════
#  DATABASE (With Caching)
#  ═══════════════════════════════════════════════════════════════
# ================================================================

def db_load(force: bool = False) -> Dict:
    """Load database with caching"""
    global _DB_CACHE, _CACHE_TIME
    
    if not force and DB_FILE.exists():
        mtime = DB_FILE.stat().st_mtime
        if DB_FILE.name in _CACHE_TIME and _CACHE_TIME.get(DB_FILE.name, 0) == mtime:
            return _DB_CACHE.get(DB_FILE.name, {})
    
    try:
        with open(DB_FILE, "r") as f:
            data = json.load(f)
    except:
        data = {"users": {}, "bots": {}, "admins": {}, "audit": [], "deleted_bots": [], "pending": {}}
    
    # Ensure all keys exist
    defaults = {"users": {}, "bots": {}, "admins": {}, "audit": [], "deleted_bots": [], "pending": {}}
    for k, v in defaults.items():
        if k not in data:
            data[k] = v
    
    with _CACHE_LOCK:
        _DB_CACHE[DB_FILE.name] = data
        _CACHE_TIME[DB_FILE.name] = DB_FILE.stat().st_mtime if DB_FILE.exists() else time.time()
    
    return data

def db_save(data: Dict) -> None:
    """Save database and update cache"""
    with _CACHE_LOCK:
        _DB_CACHE[DB_FILE.name] = data
        _CACHE_TIME[DB_FILE.name] = time.time()
    
    with open(DB_FILE, "w") as f:
        json.dumps(data, indent=2, default=str)
        f.write(json.dumps(data, indent=2, default=str))

# ================================================================
#  ═══════════════════════════════════════════════════════════════
#  USER FUNCTIONS
#  ═══════════════════════════════════════════════════════════════
# ================================================================

def get_user(uid: int) -> Optional[Dict]:
    d = db_load()
    return d["users"].get(str(uid))

def create_user(uid: int, name: str, username: str = "") -> Dict:
    d = db_load()
    key = str(uid)
    if key not in d["users"]:
        d["users"][key] = {
            "id": uid,
            "name": name,
            "username": username,
            "joined": datetime.now(timezone.utc).isoformat(),
            "plan": "free",
            "banned": False,
            "ban_reason": "",
            "stats": {"commands": 0, "uploads": 0}
        }
        db_save(d)
    return d["users"][key]

def is_admin(uid: int) -> bool:
    return uid == OWNER_ID or str(uid) in db_load().get("admins", {})

def is_banned(uid: int) -> bool:
    u = get_user(uid)
    return u and u.get("banned", False)

def user_bots(uid: int) -> List[Dict]:
    d = db_load()
    return [b for b in d["bots"].values() if b.get("owner") == uid]

def find_bot(bot_id: str) -> Optional[Dict]:
    return db_load()["bots"].get(bot_id)

def save_bot(doc: Dict) -> None:
    d = db_load()
    d["bots"][doc["_id"]] = doc
    db_save(d)

def delete_bot_doc(bot_id: str) -> None:
    d = db_load()
    b = d["bots"].get(bot_id)
    if b:
        d["deleted_bots"].append({
            "bot_id": bot_id,
            "name": b.get("name"),
            "owner": b.get("owner"),
            "deleted_at": datetime.now(timezone.utc).isoformat()
        })
        d["deleted_bots"] = d["deleted_bots"][-200:]
    d["bots"].pop(bot_id, None)
    db_save(d)

# ================================================================
#  ═══════════════════════════════════════════════════════════════
#  BOT DETECTION & DEPENDENCIES
#  ═══════════════════════════════════════════════════════════════
# ================================================================

def detect_entry(bot_dir: Path) -> Tuple[Optional[str], Optional[str]]:
    """Detect entry file for bot"""
    entries = ["bot.py", "main.py", "app.py", "run.py", "index.js", "bot.js", "server.js"]
    
    for entry in entries:
        if (bot_dir / entry).exists():
            return ("python" if entry.endswith(".py") else "node", entry)
    
    # Search for any .py file
    py_files = list(bot_dir.glob("*.py"))
    if py_files:
        return ("python", py_files[0].name)
    
    return (None, None)

def install_deps(bot_dir: Path, kind: str) -> None:
    """Install dependencies for bot"""
    if kind != "python":
        return
    
    req = bot_dir / "requirements.txt"
    if req.exists():
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(req)],
                cwd=str(bot_dir),
                capture_output=True,
                timeout=120
            )
        except:
            pass

# ================================================================
#  ═══════════════════════════════════════════════════════════════
#  SANDBOX RUNNER (With Auto-Restart)
#  ═══════════════════════════════════════════════════════════════
# ================================================================

def start_child(b: Dict) -> Dict:
    """Start a bot with proper sandboxing"""
    bid = b["_id"]
    bot_dir = Path(b["dir"])
    
    if not bot_dir.exists():
        return {"ok": False, "error": "Bot folder missing"}
    
    kind, entry = detect_entry(bot_dir)
    if not kind:
        return {"ok": False, "error": "No entry file found"}
    
    # Install dependencies
    install_deps(bot_dir, kind)
    
    # Build command
    if kind == "python":
        cmd = [sys.executable, "-u", entry]
    else:
        cmd = ["node", entry]
    
    # Environment
    env = {**os.environ, "HOME": str(bot_dir), "TMPDIR": str(bot_dir / "tmp")}
    (bot_dir / "tmp").mkdir(exist_ok=True)
    
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(bot_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid if os.name == "posix" else None,
            bufsize=1,
            text=True
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}
    
    with _RUNNING_LOCK:
        RUNNING[bid] = {
            "proc": proc,
            "log": deque(maxlen=200),
            "started": time.time(),
            "name": b.get("name"),
            "owner": b.get("owner"),
            "kind": kind,
            "pid": proc.pid,
            "restart_count": 0,
            "last_restart": 0
        }
    
    # Start log reader
    threading.Thread(target=_drain_proc, args=(bid, proc), daemon=True).start()
    
    # Update status
    b["status"] = "running"
    b["last_started"] = datetime.now(timezone.utc).isoformat()
    save_bot(b)
    
    return {"ok": True, "pid": proc.pid, "kind": kind}

def _drain_proc(bid: str, proc: subprocess.Popen) -> None:
    """Read logs from bot process"""
    try:
        for line in iter(proc.stdout.readline, ""):
            if not line:
                break
            with _RUNNING_LOCK:
                if bid in RUNNING:
                    RUNNING[bid]["log"].append(line.strip())
    except:
        pass
    finally:
        # Process ended
        with _RUNNING_LOCK:
            if bid in RUNNING:
                RUNNING[bid]["proc"] = None
        
        # Check if should auto-restart
        b = find_bot(bid)
        if b and b.get("status") == "running":
            # Auto-restart on crash
            with _RUNNING_LOCK:
                info = RUNNING.get(bid)
                if info:
                    info["restart_count"] = info.get("restart_count", 0) + 1
                    info["last_restart"] = time.time()
            
            # Only restart if not stopped manually
            if info and info.get("restart_count", 0) <= 5:
                time.sleep(3)
                start_child(b)
            else:
                b["status"] = "crashed"
                save_bot(b)
        else:
            b = find_bot(bid)
            if b:
                b["status"] = "stopped"
                save_bot(b)

def stop_child(bid: str) -> Dict:
    """Stop a running bot"""
    with _RUNNING_LOCK:
        info = RUNNING.get(bid)
        if not info:
            b = find_bot(bid)
            if b:
                b["status"] = "stopped"
                save_bot(b)
            return {"ok": True}
        
        proc = info.get("proc")
    
    if proc:
        try:
            if os.name == "posix":
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except:
                    proc.terminate()
            else:
                proc.terminate()
            proc.wait(timeout=5)
        except:
            try:
                if os.name == "posix":
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                else:
                    proc.kill()
            except:
                pass
    
    with _RUNNING_LOCK:
        RUNNING.pop(bid, None)
    
    b = find_bot(bid)
    if b:
        b["status"] = "stopped"
        save_bot(b)
    
    return {"ok": True}

def restart_child(b: Dict) -> Dict:
    """Restart a bot"""
    stop_child(b["_id"])
    time.sleep(2)
    return start_child(b)

def child_status(bid: str) -> Dict:
    """Get bot status"""
    with _RUNNING_LOCK:
        info = RUNNING.get(bid)
    
    if not info:
        return {"running": False, "logs": [], "uptime": 0}
    
    proc = info.get("proc")
    running = proc is not None and proc.poll() is None
    
    return {
        "running": running,
        "logs": list(info.get("log", [])),
        "uptime": int(time.time() - info.get("started", time.time())) if running else 0,
        "pid": info.get("pid"),
        "kind": info.get("kind", "python"),
        "restart_count": info.get("restart_count", 0)
    }

def get_running_count() -> int:
    """Get number of running bots"""
    with _RUNNING_LOCK:
        return sum(1 for info in RUNNING.values() if info.get("proc") and info["proc"].poll() is None)

def get_all_bots_status() -> List[Dict]:
    """Get status of all bots"""
    result = []
    d = db_load()
    for bid, b in d["bots"].items():
        st = child_status(bid)
        result.append({
            "id": bid,
            "name": b.get("name", "?"),
            "owner": b.get("owner"),
            "running": st["running"],
            "uptime": st["uptime"],
            "created": b.get("created", "?")
        })
    return result

# ================================================================
#  ═══════════════════════════════════════════════════════════════
#  SECURITY SCAN
#  ═══════════════════════════════════════════════════════════════
# ================================================================

def security_scan(content: bytes) -> Dict:
    """Scan file for security threats"""
    threats = []
    try:
        text = content.decode("utf-8", errors="ignore")
    except:
        return {"safe": True, "threats": [], "score": 0}
    
    # Dangerous patterns
    dangerous = [
        ("os.system", "System command execution"),
        ("subprocess.call", "Subprocess execution"),
        ("subprocess.Popen", "Subprocess execution"),
        ("eval(", "Dynamic code execution"),
        ("exec(", "Dynamic code execution"),
        ("__import__('os')", "Dynamic OS import"),
        ("open('/etc/passwd'", "Sensitive file access"),
        ("/proc/self/environ", "Environment access"),
        ("base64.b64decode", "Obfuscation"),
        ("marshal.loads", "Bytecode execution"),
        ("shutil.rmtree", "File deletion"),
        ("os.walk('/'", "Root directory scan"),
        ("requests.post", "External data exfiltration"),
    ]
    
    for pattern, desc in dangerous:
        if pattern in text:
            threats.append(desc)
    
    # Bot token detection
    token_pattern = r'\d{8,10}:[A-Za-z0-9_-]{35}'
    if re.search(token_pattern, text):
        threats.append("⚠️ Bot token found in code!")
    
    # IP/URL patterns
    if re.search(r'https?://[^\s]+', text):
        # Check for pastebin/gist
        if "pastebin.com" in text or "gist.github.com" in text:
            threats.append("External code download from pastebin/gist")
    
    score = min(len(threats) * 15, 100)
    
    return {
        "safe": len(threats) == 0,
        "threats": threats,
        "score": score,
        "threat_count": len(threats)
    }

# ================================================================
#  ═══════════════════════════════════════════════════════════════
#  GITHUB BACKUP
#  ═══════════════════════════════════════════════════════════════
# ================================================================

GITHUB_ENABLED = False
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GH_REPO = os.environ.get("GITHUB_REPO", "")
GH_BRANCH = os.environ.get("GITHUB_BRANCH", "main")

if GH_TOKEN and GH_REPO and "/" in GH_REPO:
    GITHUB_ENABLED = True

def gh_backup_now() -> Dict:
    """Backup to GitHub"""
    if not GITHUB_ENABLED:
        return {"ok": False, "error": "GitHub not configured"}
    
    try:
        data = db_load()
        content = json.dumps(data, indent=2)
        
        url = f"https://api.github.com/repos/{GH_REPO}/contents/backup.json"
        headers = {
            "Authorization": f"token {GH_TOKEN}",
            "Accept": "application/vnd.github+json"
        }
        
        # Get current SHA
        sha = None
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                sha = r.json().get("sha")
        except:
            pass
        
        payload = {
            "message": f"Backup {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}",
            "content": base64.b64encode(content.encode()).decode(),
            "branch": GH_BRANCH
        }
        if sha:
            payload["sha"] = sha
        
        r = requests.put(url, headers=headers, json=payload, timeout=30)
        
        if r.status_code in (200, 201):
            return {"ok": True, "size": len(content)}
        else:
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def gh_auto_loop():
    """Auto backup every 6 hours"""
    while True:
        time.sleep(21600)  # 6 hours
        if GITHUB_ENABLED:
            res = gh_backup_now()
            if res.get("ok"):
                print(f"[backup] ok: {res.get('size')} bytes")
            else:
                print(f"[backup] failed: {res.get('error')}")

# ================================================================
#  ═══════════════════════════════════════════════════════════════
#  KEYBOARDS
#  ═══════════════════════════════════════════════════════════════
# ================================================================

def main_kb(admin: bool = False) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📤 Upload Bot", callback_data="upload"),
        types.InlineKeyboardButton("🤖 My Bots", callback_data="my_bots"),
    )
    kb.add(
        types.InlineKeyboardButton("👤 Profile", callback_data="profile"),
        types.InlineKeyboardButton("❓ Help", callback_data="help"),
    )
    if admin:
        kb.add(types.InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin"))
    return kb

def back_kb(target: str = "menu_main") -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔙 Back", callback_data=target))
    return kb

def bot_kb(bid: str, running: bool = False) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    if running:
        kb.add(
            types.InlineKeyboardButton("⏹ Stop", callback_data=f"stop_{bid}"),
            types.InlineKeyboardButton("🔄 Restart", callback_data=f"restart_{bid}"),
        )
    else:
        kb.add(
            types.InlineKeyboardButton("▶️ Start", callback_data=f"start_{bid}"),
        )
    kb.add(
        types.InlineKeyboardButton("📋 Logs", callback_data=f"logs_{bid}"),
        types.InlineKeyboardButton("🗑️ Delete", callback_data=f"delete_{bid}"),
    )
    kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="my_bots"))
    return kb

def admin_kb() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📊 All Bots", callback_data="adm_bots"),
        types.InlineKeyboardButton("👥 All Users", callback_data="adm_users"),
    )
    kb.add(
        types.InlineKeyboardButton("🚫 Ban/Unban", callback_data="adm_ban"),
        types.InlineKeyboardButton("📢 Broadcast", callback_data="adm_broadcast"),
    )
    kb.add(
        types.InlineKeyboardButton("🔒 Security Scan", callback_data="adm_scan"),
        types.InlineKeyboardButton("💾 GitHub Backup", callback_data="adm_github"),
    )
    kb.add(
        types.InlineKeyboardButton("📋 Pending", callback_data="adm_pending"),
        types.InlineKeyboardButton("⚙️ Settings", callback_data="adm_settings"),
    )
    kb.add(types.InlineKeyboardButton("🔙 Main", callback_data="menu_main"))
    return kb

# ================================================================
#  ═══════════════════════════════════════════════════════════════
#  RENDER FUNCTIONS
#  ═══════════════════════════════════════════════════════════════
# ================================================================

def render_menu(chat_id: int, text: str, kb: Optional[types.InlineKeyboardMarkup] = None, 
                call: Optional[types.CallbackQuery] = None) -> None:
    """Render menu with edit or send"""
    if call:
        try:
            bot.edit_message_text(text, chat_id, call.message.message_id,
                                reply_markup=kb, parse_mode="HTML")
            return
        except:
            pass
    bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")

def render_main(chat_id: int, uid: int, call: Optional[types.CallbackQuery] = None) -> None:
    u = get_user(uid) or {}
    bot_count = len(user_bots(uid))
    running = sum(1 for b in user_bots(uid) if b["_id"] in RUNNING and RUNNING[b["_id"]].get("proc") and RUNNING[b["_id"]]["proc"].poll() is None)
    
    text = f"""
<b>🤖 Simran Hosting Bot</b>
━━━━━━━━━━━━━━━━━━
👋 Welcome, {u.get('name', 'User')}!
📌 Plan: {u.get('plan', 'free')}
🤖 Bots: {bot_count} ({running} running)
━━━━━━━━━━━━━━━━━━
Choose an option below:
"""
    render_menu(chat_id, text, main_kb(is_admin(uid)), call)

def render_my_bots(chat_id: int, uid: int, call: Optional[types.CallbackQuery] = None) -> None:
    bots = user_bots(uid)
    if not bots:
        text = "🤖 <b>No bots uploaded yet!</b>\n\nUpload your first bot using Upload Bot."
        render_menu(chat_id, text, back_kb("menu_main"), call)
        return
    
    kb = types.InlineKeyboardMarkup()
    for b in bots:
        running = b["_id"] in RUNNING and RUNNING[b["_id"]].get("proc") and RUNNING[b["_id"]]["proc"].poll() is None
        icon = "🟢" if running else "🔴"
        kb.add(types.InlineKeyboardButton(f"{icon} {b.get('name', '?')}", callback_data=f"view_{b['_id']}"))
    kb.add(types.InlineKeyboardButton("🔙 Main Menu", callback_data="menu_main"))
    render_menu(chat_id, f"🤖 <b>Your Bots</b> ({len(bots)})", kb, call)

def render_bot_view(chat_id: int, bid: str, call: Optional[types.CallbackQuery] = None) -> None:
    b = find_bot(bid)
    if not b:
        render_menu(chat_id, "❌ Bot not found.", back_kb("my_bots"), call)
        return
    
    st = child_status(bid)
    text = f"""
<b>🤖 {b.get('name', '?')}</b>
━━━━━━━━━━━━━━━━━━
📌 Status: {'🟢 Running' if st['running'] else '🔴 Stopped'}
⏱️ Uptime: {st['uptime']}s
📅 Created: {b.get('created', '?')[:10]}
━━━━━━━━━━━━━━━━━━
"""
    render_menu(chat_id, text, bot_kb(bid, st['running']), call)

def render_profile(chat_id: int, uid: int, call: Optional[types.CallbackQuery] = None) -> None:
    u = get_user(uid) or {}
    text = f"""
<b>👤 Profile</b>
━━━━━━━━━━━━━━━━━━
🆔 ID: {uid}
👤 Name: {u.get('name', '?')}
📌 Plan: {u.get('plan', 'free')}
🤖 Bots: {len(user_bots(uid))}
📅 Joined: {u.get('joined', '?')[:10]}
━━━━━━━━━━━━━━━━━━
"""
    render_menu(chat_id, text, back_kb("menu_main"), call)

def render_help(chat_id: int, call: Optional[types.CallbackQuery] = None) -> None:
    text = """
<b>❓ Help Guide</b>
━━━━━━━━━━━━━━━━━━
📤 <b>Upload Bot</b>
Send .py or .zip file

🤖 <b>My Bots</b>
Start/Stop/Logs/Delete

👤 <b>Profile</b>
Your account info

━━━━━━━━━━━━━━━━━━
<b>Commands:</b>
/start - Main menu
/menu - Menu
/help - Help
/cancel - Cancel
"""
    render_menu(chat_id, text, back_kb("menu_main"), call)

# ================================================================
#  ═══════════════════════════════════════════════════════════════
#  ADMIN FUNCTIONS
#  ═══════════════════════════════════════════════════════════════
# ================================================================

USER_STATES: Dict[int, Dict] = {}

def render_admin(chat_id: int, uid: int, call: Optional[types.CallbackQuery] = None) -> None:
    if not is_admin(uid):
        render_menu(chat_id, "❌ Admin only.", back_kb("menu_main"), call)
        return
    
    d = db_load()
    bots = d.get("bots", {})
    running = get_running_count()
    stopped = len(bots) - running
    
    text = f"""
<b>⚙️ Admin Panel</b>
━━━━━━━━━━━━━━━━━━
👥 Users: {len(d['users'])}
🤖 Total Bots: {len(bots)}
🟢 Running: {running}
🔴 Stopped: {stopped}
📋 Pending: {len(d.get('pending', {}))}
🗑️ Deleted: {len(d.get('deleted_bots', []))}
━━━━━━━━━━━━━━━━━━
"""
    render_menu(chat_id, text, admin_kb(), call)

def render_adm_bots(chat_id: int, call: Optional[types.CallbackQuery] = None) -> None:
    d = db_load()
    bots = list(d["bots"].values())
    running = get_running_count()
    
    rows = []
    for b in bots[:50]:
        is_running = b["_id"] in RUNNING and RUNNING[b["_id"]].get("proc") and RUNNING[b["_id"]]["proc"].poll() is None
        icon = "🟢" if is_running else "🔴"
        rows.append(f"{icon} {b.get('name', '?')} | Owner: {b.get('owner')}")
    
    text = f"""
<b>🤖 All Bots</b>
━━━━━━━━━━━━━━━━━━
📊 Total: {len(bots)}
🟢 Running: {running}
🔴 Stopped: {len(bots) - running}
━━━━━━━━━━━━━━━━━━
"""
    if rows:
        text += "\n" + "\n".join(rows[:25])
    else:
        text += "\nNo bots found."
    
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔄 Refresh", callback_data="adm_bots"))
    kb.add(types.InlineKeyboardButton("🔙 Admin", callback_data="admin"))
    render_menu(chat_id, text, kb, call)

def render_adm_users(chat_id: int, call: Optional[types.CallbackQuery] = None) -> None:
    d = db_load()
    users = list(d["users"].values())
    
    rows = []
    for u in users[:50]:
        uid = str(u["id"])
        bc = sum(1 for b in d["bots"].values() if str(b.get("owner")) == uid)
        banned = "🚫" if u.get("banned") else ""
        rows.append(f"{banned} {u.get('name', '?')} | @{u.get('username', '—')} | ID: {uid} | Bots: {bc}")
    
    text = f"""
<b>👥 All Users</b>
━━━━━━━━━━━━━━━━━━
📊 Total: {len(users)}
━━━━━━━━━━━━━━━━━━
"""
    text += "\n".join(rows) if rows else "No users found."
    
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔄 Refresh", callback_data="adm_users"))
    kb.add(types.InlineKeyboardButton("🔙 Admin", callback_data="admin"))
    render_menu(chat_id, text, kb, call)

def render_adm_ban(chat_id: int, call: Optional[types.CallbackQuery] = None) -> None:
    USER_STATES[chat_id] = {"flow": "await_ban"}
    text = """
<b>🚫 Ban / Unban</b>
━━━━━━━━━━━━━━━━━━
Format:
<code>ban USER_ID REASON</code>
<code>unban USER_ID</code>

Example:
<code>ban 123456789 Spamming</code>
"""
    render_menu(chat_id, text, back_kb("admin"), call)

def render_adm_broadcast(chat_id: int, call: Optional[types.CallbackQuery] = None) -> None:
    USER_STATES[chat_id] = {"flow": "await_broadcast"}
    text = """
<b>📢 Broadcast</b>
━━━━━━━━━━━━━━━━━━
Send message to all users.
Type your message below:
"""
    render_menu(chat_id, text, back_kb("admin"), call)

def render_adm_scan(chat_id: int, call: Optional[types.CallbackQuery] = None) -> None:
    text = """
<b>🔒 Security Scan</b>
━━━━━━━━━━━━━━━━━━
Scans uploaded files for:
• Malicious code
• Bot tokens
• System commands
• Suspicious patterns

✅ Auto-scan enabled on upload.
📊 Threat level: Moderate
"""
    render_menu(chat_id, text, back_kb("admin"), call)

def render_adm_github(chat_id: int, call: Optional[types.CallbackQuery] = None) -> None:
    status = "✅ Enabled" if GITHUB_ENABLED else "❌ Disabled"
    last_backup = "N/A"
    
    text = f"""
<b>💾 GitHub Backup</b>
━━━━━━━━━━━━━━━━━━
Status: {status}
Repo: {GH_REPO if GH_REPO else 'Not set'}
Branch: {GH_BRANCH}
Last Backup: {last_backup}
━━━━━━━━━━━━━━━━━━
Auto backup every 6 hours.
"""
    kb = types.InlineKeyboardMarkup()
    if GITHUB_ENABLED:
        kb.add(types.InlineKeyboardButton("💾 Backup Now", callback_data="adm_backup_now"))
    kb.add(types.InlineKeyboardButton("🔙 Admin", callback_data="admin"))
    render_menu(chat_id, text, kb, call)

def render_adm_pending(chat_id: int, call: Optional[types.CallbackQuery] = None) -> None:
    d = db_load()
    pending = d.get("pending", {})
    
    if not pending:
        text = "📋 <b>No pending uploads.</b>"
    else:
        rows = []
        for bid, info in pending.items():
            rows.append(f"• {info.get('name', '?')} | User: {info.get('user_id')}")
        text = f"📋 <b>Pending Uploads</b> ({len(pending)})\n━━━━━━━━━━━━━━━━━━\n" + "\n".join(rows)
    
    render_menu(chat_id, text, back_kb("admin"), call)

def render_adm_settings(chat_id: int, call: Optional[types.CallbackQuery] = None) -> None:
    d = db_load()
    text = f"""
<b>⚙️ Settings</b>
━━━━━━━━━━━━━━━━━━
🔐 Maintenance Mode: OFF
📦 GitHub Backup: {'ON' if GITHUB_ENABLED else 'OFF'}
🤖 Total Bots: {len(d['bots'])}
👥 Total Users: {len(d['users'])}
🔄 Auto-Restart: ON
📊 Rate Limit: {RATE_LIMIT['messages']}/min
━━━━━━━━━━━━━━━━━━
"""
    render_menu(chat_id, text, back_kb("admin"), call)

# ================================================================
#  ═══════════════════════════════════════════════════════════════
#  ADMIN HANDLERS (Text)
#  ═══════════════════════════════════════════════════════════════
# ================================================================

def handle_ban(m: types.Message) -> None:
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

def handle_broadcast(m: types.Message) -> None:
    if not is_admin(m.from_user.id):
        return
    text = m.text.strip()
    USER_STATES.pop(m.chat.id, None)
    
    if not text:
        bot.reply_to(m, "❌ Empty message.")
        return
    
    d = db_load()
    sent = 0
    failed = 0
    
    for uid, u in d["users"].items():
        if u.get("banned"):
            continue
        try:
            bot.send_message(int(uid), f"📢 <b>Admin Broadcast</b>\n\n{text}", parse_mode="HTML")
            sent += 1
            time.sleep(0.05)  # Avoid rate limits
        except:
            failed += 1
    
    audit(m.from_user.id, "broadcast", f"sent={sent} failed={failed}")
    bot.reply_to(m, f"✅ Broadcast sent to {sent} users. Failed: {failed}")

# ================================================================
#  ═══════════════════════════════════════════════════════════════
#  AUDIT
#  ═══════════════════════════════════════════════════════════════
# ================================================================

def audit(uid: int, action: str, detail: str = "") -> None:
    d = db_load()
    d["audit"].append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "uid": uid,
        "action": action,
        "detail": detail
    })
    d["audit"] = d["audit"][-500:]
    db_save(d)

def notify_owner(text: str) -> None:
    try:
        bot.send_message(OWNER_ID, text, parse_mode="HTML")
    except:
        pass

# ================================================================
#  ═══════════════════════════════════════════════════════════════
#  UPLOAD HANDLER
#  ═══════════════════════════════════════════════════════════════
# ================================================================

def handle_upload(m: types.Message) -> None:
    uid = m.from_user.id
    doc = m.document
    if not doc:
        return
    
    # Rate limit
    if not rate_check(uid, "upload"):
        bot.reply_to(m, "⏳ Slow down! Max 5 uploads per minute.")
        return
    
    fname = doc.file_name or "bot.py"
    
    # Check file size
    if doc.file_size and doc.file_size > 75 * 1024 * 1024:
        bot.reply_to(m, "❌ File too large! Max 75MB.")
        return
    
    try:
        f = bot.get_file(doc.file_id)
        raw = bot.download_file(f.file_path)
    except Exception as e:
        bot.reply_to(m, f"❌ Download failed: {e}")
        return
    
    # Security scan
    scan = security_scan(raw)
    if not scan["safe"]:
        threats = "\n".join(scan["threats"])
        bot.reply_to(m, f"🚫 <b>Security Threat Detected!</b>\n\n{threats}\n\nFile blocked.", parse_mode="HTML")
        notify_owner(f"🚨 Security threat in {fname} from {uid}\n{threats}")
        return
    
    # Check bot limit (Free: 5 bots)
    if len(user_bots(uid)) >= 5:
        bot.reply_to(m, "❌ Free plan: max 5 bots. Delete old ones first.")
        return
    
    # Create bot
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
                    target = bot_dir / Path(rel).name
                    target.write_bytes(data)
                    files_added.append(rel)
        except Exception as e:
            bot.reply_to(m, f"❌ Invalid ZIP: {e}")
            shutil.rmtree(bot_dir, ignore_errors=True)
            return
    else:
        (bot_dir / fname).write_bytes(raw)
        files_added.append(fname)
    
    name = Path(fname).stem[:30]
    
    doc_db = {
        "_id": bot_id,
        "owner": uid,
        "name": name,
        "dir": str(bot_dir),
        "created": datetime.now(timezone.utc).isoformat(),
        "files": files_added,
        "status": "stopped",
        "scan_score": scan["score"]
    }
    
    save_bot(doc_db)
    audit(uid, "upload", f"bot={bot_id} name={name}")
    notify_owner(f"📤 New bot: {name} by {uid}")
    
    bot.reply_to(m, f"✅ <b>{name}</b> uploaded!\nBot ID: <code>{bot_id}</code>\nScan Score: {scan['score']}/100", parse_mode="HTML")
    
    # Auto-start
    start_child(doc_db)

# ================================================================
#  ═══════════════════════════════════════════════════════════════
#  FLASK KEEP-ALIVE
#  ═══════════════════════════════════════════════════════════════
# ================================================================

app = Flask(__name__)

@app.route("/")
def root():
    d = db_load()
    return jsonify({
        "status": "ok",
        "bots": len(d["bots"]),
        "users": len(d["users"]),
        "running": get_running_count(),
        "uptime": int(time.time() - START_TIME) if 'START_TIME' in globals() else 0
    })

@app.route("/health")
def health():
    return jsonify({"status": "alive", "timestamp": datetime.now(timezone.utc).isoformat()})

@app.route("/stats")
def stats():
    d = db_load()
    return jsonify({
        "users": len(d["users"]),
        "bots": len(d["bots"]),
        "running": get_running_count(),
        "deleted": len(d.get("deleted_bots", [])),
        "audit": len(d.get("audit", []))
    })

def start_keepalive():
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True), daemon=True).start()

# ================================================================
#  ═══════════════════════════════════════════════════════════════
#  COMMAND HANDLERS
#  ═══════════════════════════════════════════════════════════════
# ================================================================

def is_private(m):
    return m.chat.type == "private"

@bot.message_handler(commands=["start", "menu"])
def cmd_start(m: types.Message):
    if not is_private(m):
        return
    uid = m.from_user.id
    create_user(uid, m.from_user.first_name or "User", m.from_user.username or "")
    render_main(m.chat.id, uid)

@bot.message_handler(commands=["help"])
def cmd_help(m: types.Message):
    if not is_private(m):
        return
    render_help(m.chat.id)

@bot.message_handler(commands=["cancel"])
def cmd_cancel(m: types.Message):
    if not is_private(m):
        return
    USER_STATES.pop(m.chat.id, None)
    bot.reply_to(m, "✅ Cancelled.")

@bot.message_handler(commands=["id"])
def cmd_id(m: types.Message):
    if not is_private(m):
        return
    bot.reply_to(m, f"<code>{m.from_user.id}</code>", parse_mode="HTML")

@bot.message_handler(commands=["admin"])
def cmd_admin(m: types.Message):
    if not is_private(m):
        return
    if not is_admin(m.from_user.id):
        bot.reply_to(m, "❌ Admin only.")
        return
    render_admin(m.chat.id, m.from_user.id)

@bot.message_handler(content_types=["document"])
def on_doc(m: types.Message):
    if not is_private(m):
        return
    uid = m.from_user.id
    create_user(uid, m.from_user.first_name or "User", m.from_user.username or "")
    
    if is_banned(uid):
        bot.reply_to(m, "🚫 You are banned.")
        return
    
    if not rate_check(uid, "message"):
        bot.reply_to(m, "⏳ Slow down!")
        return
    
    handle_upload(m)

@bot.message_handler(func=lambda m: True, content_types=["text"])
def on_text(m: types.Message):
    if not is_private(m):
        return
    
    uid = m.from_user.id
    create_user(uid, m.from_user.first_name or "User", m.from_user.username or "")
    
    if is_banned(uid):
        bot.reply_to(m, "🚫 You are banned.")
        return
    
    if not rate_check(uid, "message"):
        bot.reply_to(m, "⏳ Slow down!")
        return
    
    state = USER_STATES.get(m.chat.id, {}).get("flow", "")
    
    if state == "await_broadcast" and is_admin(uid):
        handle_broadcast(m)
        USER_STATES.pop(m.chat.id, None)
    elif state == "await_ban" and is_admin(uid):
        handle_ban(m)
        USER_STATES.pop(m.chat.id, None)
    else:
        # Ignore other text messages
        pass

# ================================================================
#  ═══════════════════════════════════════════════════════════════
#  CALLBACK HANDLERS
#  ═══════════════════════════════════════════════════════════════
# ================================================================

@bot.callback_query_handler(func=lambda c: True)
def cb_handler(call: types.CallbackQuery):
    uid = call.from_user.id
    chat_id = call.message.chat.id
    data = call.data
    
    try:
        bot.answer_callback_query(call.id)
    except:
        pass
    
    # Main menu
    if data == "menu_main":
        render_main(chat_id, uid, call)
        return
    
    # My Bots
    if data == "my_bots":
        render_my_bots(chat_id, uid, call)
        return
    
    # Upload
    if data == "upload":
        render_menu(chat_id, "📤 Send your .py or .zip file now.", back_kb("menu_main"), call)
        return
    
    # Profile
    if data == "profile":
        render_profile(chat_id, uid, call)
        return
    
    # Help
    if data == "help":
        render_help(chat_id, call)
        return
    
    # Admin
    if data == "admin":
        render_admin(chat_id, uid, call)
        return
    
    # Admin sub-menus
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
    if data == "adm_pending":
        render_adm_pending(chat_id, call)
        return
    if data == "adm_settings":
        render_adm_settings(chat_id, call)
        return
    
    # Backup now
    if data == "adm_backup_now":
        if not is_admin(uid):
            render_menu(chat_id, "❌ Admin only.", back_kb("admin"), call)
            return
        render_menu(chat_id, "⏳ Backing up...", back_kb("admin"), call)
        threading.Thread(target=lambda: do_backup(chat_id), daemon=True).start()
        return
    
    # Bot view
    if data.startswith("view_"):
        bid = data[5:]
        render_bot_view(chat_id, bid, call)
        return
    
    # Start bot
    if data.startswith("start_"):
        bid = data[6:]
        b = find_bot(bid)
        if b and (b.get("owner") == uid or is_admin(uid)):
            res = start_child(b)
            if res.get("ok"):
                render_menu(chat_id, f"✅ Bot started. PID: {res.get('pid')}", back_kb(f"view_{bid}"), call)
            else:
                render_menu(chat_id, f"❌ {res.get('error')}", back_kb(f"view_{bid}"), call)
        else:
            render_menu(chat_id, "❌ Not yours.", back_kb("my_bots"), call)
        return
    
    # Stop bot
    if data.startswith("stop_"):
        bid = data[5:]
        b = find_bot(bid)
        if b and (b.get("owner") == uid or is_admin(uid)):
            stop_child(bid)
            render_menu(chat_id, "✅ Bot stopped.", back_kb(f"view_{bid}"), call)
        else:
            render_menu(chat_id, "❌ Not yours.", back_kb("my_bots"), call)
        return
    
    # Restart bot
    if data.startswith("restart_"):
        bid = data[8:]
        b = find_bot(bid)
        if b and (b.get("owner") == uid or is_admin(uid)):
            res = restart_child(b)
            if res.get("ok"):
                render_menu(chat_id, "✅ Bot restarted.", back_kb(f"view_{bid}"), call)
            else:
                render_menu(chat_id, f"❌ {res.get('error')}", back_kb(f"view_{bid}"), call)
        else:
            render_menu(chat_id, "❌ Not yours.", back_kb("my_bots"), call)
        return
    
    # Logs
    if data.startswith("logs_"):
        bid = data[5:]
        b = find_bot(bid)
        if b and (b.get("owner") == uid or is_admin(uid)):
            st = child_status(bid)
            logs = "\n".join(st["logs"][-30:]) or "(no logs)"
            render_menu(chat_id, f"📋 <b>Logs</b>\n━━━━━━━━━━━━━━━━━━\n<pre>{logs[:3000]}</pre>", 
                       back_kb(f"view_{bid}"), call)
        else:
            render_menu(chat_id, "❌ Not yours.", back_kb("my_bots"), call)
        return
    
    # Delete bot
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
            render_menu(chat_id, "🗑️ Bot deleted.", back_kb("my_bots"), call)
        else:
            render_menu(chat_id, "❌ Not yours.", back_kb("my_bots"), call)
        return
    
    # Unknown
    render_menu(chat_id, "❓ Unknown option.", back_kb("menu_main"), call)

def do_backup(chat_id: int):
    res = gh_backup_now()
    if res.get("ok"):
        bot.send_message(chat_id, f"✅ Backup done! Size: {res.get('size')} bytes")
    else:
        bot.send_message(chat_id, f"❌ Backup failed: {res.get('error')}")

# ================================================================
#  ═══════════════════════════════════════════════════════════════
#  MAIN
#  ═══════════════════════════════════════════════════════════════
# ================================================================

START_TIME = time.time()

def banner():
    print("=" * 60)
    print(f"   🤖 Simran Hosting Bot v3.0")
    print(f"   👤 Owner ID: {OWNER_ID}")
    print(f"   📦 GitHub Backup: {'ON' if GITHUB_ENABLED else 'OFF'}")
    print(f"   🔒 Rate Limit: {RATE_LIMIT['messages']}/min")
    print(f"   🔄 Auto-Restart: ON")
    print(f"   🚀 Running on port: {PORT}")
    print("=" * 60)

def main():
    banner()
    
    # Start GitHub auto backup
    if GITHUB_ENABLED:
        threading.Thread(target=gh_auto_loop, daemon=True).start()
    
    # Start rate cleanup
    threading.Thread(target=cleanup_rates, daemon=True).start()
    
    # Start keepalive
    start_keepalive()
    
    # Set commands
    try:
        bot.set_my_commands([
            types.BotCommand("start", "Main menu"),
            types.BotCommand("menu", "Menu"),
            types.BotCommand("help", "Help"),
            types.BotCommand("id", "Your ID"),
            types.BotCommand("cancel", "Cancel"),
            types.BotCommand("admin", "Admin panel"),
        ])
    except:
        pass
    
    # Notify owner
    notify_owner("✅ Bot started! Ready for 20,000+ users.")
    
    print("[bot] polling...")
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=25)
        except Exception as e:
            print(f"[bot] error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
