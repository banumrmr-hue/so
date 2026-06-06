import os
import sys
import io
import ast
import re
import logging
import asyncio
import sqlite3
import marshal
import zlib
import base64
import random
import hashlib
import tempfile
import threading
from datetime import date, datetime
from contextlib import contextmanager
from functools import wraps
from http.server import BaseHTTPRequestHandler, HTTPServer

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from python_minifier import minify
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply, Message
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

# ══════════════════ CONFIG ══════════════════
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "Somanigod")
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
FORCE_JOIN_CHANNEL = os.environ.get("FORCE_JOIN_CHANNEL", "")
FREE_DAILY_LIMIT = int(os.environ.get("FREE_DAILY_LIMIT", "5"))
DB_PATH = os.environ.get("DB_PATH", "somani_encoder.db")
BRANDING = "⚡ SOMANI ENCODER BOT⚡"
BRANDING_LINE = "══════════════════════════════"

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ══════════════════ KEEP ALIVE ══════════════════
class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"SOMANI ENCODER BOTis alive!")
    def log_message(self, format, *args): pass

def keep_alive():
    if not os.environ.get("RENDER"):
        return
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"[keep_alive] Health check server on port {port}")

# ══════════════════ DATABASE ══════════════════
@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                join_date   TEXT NOT NULL,
                is_premium  INTEGER NOT NULL DEFAULT 0,
                is_banned   INTEGER NOT NULL DEFAULT 0,
                files_today INTEGER NOT NULL DEFAULT 0,
                last_reset  TEXT NOT NULL,
                files_total INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS stats (
                id              INTEGER PRIMARY KEY CHECK (id = 1),
                total_files     INTEGER NOT NULL DEFAULT 0,
                premium_users   INTEGER NOT NULL DEFAULT 0,
                today_activity  INTEGER NOT NULL DEFAULT 0,
                last_reset      TEXT NOT NULL
            );
            INSERT OR IGNORE INTO stats (id, total_files, premium_users, today_activity, last_reset)
            VALUES (1, 0, 0, 0, date('now'));
        """)
    logger.info("Database initialised.")

def _reset_daily_if_needed(conn, row):
    today = date.today().isoformat()
    if row["last_reset"] != today:
        conn.execute("UPDATE users SET files_today=0, last_reset=? WHERE user_id=?", (today, row["user_id"]))
        return dict(row) | {"files_today": 0, "last_reset": today}
    return dict(row)

def get_or_create_user(user_id, username, first_name):
    today = date.today().isoformat()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users (user_id, username, first_name, join_date, last_reset) VALUES (?,?,?,?,?)",
                (user_id, username, first_name, today, today),
            )
            row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        else:
            conn.execute("UPDATE users SET username=?, first_name=? WHERE user_id=?", (username, first_name, user_id))
        return _reset_daily_if_needed(conn, row)

def get_user(user_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            return None
        return _reset_daily_if_needed(conn, row)

def increment_user_files(user_id):
    with get_conn() as conn:
        conn.execute("UPDATE users SET files_today=files_today+1, files_total=files_total+1 WHERE user_id=?", (user_id,))
        conn.execute("UPDATE stats SET total_files=total_files+1, today_activity=today_activity+1 WHERE id=1")

def set_premium(user_id, value):
    with get_conn() as conn:
        conn.execute("UPDATE users SET is_premium=? WHERE user_id=?", (int(value), user_id))
        delta = 1 if value else -1
        conn.execute("UPDATE stats SET premium_users=MAX(0,premium_users+?) WHERE id=1", (delta,))

def set_banned(user_id, value):
    with get_conn() as conn:
        conn.execute("UPDATE users SET is_banned=? WHERE user_id=?", (int(value), user_id))

def get_stats():
    today = date.today().isoformat()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM stats WHERE id=1").fetchone()
        if row["last_reset"] != today:
            conn.execute("UPDATE stats SET today_activity=0, last_reset=? WHERE id=1", (today,))
            row = conn.execute("SELECT * FROM stats WHERE id=1").fetchone()
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        premium_users = conn.execute("SELECT COUNT(*) FROM users WHERE is_premium=1").fetchone()[0]
        return {
            "total_users": total_users,
            "total_files": row["total_files"],
            "premium_users": premium_users,
            "today_activity": row["today_activity"],
        }

def get_all_user_ids():
    with get_conn() as conn:
        rows = conn.execute("SELECT user_id FROM users WHERE is_banned=0").fetchall()
        return [r["user_id"] for r in rows]

# ══════════════════ HELPERS ══════════════════
async def check_membership(bot, user_id, channel):
    if not channel:
        return True
    try:
        member = await bot.get_chat_member(channel, user_id)
        return member.status not in ("left", "kicked", "banned")
    except Exception:
        return True

def require_not_banned(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        db_user = get_or_create_user(user.id, user.username, user.first_name)
        if db_user["is_banned"]:
            await update.effective_message.reply_text(f"{BRANDING}\n\n🚫 You have been banned from using this bot.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

def require_membership(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if FORCE_JOIN_CHANNEL:
            user_id = update.effective_user.id
            is_member = await check_membership(context.bot, user_id, FORCE_JOIN_CHANNEL)
            if not is_member:
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{FORCE_JOIN_CHANNEL.lstrip('@')}"),
                    InlineKeyboardButton("✅ I Joined", callback_data="check_membership"),
                ]])
                await update.effective_message.reply_text(
                    f"{BRANDING}\n\n⚠️ You must join our channel to use this bot.\n\n📢 Channel: {FORCE_JOIN_CHANNEL}",
                    reply_markup=kb,
                )
                return
        return await func(update, context, *args, **kwargs)
    return wrapper

def require_admin(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if update.effective_user.id not in ADMIN_IDS:
            await update.effective_message.reply_text("⛔ Admin only.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

def can_process(user_id):
    db_user = get_user(user_id)
    if not db_user:
        return True, ""
    if db_user["is_premium"]:
        return True, ""
    if db_user["files_today"] >= FREE_DAILY_LIMIT:
        return False, (f"⚠️ Daily limit reached ({FREE_DAILY_LIMIT} files/day on Free plan).\nUpgrade to 💎 Premium for unlimited usage.")
    return True, ""

def plan_badge(is_premium):
    return "💎 Premium" if is_premium else "🆓 Free"

# ══════════════════ PROGRESS ══════════════════
STEPS = {
    "obfuxtreme": ["⚡ Reading File...","⚡ Collecting Variables...","⚡ Applying AES String Encryption...","⚡ Flattening Control Flow...","⚡ Marshalling & Compressing...","⚡ Generating Loader...","✅ Obfuscation Complete!"],
    "logic_changer": ["⚡ Reading File...","⚡ Parsing AST...","⚡ Generating State Machines...","⚡ Flattening Control Flow...","⚡ Restructuring Functions...","✅ Logic Changed!"],
    "expiry": ["⚡ Reading File...","⚡ Validating Expiry Date...","⚡ Injecting Expiry Protection...","⚡ Adding Debugger Detection...","⚡ Adding Time Tamper Guard...","✅ Expiry Injected!"],
    "shortpy": ["⚡ Reading File...","⚡ Removing Comments...","⚡ Renaming Variables...","⚡ Combining Imports...","⚡ Minifying Code...","✅ Minification Complete!"],
    "combo": ["⚡ Reading File...","⚡ Stage 1: Short Py — Minifying...","⚡ Stage 2: Logic Changer — Flattening...","⚡ Stage 3: Expiry Inject — Protecting...","⚡ Stage 4: ObfuXtreme — Encrypting...","⚡ Finalizing Premium Protection...","✅ Premium Combo Complete!"],
}

async def animate_progress(msg: Message, module: str, delay: float = 0.6):
    steps = STEPS.get(module, ["⚡ Processing...", "✅ Done!"])
    for step in steps[:-1]:
        try:
            await msg.edit_text(f"<b>⚡ SOMANI ENCODER BOT⚡</b>\n\n{step}", parse_mode="HTML")
        except Exception:
            pass
        await asyncio.sleep(delay)
    return steps[-1]

# ══════════════════ MODULE: SHORTPY ══════════════════
def run_shortpy(source_code):
    return minify(source_code, remove_literal_statements=True, remove_annotations=True,
                  remove_pass=True, remove_asserts=True, remove_debug=True,
                  remove_explicit_return_none=True, remove_object_base=True,
                  combine_imports=True, hoist_literals=False, rename_globals=True,
                  rename_locals=True, preserve_shebang=True)

# ══════════════════ MODULE: LOGIC CHANGER ══════════════════
try:
    from ast import unparse as _unparse
except ImportError:
    _unparse = None

class FlattenTransformer(ast.NodeTransformer):
    def __init__(self):
        super().__init__()
        self._counter = 0

    def _new_state(self):
        self._counter += 1
        return self._counter

    def visit_FunctionDef(self, node):
        node.body = self._flatten_body(node.body)
        self.generic_visit(node)
        return node

    def visit_AsyncFunctionDef(self, node):
        node.body = self._flatten_body(node.body)
        self.generic_visit(node)
        return node

    def _flatten_body(self, body):
        if not body or (len(body) == 1 and isinstance(body[0], ast.Pass)):
            return body
        blocks = self._build_blocks(body)
        state_var = ast.Name('__cf_state__', ast.Store())
        init_state = ast.Assign(targets=[state_var], value=ast.Constant(0))
        while_body = self._create_dispatcher(blocks, '__cf_state__')
        while_loop = ast.While(test=ast.Constant(True), body=while_body, orelse=[])
        return [init_state, while_loop]

    def _build_blocks(self, stmts):
        blocks, current = [], []
        for stmt in stmts:
            if isinstance(stmt, (ast.If, ast.While, ast.For, ast.Try, ast.Break, ast.Continue, ast.Return, ast.Raise)):
                if current:
                    blocks.append(current)
                    current = []
                transformed = self._transform_branch(stmt)
                blocks.append(transformed if isinstance(transformed, list) else [transformed])
            else:
                current.append(stmt)
        if current:
            blocks.append(current)
        return blocks

    def _transform_branch(self, stmt):
        if isinstance(stmt, ast.If):
            return self._flatten_if(stmt)
        elif isinstance(stmt, ast.While):
            return self._flatten_while(stmt)
        elif isinstance(stmt, ast.For):
            return self._flatten_for(stmt)
        return stmt

    def _flatten_if(self, node):
        then_state = self._new_state()
        else_state = self._new_state()
        return ast.Assign(
            targets=[ast.Name('__cf_state__', ast.Store())],
            value=ast.IfExp(test=node.test, body=ast.Constant(then_state), orelse=ast.Constant(else_state))
        )

    def _flatten_while(self, node):
        loop_state = self._new_state()
        exit_state = self._new_state()
        return ast.Assign(
            targets=[ast.Name('__cf_state__', ast.Store())],
            value=ast.IfExp(test=node.test, body=ast.Constant(loop_state), orelse=ast.Constant(exit_state))
        )

    def _flatten_for(self, node):
        iter_assign = ast.Assign(
            targets=[ast.Name('__cf_iter__', ast.Store())],
            value=ast.Call(func=ast.Name('iter', ast.Load()), args=[node.iter], keywords=[])
        )
        try_body = [ast.Assign(targets=[node.target], value=ast.Call(func=ast.Name('next', ast.Load()), args=[ast.Name('__cf_iter__', ast.Load())], keywords=[]))]
        except_handler = ast.ExceptHandler(type=ast.Name('StopIteration', ast.Load()), name=None, body=[ast.Break()])
        try_stmt = ast.Try(body=try_body, handlers=[except_handler], orelse=[], finalbody=[])
        while_body = [try_stmt] + node.body
        while_node = ast.While(test=ast.Constant(True), body=while_body, orelse=[])
        return [iter_assign, self._transform_branch(while_node)]

    def _create_dispatcher(self, blocks, state_var_name):
        state_var = ast.Name(state_var_name, ast.Load())
        dispatcher = [ast.If(
            test=ast.Compare(left=state_var, ops=[ast.Eq()], comparators=[ast.Constant(0)]),
            body=[ast.Assign(targets=[ast.Name(state_var_name, ast.Store())], value=ast.Constant(1))],
            orelse=[]
        )]
        for idx, block in enumerate(blocks, start=1):
            block_stmts = list(block)
            if not any(isinstance(s, (ast.Return, ast.Break, ast.Continue, ast.Raise)) for s in block_stmts):
                next_state = idx + 1 if idx < len(blocks) else len(blocks) + 1
                block_stmts.append(ast.Assign(targets=[ast.Name(state_var_name, ast.Store())], value=ast.Constant(next_state)))
            dispatcher.append(ast.If(test=ast.Compare(left=state_var, ops=[ast.Eq()], comparators=[ast.Constant(idx)]), body=block_stmts, orelse=[]))
        final_state = len(blocks) + 1
        dispatcher.append(ast.If(test=ast.Compare(left=state_var, ops=[ast.Eq()], comparators=[ast.Constant(final_state)]), body=[ast.Break()], orelse=[]))
        return dispatcher

def run_logic_changer(source_code):
    tree = ast.parse(source_code)
    transformer = FlattenTransformer()
    transformed = transformer.visit(tree)
    ast.fix_missing_locations(transformed)
    if _unparse:
        return _unparse(transformed)
    try:
        import astor
        return astor.to_source(transformed)
    except ImportError:
        raise RuntimeError("ast.unparse not available and astor not installed.")

# ══════════════════ MODULE: EXPIRY INJECTOR ══════════════════
EXPIRY_TEMPLATE = '''
import sys as _sys, os as _os, signal as _signal, threading as _threading
import hashlib as _hashlib, tempfile as _tempfile
from datetime import datetime as _datetime

_EXPIRY_DATE = "{expiry_date}"
_original_datetime_now = _datetime.now
_original_os_kill = _os.kill
_original_os_exit = _os._exit

def _check_debugger():
    if _sys.gettrace() is not None or _sys.getprofile() is not None:
        print("Contact : @somani_07x")
        return True
    return False

def _get_self_code_hash():
    try:
        with open(__file__, 'rb') as _f:
            return _hashlib.sha256(_f.read()).hexdigest()
    except Exception:
        return None

_SELF_HASH = _get_self_code_hash()

def _check_code_integrity():
    if _SELF_HASH is not None:
        if _get_self_code_hash() != _SELF_HASH:
            print("Contact : @somani_07x")
            return False
    return True

def _get_secure_time():
    try:
        import urllib.request as _ur, json as _json
        with _ur.urlopen('http://worldtimeapi.org/api/timezone/Etc/UTC', timeout=2) as _f:
            _data = _f.read().decode()
            _dt_str = _json.loads(_data)['datetime']
            return _datetime.strptime(_dt_str[:19], '%Y-%m-%dT%H:%M:%S').date()
    except Exception:
        return _original_datetime_now().date()

def _check_time_tamper(expiry_date_str):
    expiry_date = _datetime.strptime(expiry_date_str, '%Y-%m-%d').date()
    current_date = _get_secure_time()
    token_file = _os.path.join(_tempfile.gettempdir(), '.alex_expiry_token')
    try:
        with open(token_file, 'r') as _f:
            last_date = _datetime.strptime(_f.read().strip(), '%Y-%m-%d').date()
            if last_date > current_date:
                print('Contact : @somani_07x')
                return True
    except Exception:
        pass
    with open(token_file, 'w') as _f:
        _f.write(current_date.strftime('%Y-%m-%d'))
    return current_date > expiry_date

_terminate_flag = False

def _monitor_loop(expiry_date_str):
    global _terminate_flag
    while not _terminate_flag:
        try:
            if _check_debugger() or not _check_code_integrity() or _check_time_tamper(expiry_date_str):
                print("Contact : @somani_07x")
                _original_os_kill(_os.getpid(), _signal.SIGKILL)
                _original_os_exit(1)
        except Exception:
            pass
        _threading.Event().wait(0.5)

def _check_expiry(expiry_date_str=_EXPIRY_DATE):
    try:
        _t = _threading.Thread(target=_monitor_loop, args=(expiry_date_str,), daemon=True)
        _t.start()
        if _check_debugger():
            raise Exception("Contact : @somani_07x")
        if not _check_code_integrity():
            raise Exception("Contact : @somani_07x")
        if _check_time_tamper(expiry_date_str):
            print('Contact : @somani_07x')
            _original_os_kill(_os.getpid(), _signal.SIGKILL)
            _original_os_exit(1)
    except Exception as _e:
        print(f"Error: {{_e}}")
        _original_os_kill(_os.getpid(), _signal.SIGKILL)
        _original_os_exit(1)

_check_expiry()
'''

def run_expiry_injector(source_code, expiry_date):
    return EXPIRY_TEMPLATE.format(expiry_date=expiry_date) + "\n" + source_code

# ══════════════════ MODULE: OBFUXTREME ══════════════════
class UltimateObfuscator:
    def __init__(self, source_code):
        self.code = source_code
        self._key_fragments = [b'\xe1H-\x03', b't\xfa\r]', b'SD\x9a\x95', b'\xca\xc1\t\x86', b'\x18\xca]\x7f', b'\xaeA\xf6\xbe', b'\xf7A\xc3\xc7', b'\xa6$\xae>']
        self._iv_bytes = b'\x10p\x8a\xefRJ\xadd\xd3\x97%\xab\xcaE\xfb\\'

    def _derive_key(self):
        return b''.join(self._key_fragments)

    class VariableCollector(ast.NodeVisitor):
        def __init__(self):
            self.assigned_vars = set()
        def visit_Name(self, node):
            if isinstance(node.ctx, ast.Store):
                self.assigned_vars.add(node.id)
            self.generic_visit(node)
        def visit_arg(self, node):
            self.assigned_vars.add(node.arg)
            self.generic_visit(node)

    class VariableRenamer(ast.NodeTransformer):
        def __init__(self, assigned_vars):
            self.var_map = {}
            self.assigned_vars = assigned_vars
        def _obf_name(self, original):
            return f"__v_{hashlib.shake_128(original.encode()).hexdigest(8)}"
        def visit_Name(self, node):
            if node.id in self.assigned_vars:
                if node.id not in self.var_map:
                    self.var_map[node.id] = self._obf_name(node.id)
                node.id = self.var_map[node.id]
            return node
        def visit_arg(self, node):
            if node.arg in self.assigned_vars:
                if node.arg not in self.var_map:
                    self.var_map[node.arg] = self._obf_name(node.arg)
                node.arg = self.var_map[node.arg]
            return node

    class ControlFlowFlattener(ast.NodeTransformer):
        def visit_FunctionDef(self, node):
            self.generic_visit(node)
            state_var = f"__state_{random.randint(1000, 9999)}"
            new_body = [ast.Assign(targets=[ast.Name(id=state_var, ctx=ast.Store())], value=ast.Constant(value=0))]
            while_body = []
            for i, stmt in enumerate(node.body):
                while_body.append(ast.If(
                    test=ast.Compare(left=ast.Name(id=state_var, ctx=ast.Load()), ops=[ast.Eq()], comparators=[ast.Constant(value=i)]),
                    body=[stmt, ast.AugAssign(target=ast.Name(id=state_var, ctx=ast.Store()), op=ast.Add(), value=ast.Constant(value=1))],
                    orelse=[]
                ))
            new_body.append(ast.While(
                test=ast.Compare(left=ast.Name(id=state_var, ctx=ast.Load()), ops=[ast.Lt()], comparators=[ast.Constant(value=len(node.body))]),
                body=while_body, orelse=[]
            ))
            node.body = new_body
            return node

    class StringEncryptor(ast.NodeTransformer):
        def __init__(self, obfuscator):
            self.obfuscator = obfuscator
            self.in_fstring = False
        def visit_JoinedStr(self, node):
            self.in_fstring = True
            self.generic_visit(node)
            self.in_fstring = False
            return node
        def visit_Constant(self, node):
            if isinstance(node.value, str) and not self.in_fstring:
                cipher = AES.new(self.obfuscator._derive_key(), AES.MODE_CBC, self.obfuscator._iv_bytes)
                encrypted = cipher.encrypt(pad(node.value.encode(), 16))
                return ast.Call(func=ast.Name(id='__decode_x', ctx=ast.Load()), args=[ast.Constant(value=encrypted)], keywords=[])
            return node

    def _transform_ast(self):
        tree = ast.parse(self.code)
        collector = self.VariableCollector()
        collector.visit(tree)
        assigned_vars = collector.assigned_vars
        for transformer in [self.VariableRenamer(assigned_vars), self.ControlFlowFlattener(), self.StringEncryptor(self)]:
            tree = transformer.visit(tree)
            ast.fix_missing_locations(tree)
        return marshal.dumps(compile(tree, "<obfuscated>", "exec"))

    def _split_payload(self, b85, size=150):
        return [b85[i:i+size] for i in range(0, len(b85), size)]

    def _build_loader(self, chunks):
        chunk_defs = "\n".join([f"__chunk{i} = \"{chunk}\"" for i, chunk in enumerate(chunks)])
        joined = ", ".join([f"__chunk{i}" for i in range(len(chunks))])
        return f'''
import sys as __s, os as __o, base64 as __b, marshal as __m, zlib as __z, traceback as __t
from Crypto.Cipher import AES as __A
from Crypto.Util.Padding import unpad as __u

def __anti_dbg():
    if __s.gettrace() or (__o.name == 'nt' and __import__('ctypes').windll.kernel32.IsDebuggerPresent()):
        __s.exit(0)
__anti_dbg()

__iv = {repr(self._iv_bytes)}
__kf = [b'\\xe1H-\\x03', b't\\xfa\\r]', b'SD\\x9a\\x95', b'\\xca\\xc1\\t\\x86', b'\\x18\\xca]\\x7f', b'\\xaeA\\xf6\\xbe', b'\\xf7A\\xc3\\xc7', b'\\xa6$\\xae>']
__key = b''.join(__kf)

def __decode_x(d):
    try:
        return __u(__A.new(__key, __A.MODE_CBC, __iv).decrypt(d), 16).decode()
    except: return ""

{chunk_defs}
__BLOB = "".join([{joined}])

def __boot():
    try:
        raw = __b.b85decode(__BLOB.encode())
        decrypted = __u(__A.new(__key, __A.MODE_CBC, __iv).decrypt(raw), 16)
        payload = __z.decompress(decrypted)
        exec(__m.loads(payload), {{'__name__': '__main__', '__builtins__': __builtins__, '__decode_x': __decode_x}})
    except Exception:
        __t.print_exc()
        __s.exit(1)

__boot()
'''

    def obfuscate(self):
        transformed = self._transform_ast()
        compressed = zlib.compress(transformed, level=9)
        cipher = AES.new(self._derive_key(), AES.MODE_CBC, self._iv_bytes)
        encrypted = cipher.encrypt(pad(compressed, 16))
        b85 = base64.b85encode(encrypted).decode()
        return self._build_loader(self._split_payload(b85))

def run_obfuxtreme(source_code):
    return UltimateObfuscator(source_code).obfuscate()

# ══════════════════ UI COMPONENTS ══════════════════
BACK_KB = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Menu", callback_data="nav:menu")]])

MAIN_MENU_TEXT = (
    f"<b>{BRANDING}</b>\n<code>{BRANDING_LINE}</code>\n\n"
    "Welcome! Upload a <b>.py</b> file and choose your protection module.\n\n"
    "<b>Available Modules:</b>\n"
    "⚡ <b>ObfuXtreme</b> — Powerful code obfuscation\n"
    "🧠 <b>Logic Changer</b> — Control flow flattening\n"
    "🔐 <b>Expiry Inject</b> — Inject expiry protection\n"
    "✂️ <b>Short Py</b> — Minify your code\n"
    "💎 <b>Premium Combo</b> — Full protection pipeline\n\n"
    f"<code>{BRANDING_LINE}</code>"
)

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ ObfuXtreme", callback_data="mod:obfuxtreme"), InlineKeyboardButton("🧠 Logic Changer", callback_data="mod:logic_changer")],
        [InlineKeyboardButton("🔐 Expiry Inject", callback_data="mod:expiry"), InlineKeyboardButton("✂️ Short Py", callback_data="mod:shortpy")],
        [InlineKeyboardButton("💎 Premium Combo", callback_data="mod:combo")],
        [InlineKeyboardButton("👤 Profile", callback_data="nav:profile"), InlineKeyboardButton("📊 Statistics", callback_data="nav:stats"), InlineKeyboardButton("ℹ️ Help", callback_data="nav:help")],
    ])

# ══════════════════ HANDLERS: START ══════════════════
@require_not_banned
@require_membership
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_or_create_user(user.id, user.username, user.first_name)
    context.user_data.clear()
    await update.effective_message.reply_text(MAIN_MENU_TEXT, parse_mode="HTML", reply_markup=main_menu_keyboard())

async def membership_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    if FORCE_JOIN_CHANNEL:
        is_member = await check_membership(context.bot, user.id, FORCE_JOIN_CHANNEL)
        if is_member:
            get_or_create_user(user.id, user.username, user.first_name)
            await query.message.edit_text(MAIN_MENU_TEXT, parse_mode="HTML", reply_markup=main_menu_keyboard())
        else:
            await query.answer("❌ You haven't joined yet!", show_alert=True)
    else:
        await query.message.edit_text(MAIN_MENU_TEXT, parse_mode="HTML", reply_markup=main_menu_keyboard())

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.message.edit_text(MAIN_MENU_TEXT, parse_mode="HTML", reply_markup=main_menu_keyboard())

# ══════════════════ HANDLERS: PROFILE / STATS / HELP ══════════════════
@require_not_banned
async def profile_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_or_create_user(user.id, user.username, user.first_name)
    text = (
        f"<b>{BRANDING}</b>\n<code>{BRANDING_LINE}</code>\n\n"
        "👤 <b>Your Profile</b>\n\n"
        f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
        f"👤 <b>Name:</b> {user.first_name or 'N/A'}\n"
        f"📅 <b>Join Date:</b> {db_user['join_date']}\n"
        f"📦 <b>Plan:</b> {plan_badge(bool(db_user['is_premium']))}\n"
        f"📁 <b>Files Today:</b> {db_user['files_today']}\n"
        f"📊 <b>Total Files:</b> {db_user['files_total']}\n\n"
        f"<code>{BRANDING_LINE}</code>"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.edit_text(text, parse_mode="HTML", reply_markup=BACK_KB)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=BACK_KB)

async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_stats()
    text = (
        f"<b>{BRANDING}</b>\n<code>{BRANDING_LINE}</code>\n\n"
        "📊 <b>Bot Statistics</b>\n\n"
        f"👥 <b>Total Users:</b> {stats['total_users']:,}\n"
        f"📁 <b>Total Files Processed:</b> {stats['total_files']:,}\n"
        f"💎 <b>Premium Users:</b> {stats['premium_users']:,}\n"
        f"📅 <b>Today's Activity:</b> {stats['today_activity']:,}\n\n"
        f"<code>{BRANDING_LINE}</code>"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.edit_text(text, parse_mode="HTML", reply_markup=BACK_KB)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=BACK_KB)

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"<b>{BRANDING}</b>\n<code>{BRANDING_LINE}</code>\n\n"
        "ℹ️ <b>Help — Module Guide</b>\n\n"
        "⚡ <b>ObfuXtreme</b> — Multi-layer obfuscation with AES encryption\n"
        "🧠 <b>Logic Changer</b> — Control flow flattening & state machines\n"
        "🔐 <b>Expiry Inject</b> — Time-based expiry + anti-debug protection\n"
        "✂️ <b>Short Py</b> — Minify & compress your Python code\n"
        "💎 <b>Premium Combo</b> — Full pipeline (Premium only)\n\n"
        "📌 <b>How to use:</b>\n"
        "1. Choose a module from the menu\n"
        "2. Upload your <code>.py</code> file\n"
        "3. Receive the protected file instantly\n\n"
        f"<code>{BRANDING_LINE}</code>"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.edit_text(text, parse_mode="HTML", reply_markup=BACK_KB)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=BACK_KB)

# ══════════════════ HANDLERS: ADMIN ══════════════════
@require_admin
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_stats()
    text = (
        f"<b>{BRANDING}</b>\n<code>{BRANDING_LINE}</code>\n\n"
        "🛠 <b>Admin Panel</b>\n\n"
        f"👥 Total Users: <b>{stats['total_users']:,}</b>\n"
        f"📁 Total Files: <b>{stats['total_files']:,}</b>\n"
        f"💎 Premium Users: <b>{stats['premium_users']:,}</b>\n"
        f"📅 Today: <b>{stats['today_activity']:,}</b>\n\n"
        "<b>Commands:</b>\n/adduser &lt;id&gt;\n/addpremium &lt;id&gt;\n"
        "/removepremium &lt;id&gt;\n/ban &lt;id&gt;\n/unban &lt;id&gt;\n"
        "/broadcast &lt;msg&gt;\n/stats\n\n"
        f"<code>{BRANDING_LINE}</code>"
    )
    await update.message.reply_text(text, parse_mode="HTML")

@require_admin
async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(f"<b>{BRANDING}</b>\n\nUsage: /adduser &lt;user_id&gt;", parse_mode="HTML")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    existing = get_user(uid)
    if existing:
        await update.message.reply_text(
            f"<b>{BRANDING}</b>\n<code>{BRANDING_LINE}</code>\n\nℹ️ User already exists:\n\n"
            f"🆔 <code>{uid}</code>\n📦 {plan_badge(bool(existing['is_premium']))}\n"
            f"📁 Files Total: {existing['files_total']}\n\n<code>{BRANDING_LINE}</code>", parse_mode="HTML")
        return
    db_user = get_or_create_user(uid, username=None, first_name="Added by Admin")
    await update.message.reply_text(
        f"<b>{BRANDING}</b>\n<code>{BRANDING_LINE}</code>\n\n✅ <b>User added!</b>\n\n"
        f"🆔 <code>{uid}</code>\n📅 {db_user.get('join_date')}\n📦 🆓 Free\n\n<code>{BRANDING_LINE}</code>", parse_mode="HTML")
    try:
        await context.bot.send_message(uid, f"<b>{BRANDING}</b>\n\n👋 You have been added to the bot by an admin.\nUse /start to begin.", parse_mode="HTML")
    except Exception:
        pass

@require_admin
async def add_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /addpremium <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    set_premium(uid, True)
    await update.message.reply_text(f"<b>{BRANDING}</b>\n\n✅ User <code>{uid}</code> granted 💎 Premium.", parse_mode="HTML")
    try:
        await context.bot.send_message(uid, f"<b>{BRANDING}</b>\n\n🎉 You have been upgraded to <b>💎 Premium</b>!\n\nEnjoy unlimited usage and the full Premium Combo.", parse_mode="HTML")
    except Exception:
        pass

@require_admin
async def remove_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /removepremium <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    set_premium(uid, False)
    await update.message.reply_text(f"<b>{BRANDING}</b>\n\n✅ Premium removed from <code>{uid}</code>.", parse_mode="HTML")

@require_admin
async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /ban <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    if uid in ADMIN_IDS:
        await update.message.reply_text("⛔ Cannot ban an admin.")
        return
    set_banned(uid, True)
    await update.message.reply_text(f"<b>{BRANDING}</b>\n\n🚫 User <code>{uid}</code> has been banned.", parse_mode="HTML")

@require_admin
async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    set_banned(uid, False)
    await update.message.reply_text(f"<b>{BRANDING}</b>\n\n✅ User <code>{uid}</code> has been unbanned.", parse_mode="HTML")

@require_admin
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    msg_text = " ".join(context.args)
    broadcast_text = (f"<b>{BRANDING}</b>\n<code>{BRANDING_LINE}</code>\n\n📢 <b>Broadcast</b>\n\n{msg_text}\n\n<code>{BRANDING_LINE}</code>")
    user_ids = get_all_user_ids()
    status = await update.message.reply_text(f"⚡ Broadcasting to {len(user_ids)} users...")
    sent = failed = 0
    for uid in user_ids:
        try:
            await context.bot.send_message(uid, broadcast_text, parse_mode="HTML")
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await status.edit_text(f"<b>{BRANDING}</b>\n\n📢 Broadcast complete!\n✅ Sent: {sent}\n❌ Failed: {failed}", parse_mode="HTML")

@require_admin
async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_stats()
    text = (
        f"<b>{BRANDING}</b>\n<code>{BRANDING_LINE}</code>\n\n"
        "📊 <b>Statistics</b>\n\n"
        f"👥 Total Users: <b>{stats['total_users']:,}</b>\n"
        f"📁 Total Files: <b>{stats['total_files']:,}</b>\n"
        f"💎 Premium Users: <b>{stats['premium_users']:,}</b>\n"
        f"📅 Today's Activity: <b>{stats['today_activity']:,}</b>\n\n"
        f"<code>{BRANDING_LINE}</code>"
    )
    await update.message.reply_text(text, parse_mode="HTML")

# ══════════════════ HANDLERS: MODULES ══════════════════
async def _module_select(update, context, module_key, select_text):
    query = update.callback_query
    await query.answer()
    context.user_data["module"] = module_key
    context.user_data["awaiting_file"] = True
    await query.message.edit_text(select_text, parse_mode="HTML", reply_markup=BACK_KB)

async def _send_result(update, context, result, out_name, caption):
    out_bytes = io.BytesIO(result.encode("utf-8"))
    out_bytes.name = out_name
    await update.message.reply_document(document=out_bytes, filename=out_name, caption=caption, parse_mode="HTML", reply_markup=BACK_KB)

@require_not_banned
@require_membership
async def obfuscate_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _module_select(update, context, "obfuxtreme",
        f"<b>{BRANDING}</b>\n<code>{BRANDING_LINE}</code>\n\n⚡ <b>ObfuXtreme</b>\n\nApplies powerful multi-layer obfuscation.\n\n📁 <b>Please upload your <code>.py</code> file now.</b>\n\n<code>{BRANDING_LINE}</code>")

@require_not_banned
async def obfuscate_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, doc = update.effective_user, update.message.document
    get_or_create_user(user.id, user.username, user.first_name)
    ok, reason = can_process(user.id)
    if not ok:
        await update.message.reply_text(f"<b>{BRANDING}</b>\n\n{reason}", parse_mode="HTML")
        return
    status_msg = await update.message.reply_text(f"<b>{BRANDING}</b>\n\n⚡ Reading File...", parse_mode="HTML")
    try:
        raw = await (await context.bot.get_file(doc.file_id)).download_as_bytearray()
        final_step = await animate_progress(status_msg, "obfuxtreme")
        result = run_obfuxtreme(raw.decode("utf-8"))
        await status_msg.edit_text(f"<b>{BRANDING}</b>\n\n{final_step}", parse_mode="HTML")
        out_name = f"Obfuscated_{doc.file_name}"
        increment_user_files(user.id)
        context.user_data.pop("awaiting_file", None)
        context.user_data.pop("module", None)
        await _send_result(update, context, result, out_name, f"<b>{BRANDING}</b>\n\n✅ <b>ObfuXtreme complete!</b>\n📁 <code>{out_name}</code>\n\n<code>{BRANDING_LINE}</code>")
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text(f"<b>{BRANDING}</b>\n\n❌ Error: <code>{e}</code>", parse_mode="HTML", reply_markup=BACK_KB)

@require_not_banned
@require_membership
async def logic_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _module_select(update, context, "logic_changer",
        f"<b>{BRANDING}</b>\n<code>{BRANDING_LINE}</code>\n\n🧠 <b>Logic Changer</b>\n\nTransforms your code's control flow.\n\n📁 <b>Please upload your <code>.py</code> file now.</b>\n\n<code>{BRANDING_LINE}</code>")

@require_not_banned
async def logic_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, doc = update.effective_user, update.message.document
    get_or_create_user(user.id, user.username, user.first_name)
    ok, reason = can_process(user.id)
    if not ok:
        await update.message.reply_text(f"<b>{BRANDING}</b>\n\n{reason}", parse_mode="HTML")
        return
    status_msg = await update.message.reply_text(f"<b>{BRANDING}</b>\n\n⚡ Reading File...", parse_mode="HTML")
    try:
        raw = await (await context.bot.get_file(doc.file_id)).download_as_bytearray()
        final_step = await animate_progress(status_msg, "logic_changer")
        result = run_logic_changer(raw.decode("utf-8"))
        await status_msg.edit_text(f"<b>{BRANDING}</b>\n\n{final_step}", parse_mode="HTML")
        out_name = f"LogicChanged_{doc.file_name}"
        increment_user_files(user.id)
        context.user_data.pop("awaiting_file", None)
        context.user_data.pop("module", None)
        await _send_result(update, context, result, out_name, f"<b>{BRANDING}</b>\n\n✅ <b>Logic Changer complete!</b>\n📁 <code>{out_name}</code>\n\n<code>{BRANDING_LINE}</code>")
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text(f"<b>{BRANDING}</b>\n\n❌ Error: <code>{e}</code>", parse_mode="HTML", reply_markup=BACK_KB)

@require_not_banned
@require_membership
async def expiry_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["module"] = "expiry"
    context.user_data["awaiting_file"] = True
    context.user_data.pop("expiry_source", None)
    context.user_data.pop("expiry_filename", None)
    await query.message.edit_text(
        f"<b>{BRANDING}</b>\n<code>{BRANDING_LINE}</code>\n\n🔐 <b>Expiry Inject</b>\n\nInjects time-based expiry protection.\n\n📁 <b>Please upload your <code>.py</code> file now.</b>\n\n<code>{BRANDING_LINE}</code>",
        parse_mode="HTML", reply_markup=BACK_KB)

@require_not_banned
async def expiry_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, doc = update.effective_user, update.message.document
    ok, reason = can_process(user.id)
    if not ok:
        await update.message.reply_text(f"<b>{BRANDING}</b>\n\n{reason}", parse_mode="HTML")
        return
    raw = await (await context.bot.get_file(doc.file_id)).download_as_bytearray()
    context.user_data["expiry_source"] = raw.decode("utf-8")
    context.user_data["expiry_filename"] = doc.file_name
    context.user_data["awaiting_date"] = True
    context.user_data.pop("awaiting_file", None)
    await update.message.reply_text(
        f"<b>{BRANDING}</b>\n<code>{BRANDING_LINE}</code>\n\n🔐 <b>Expiry Inject</b>\n\n📅 <b>Enter the expiry date:</b>\n\n<b>Format:</b> <code>YYYY-MM-DD</code>\n<b>Example:</b> <code>2026-12-31</code>\n\n<code>{BRANDING_LINE}</code>",
        parse_mode="HTML", reply_markup=ForceReply(selective=True))

@require_not_banned
async def expiry_date_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, date_str = update.effective_user, update.message.text.strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        await update.message.reply_text(f"<b>{BRANDING}</b>\n\n❌ Invalid format. Use <code>YYYY-MM-DD</code>", parse_mode="HTML")
        return
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        await update.message.reply_text(f"<b>{BRANDING}</b>\n\n❌ Invalid date.", parse_mode="HTML")
        return
    source = context.user_data.get("expiry_source")
    filename = context.user_data.get("expiry_filename", "output.py")
    if not source:
        await update.message.reply_text(f"<b>{BRANDING}</b>\n\n❌ No file found. Please restart.", parse_mode="HTML", reply_markup=BACK_KB)
        return
    status_msg = await update.message.reply_text(f"<b>{BRANDING}</b>\n\n⚡ Injecting...", parse_mode="HTML")
    try:
        final_step = await animate_progress(status_msg, "expiry")
        result = run_expiry_injector(source, date_str)
        await status_msg.edit_text(f"<b>{BRANDING}</b>\n\n{final_step}", parse_mode="HTML")
        out_name = f"ExpiryProtected_{filename}"
        increment_user_files(user.id)
        for k in ["expiry_source", "expiry_filename", "awaiting_date", "module"]:
            context.user_data.pop(k, None)
        await _send_result(update, context, result, out_name, f"<b>{BRANDING}</b>\n\n✅ <b>Expiry Inject complete!</b>\n📁 <code>{out_name}</code>\n📅 Expires: <code>{date_str}</code>\n\n<code>{BRANDING_LINE}</code>")
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text(f"<b>{BRANDING}</b>\n\n❌ Error: <code>{e}</code>", parse_mode="HTML", reply_markup=BACK_KB)

@require_not_banned
@require_membership
async def shortpy_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _module_select(update, context, "shortpy",
        f"<b>{BRANDING}</b>\n<code>{BRANDING_LINE}</code>\n\n✂️ <b>Short Py</b>\n\nMinifies and compresses your Python code.\n\n📁 <b>Please upload your <code>.py</code> file now.</b>\n\n<code>{BRANDING_LINE}</code>")

@require_not_banned
async def shortpy_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, doc = update.effective_user, update.message.document
    get_or_create_user(user.id, user.username, user.first_name)
    ok, reason = can_process(user.id)
    if not ok:
        await update.message.reply_text(f"<b>{BRANDING}</b>\n\n{reason}", parse_mode="HTML")
        return
    status_msg = await update.message.reply_text(f"<b>{BRANDING}</b>\n\n⚡ Reading File...", parse_mode="HTML")
    try:
        raw = await (await context.bot.get_file(doc.file_id)).download_as_bytearray()
        final_step = await animate_progress(status_msg, "shortpy")
        result = run_shortpy(raw.decode("utf-8"))
        await status_msg.edit_text(f"<b>{BRANDING}</b>\n\n{final_step}", parse_mode="HTML")
        out_name = f"ShortPy_{doc.file_name}"
        increment_user_files(user.id)
        context.user_data.pop("awaiting_file", None)
        context.user_data.pop("module", None)
        await _send_result(update, context, result, out_name, f"<b>{BRANDING}</b>\n\n✅ <b>Short Py complete!</b>\n📁 <code>{out_name}</code>\n\n<code>{BRANDING_LINE}</code>")
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text(f"<b>{BRANDING}</b>\n\n❌ Error: <code>{e}</code>", parse_mode="HTML", reply_markup=BACK_KB)

NOT_PREMIUM_TEXT = (
    f"<b>{BRANDING}</b>\n<code>{BRANDING_LINE}</code>\n\n💎 <b>Premium Combo</b>\n\n"
    "⛔ This module requires a <b>Premium</b> plan.\n\nContact an admin to upgrade.\n\n"
    f"<code>{BRANDING_LINE}</code>"
)

@require_not_banned
@require_membership
async def combo_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    db_user = get_or_create_user(user.id, user.username, user.first_name)
    if not db_user["is_premium"]:
        await query.message.edit_text(NOT_PREMIUM_TEXT, parse_mode="HTML", reply_markup=BACK_KB)
        return
    context.user_data["module"] = "combo"
    context.user_data["awaiting_file"] = True
    context.user_data.pop("combo_source", None)
    await query.message.edit_text(
        f"<b>{BRANDING}</b>\n<code>{BRANDING_LINE}</code>\n\n💎 <b>Premium Combo</b>\n\nFull pipeline: Short Py → Logic Changer → Expiry Inject → ObfuXtreme\n\n📁 <b>Please upload your <code>.py</code> file now.</b>\n\n<code>{BRANDING_LINE}</code>",
        parse_mode="HTML", reply_markup=BACK_KB)

@require_not_banned
async def combo_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, doc = update.effective_user, update.message.document
    db_user = get_or_create_user(user.id, user.username, user.first_name)
    if not db_user["is_premium"]:
        await update.message.reply_text(NOT_PREMIUM_TEXT, parse_mode="HTML", reply_markup=BACK_KB)
        return
    ok, reason = can_process(user.id)
    if not ok:
        await update.message.reply_text(f"<b>{BRANDING}</b>\n\n{reason}", parse_mode="HTML")
        return
    raw = await (await context.bot.get_file(doc.file_id)).download_as_bytearray()
    context.user_data["combo_source"] = raw.decode("utf-8")
    context.user_data["combo_filename"] = doc.file_name
    context.user_data["awaiting_date"] = True
    context.user_data.pop("awaiting_file", None)
    await update.message.reply_text(
        f"<b>{BRANDING}</b>\n<code>{BRANDING_LINE}</code>\n\n💎 <b>Premium Combo</b>\n\n📅 <b>Enter the expiry date:</b>\n\n<b>Format:</b> <code>YYYY-MM-DD</code>\n<b>Example:</b> <code>2026-12-31</code>\n\n<code>{BRANDING_LINE}</code>",
        parse_mode="HTML", reply_markup=ForceReply(selective=True))

@require_not_banned
async def combo_date_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, date_str = update.effective_user, update.message.text.strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        await update.message.reply_text(f"<b>{BRANDING}</b>\n\n❌ Invalid format. Use <code>YYYY-MM-DD</code>", parse_mode="HTML")
        return
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        await update.message.reply_text(f"<b>{BRANDING}</b>\n\n❌ Invalid date.", parse_mode="HTML")
        return
    source = context.user_data.get("combo_source")
    filename = context.user_data.get("combo_filename", "output.py")
    if not source:
        await update.message.reply_text(f"<b>{BRANDING}</b>\n\n❌ No file found. Please restart.", parse_mode="HTML", reply_markup=BACK_KB)
        return
    status_msg = await update.message.reply_text(f"<b>{BRANDING}</b>\n\n⚡ Starting Premium Combo Pipeline...", parse_mode="HTML")
    try:
        final_step = await animate_progress(status_msg, "combo", delay=0.8)
        code = run_shortpy(source)
        code = run_logic_changer(code)
        code = run_expiry_injector(code, date_str)
        code = run_obfuxtreme(code)
        await status_msg.edit_text(f"<b>{BRANDING}</b>\n\n{final_step}", parse_mode="HTML")
        base_name = filename.replace(".py", "")
        out_name = f"Alex_PremiumProtected_{base_name}.py"
        increment_user_files(user.id)
        for k in ["combo_source", "combo_filename", "awaiting_date", "module"]:
            context.user_data.pop(k, None)
        await _send_result(update, context, code, out_name,
            f"<b>{BRANDING}</b>\n\n✅ <b>Premium Combo complete!</b>\n📁 <code>{out_name}</code>\n📅 Expires: <code>{date_str}</code>\n\n"
            f"Pipeline: <code>✂️ Short Py → 🧠 Logic Changer → 🔐 Expiry Inject → ⚡ ObfuXtreme</code>\n\n<code>{BRANDING_LINE}</code>")
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text(f"<b>{BRANDING}</b>\n\n❌ Error: <code>{e}</code>", parse_mode="HTML", reply_markup=BACK_KB)

# ══════════════════ DISPATCHER ══════════════════
@require_not_banned
async def document_dispatcher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    doc = update.message.document
    get_or_create_user(user.id, user.username, user.first_name)
    if not doc.file_name.endswith(".py"):
        await update.message.reply_text(f"<b>{BRANDING}</b>\n\n❌ Please upload a valid <code>.py</code> Python file.", parse_mode="HTML")
        return
    module = context.user_data.get("module")
    awaiting_file = context.user_data.get("awaiting_file", False)
    if not module or not awaiting_file:
        await update.message.reply_text(f"<b>{BRANDING}</b>\n\n⚠️ Please select a module first from the menu.\n\nUse /start to open the main menu.", parse_mode="HTML")
        return
    router = {"obfuxtreme": obfuscate_file, "logic_changer": logic_file, "expiry": expiry_file, "shortpy": shortpy_file, "combo": combo_file}
    handler = router.get(module)
    if handler:
        await handler(update, context)
    else:
        await update.message.reply_text(f"<b>{BRANDING}</b>\n\n❌ Unknown module. Use /start.", parse_mode="HTML")

@require_not_banned
async def text_dispatcher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_or_create_user(user.id, user.username, user.first_name)
    module = context.user_data.get("module")
    awaiting_date = context.user_data.get("awaiting_date", False)
    if awaiting_date and module == "expiry":
        await expiry_date_input(update, context)
        return
    if awaiting_date and module == "combo":
        await combo_date_input(update, context)
        return
    await update.message.reply_text(f"<b>{BRANDING}</b>\n\nUse /start to open the main menu and choose a module.", parse_mode="HTML")

async def callback_dispatcher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    routes = {
        "nav:menu": menu_callback, "nav:profile": profile_handler,
        "nav:stats": stats_handler, "nav:help": help_handler,
        "mod:obfuxtreme": obfuscate_select, "mod:logic_changer": logic_select,
        "mod:expiry": expiry_select, "mod:shortpy": shortpy_select,
        "mod:combo": combo_select, "check_membership": membership_callback,
    }
    handler = routes.get(data)
    if handler:
        await handler(update, context)
    else:
        await query.answer("Unknown action.", show_alert=True)

# ══════════════════ MAIN ══════════════════
def build_app():
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN is not set!")
        sys.exit(1)
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("adduser", add_user))
    app.add_handler(CommandHandler("addpremium", add_premium))
    app.add_handler(CommandHandler("removepremium", remove_premium))
    app.add_handler(CommandHandler("ban", ban_user))
    app.add_handler(CommandHandler("unban", unban_user))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(CallbackQueryHandler(callback_dispatcher))
    app.add_handler(MessageHandler(filters.Document.ALL, document_dispatcher))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_dispatcher))
    return app

def main():
    init_db()
    keep_alive()
    logger.info(f"Starting {BRANDING}")
    app = build_app()
    logger.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
