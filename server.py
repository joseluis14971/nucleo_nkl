# -*- coding: utf-8 -*-
from datetime import datetime, timezone
import hashlib
"""
Nucleo NKL - Pool Server v3.1
Correcciones v2.2:
  - get_job asigna nonce_start unico por minero (anti-colision)
  - nonce_range devuelto en el job para que cada minero busque en su espacio
  - Todo lo demas identico a v2.1
"""

import time, hashlib, secrets, sqlite3, logging, os, struct
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from functools import wraps
from flask import Flask, jsonify, request, send_file, abort, g
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from nkl_constants import (
    get_block_reward, INITIAL_DIFFICULTY, DIFFICULTY_WINDOW,
    MIN_DIFFICULTY, MAX_DIFFICULTY, BLOCK_TIME_SECONDS,
    MEMORY_COST_KB, ARGON_ITERATIONS,
    FOUNDER_RESERVE, TOTAL_SUPPLY, MINEABLE_SUPPLY,
    HALVING_INTERVAL, NETWORK_FEE_PCT,
    FOUNDER_USER_DEFAULT, PREMINE_ACCOUNT, FEE_ACCOUNT,
    print_tokenomics
)

# ═══════════════════════════════════════════════════════
#  CONFIG — cambiar via variables de entorno en produccion
# ═══════════════════════════════════════════════════════
DB_PATH      = os.environ.get("NKL_DB",           "nkl_pool.db")
ADMIN_TOKEN  = os.environ.get("NKL_ADMIN_TOKEN",  "CAMBIA-ESTO-EN-PRODUCCION")
FOUNDER_USER = os.environ.get("NKL_FOUNDER_USER", FOUNDER_USER_DEFAULT)
FOUNDER_KEY  = os.environ.get("NKL_FOUNDER_KEY",  "")
FEE_KEY      = os.environ.get("NKL_FEE_KEY",      "")
PREMINE_KEY  = os.environ.get("NKL_PREMINE_KEY",  "")

# Retiros automaticos via Web3 (opcional)
BSC_SENDER_ADDRESS   = os.environ.get("NKL_BSC_ADDRESS", "")
BSC_PRIVATE_KEY      = os.environ.get("NKL_BSC_PRIVKEY", "")
NKL_CONTRACT_ADDRESS = os.environ.get("NKL_CONTRACT",    "")
AUTO_WITHDRAWAL_ENABLED = bool(
    BSC_SENDER_ADDRESS and BSC_PRIVATE_KEY and NKL_CONTRACT_ADDRESS
)

# Anti-granja
MAX_SHARES_PER_MINUTE = 10
MAX_SHARES_PER_HOUR   = 300
POW_TIMEOUT_SECONDS   = 30
MIN_SOLVE_TIME        = 2

# Rango de nonces por minero — 50 millones por slot
NONCE_RANGE_SIZE = 50_000_000

MIN_WITHDRAWAL   = 1000.0
NETWORKS_ALLOWED = ["BSC","ETH","POLYGON","ARBITRUM","SOLANA","OTHER"]

_pow_executor = ThreadPoolExecutor(max_workers=4)

# ═══════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("pool.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("nkl")

# ═══════════════════════════════════════════════════════
#  FLASK
# ═══════════════════════════════════════════════════════
app = Flask(__name__)
limiter = Limiter(
    key_func=get_remote_address, app=app,
    default_limits=["300 per minute"], storage_uri="memory://"
)

@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"]    = "nosniff"
    response.headers["X-Frame-Options"]           = "DENY"
    response.headers["X-XSS-Protection"]          = "1; mode=block"
    response.headers["Referrer-Policy"]           = "no-referrer"
    response.headers["Cache-Control"]             = "no-store"
    response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response

# ═══════════════════════════════════════════════════════
#  DATABASE
# ═══════════════════════════════════════════════════════
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=30)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
        g.db.execute("PRAGMA busy_timeout=10000")
        g.db.execute("PRAGMA synchronous=NORMAL")
        g.db.execute("PRAGMA cache_size=10000")
    return g.db

@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db:
        try: db.close()
        except: pass

def init_db():
    with sqlite3.connect(DB_PATH, timeout=30) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=10000")
        c.executescript("""
            CREATE TABLE IF NOT EXISTS miners (
                username           TEXT PRIMARY KEY,
                api_key            TEXT UNIQUE NOT NULL,
                created_at         INTEGER NOT NULL,
                is_founder         INTEGER DEFAULT 0,
                is_system          INTEGER DEFAULT 0,
                banned             INTEGER DEFAULT 0,
                last_share_at      INTEGER DEFAULT 0,
                shares_this_minute INTEGER DEFAULT 0,
                minute_window      INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS balances (
                username TEXT PRIMARY KEY,
                balance  REAL DEFAULT 0.0,
                locked   INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS shares (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                username     TEXT NOT NULL,
                nonce        TEXT NOT NULL,
                hash         TEXT NOT NULL,
                block_index  INTEGER NOT NULL,
                reward       REAL NOT NULL,
                submitted_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS blocks (
                index_        INTEGER PRIMARY KEY,
                previous_hash TEXT NOT NULL,
                timestamp     INTEGER NOT NULL,
                difficulty    REAL NOT NULL,
                reward        REAL NOT NULL,
                solved_by     TEXT,
                solved_at     INTEGER
            );
            CREATE TABLE IF NOT EXISTS fee_ledger (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                action    TEXT NOT NULL,
                amount    REAL NOT NULL,
                from_user TEXT,
                note      TEXT,
                ts        INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS premine_ledger (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                amount REAL NOT NULL,
                note   TEXT,
                ts     INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS withdrawals (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                username       TEXT NOT NULL,
                amount_nkl     REAL NOT NULL,
                fee_nkl        REAL NOT NULL,
                net_nkl        REAL NOT NULL,
                wallet_address TEXT NOT NULL,
                network        TEXT NOT NULL DEFAULT 'BSC',
                status         TEXT DEFAULT 'pending',
                tx_hash        TEXT,
                error_msg      TEXT,
                requested_at   INTEGER NOT NULL,
                processed_at   INTEGER,
                admin_note     TEXT,
                auto_processed INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS suspicious_log (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT,
                ip       TEXT,
                reason   TEXT,
                ts       INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_shares_block ON shares(block_index);
            CREATE INDEX IF NOT EXISTS idx_shares_user  ON shares(username);
            CREATE INDEX IF NOT EXISTS idx_shares_nonce ON shares(nonce, block_index);
            CREATE INDEX IF NOT EXISTS idx_shares_time  ON shares(submitted_at);
            CREATE INDEX IF NOT EXISTS idx_wd_user      ON withdrawals(username);
            CREATE INDEX IF NOT EXISTS idx_wd_status    ON withdrawals(status);
        """)
        c.commit()
    _init_system_accounts()
    _init_genesis()
    _init_founder()
    # Migración: asegurar que difficulty acepta decimales (SQLite lo maneja solo con REAL)
    try:
        with sqlite3.connect(DB_PATH, timeout=30) as cm:
            cm.execute("UPDATE blocks SET difficulty = CAST(difficulty AS REAL) WHERE typeof(difficulty)='integer'")
            cm.commit()
    except Exception:
        pass
    log.info("DB lista: %s", DB_PATH)

def _init_system_accounts():
    global FEE_KEY, PREMINE_KEY
    with sqlite3.connect(DB_PATH, timeout=30) as c:
        c.row_factory = sqlite3.Row

        row = c.execute(
            "SELECT api_key FROM miners WHERE username=?", (FEE_ACCOUNT,)
        ).fetchone()
        if not row:
            key     = FEE_KEY or secrets.token_hex(32)
            FEE_KEY = key
            c.execute(
                "INSERT INTO miners (username,api_key,created_at,is_system)"
                " VALUES (?,?,?,1)",
                (FEE_ACCOUNT, key, int(time.time()))
            )
            c.execute(
                "INSERT OR REPLACE INTO balances (username,balance,locked)"
                " VALUES (?,0.0,0)", (FEE_ACCOUNT,)
            )
            log.info("Cuenta fees: %s | key: %s", FEE_ACCOUNT, key)
        else:
            FEE_KEY = row["api_key"]

        row = c.execute(
            "SELECT api_key FROM miners WHERE username=?", (PREMINE_ACCOUNT,)
        ).fetchone()
        if not row:
            key        = PREMINE_KEY or secrets.token_hex(32)
            PREMINE_KEY = key
            c.execute(
                "INSERT INTO miners"
                " (username,api_key,created_at,is_system,is_founder)"
                " VALUES (?,?,?,1,1)",
                (PREMINE_ACCOUNT, key, int(time.time()))
            )
            c.execute(
                "INSERT OR REPLACE INTO balances (username,balance,locked)"
                " VALUES (?,?,1)",
                (PREMINE_ACCOUNT, float(FOUNDER_RESERVE))
            )
            c.execute(
                "INSERT INTO premine_ledger (action,amount,note,ts)"
                " VALUES (?,?,?,?)",
                ("GENESIS", FOUNDER_RESERVE,
                 f"30% premine del creador - {FOUNDER_RESERVE:,} NKL - BLOQUEADO",
                 int(time.time()))
            )
            log.info("=" * 60)
            log.info("PREMINE: %s | %s NKL (BLOQUEADO)",
                     PREMINE_ACCOUNT, f"{FOUNDER_RESERVE:,}")
            log.info("PREMINE KEY: %s", key)
            log.info("=" * 60)
        else:
            PREMINE_KEY = row["api_key"]

        c.commit()

def _init_genesis():
    with sqlite3.connect(DB_PATH, timeout=30) as c:
        c.row_factory = sqlite3.Row
        if not c.execute("SELECT 1 FROM blocks WHERE index_=1").fetchone():
            c.execute(
                "INSERT INTO blocks"
                " (index_,previous_hash,timestamp,difficulty,reward)"
                " VALUES (1,?,?,?,?)",
                ("0"*64, int(time.time()),
                 INITIAL_DIFFICULTY, get_block_reward(1))
            )
            c.commit()
            log.info("Bloque genesis #1 | dificultad=%d | reward=%.2f NKL",
                     INITIAL_DIFFICULTY, get_block_reward(1))

def _init_founder():
    global FOUNDER_KEY
    with sqlite3.connect(DB_PATH, timeout=30) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT api_key FROM miners WHERE username=?", (FOUNDER_USER,)
        ).fetchone()
        if not row:
            key         = FOUNDER_KEY or secrets.token_hex(32)
            FOUNDER_KEY = key
            c.execute(
                "INSERT INTO miners (username,api_key,created_at,is_founder)"
                " VALUES (?,?,?,1)",
                (FOUNDER_USER, key, int(time.time()))
            )
            c.execute(
                "INSERT OR IGNORE INTO balances (username,balance)"
                " VALUES (?,0.0)", (FOUNDER_USER,)
            )
            c.commit()
            log.info("=" * 60)
            log.info("FUNDADOR INICIALIZADO")
            log.info("  Usuario : %s", FOUNDER_USER)
            log.info("  API Key : %s", key)
            log.info("  GUARDA ESTA KEY - no se muestra de nuevo")
            log.info("=" * 60)
        else:
            FOUNDER_KEY = row["api_key"]

# ═══════════════════════════════════════════════════════
#  NKL-ARGON — IDENTICO al miner.py
# ═══════════════════════════════════════════════════════
_BLOCK_SIZE = 64
_NUM_BLOCKS = (MEMORY_COST_KB * 1024) // _BLOCK_SIZE

def nkl_hash(block_index, prev_hash, timestamp, nonce):
    seed = hashlib.sha256(
        f"{block_index}{prev_hash}{timestamp}{nonce}".encode()
    ).digest()
    memory = bytearray(_NUM_BLOCKS * _BLOCK_SIZE)
    prev   = seed
    for i in range(_NUM_BLOCKS):
        blk = hashlib.sha256(prev + struct.pack("<I", i)).digest() * 2
        off = i * _BLOCK_SIZE
        memory[off:off+_BLOCK_SIZE] = blk
        prev = blk[:32]
    for _ in range(ARGON_ITERATIONS):
        for i in range(_NUM_BLOCKS):
            j = struct.unpack(
                "<I", memory[i*_BLOCK_SIZE:i*_BLOCK_SIZE+4]
            )[0] % _NUM_BLOCKS
            for b in range(_BLOCK_SIZE):
                memory[i*_BLOCK_SIZE+b] ^= memory[j*_BLOCK_SIZE+b]
            nb = hashlib.sha256(
                memory[i*_BLOCK_SIZE:i*_BLOCK_SIZE+_BLOCK_SIZE]
            ).digest() * 2
            memory[i*_BLOCK_SIZE:i*_BLOCK_SIZE+_BLOCK_SIZE] = nb
    return hashlib.sha256(memory).hexdigest()

def _difficulty_to_target(difficulty):
    """
    Convierte dificultad flotante a target numerico.
    Calibrado para que D=N entero sea equivalente a startswith("0"*N).
    Un hash hex con N ceros al inicio: hash_int < 16^(64-N)
    Para D flotante interpolamos: target = 16^(64-D)
    """
    # 16^(64 - D) = 2^(4*(64-D)) = 2^(256 - 4*D)
    exp = 256.0 - 4.0 * float(difficulty)
    if exp <= 0:
        return 0
    # Usar interpolacion: 2^exp con exp flotante
    exp_int  = int(exp)
    exp_frac = exp - exp_int
    target   = (1 << exp_int)
    if exp_frac > 0:
        # multiplicar por 2^frac_part ≈ interpolacion lineal entre potencias
        import math
        target = int(target * (2 ** exp_frac))
    return target - 1

def _do_validate(block_index, prev_hash, timestamp, nonce, difficulty):
    h        = nkl_hash(block_index, prev_hash, timestamp, nonce)
    target   = _difficulty_to_target(difficulty)
    hash_int = int(h, 16)
    return hash_int <= target, h

def validate_pow_async(block_index, prev_hash, timestamp, nonce, difficulty):
    future = _pow_executor.submit(
        _do_validate, block_index, prev_hash, timestamp, nonce, difficulty
    )
    try:
        return future.result(timeout=POW_TIMEOUT_SECONDS)
    except FuturesTimeout:
        log.warning("PoW timeout nonce=%s", nonce)
        return False, ""

# ═══════════════════════════════════════════════════════
#  ANTI-GRANJA / ANTI-BOT
# ═══════════════════════════════════════════════════════
def check_miner_rate(db, username, ip):
    now  = int(time.time())
    row  = db.execute(
        "SELECT last_share_at,shares_this_minute,minute_window"
        " FROM miners WHERE username=?", (username,)
    ).fetchone()
    if not row: return True, ""

    last_at    = row["last_share_at"] or 0
    cur_min    = now // 60
    prev_min   = row["minute_window"] or 0
    shares_min = row["shares_this_minute"] or 0
    if cur_min != prev_min:
        shares_min = 0

    if last_at > 0 and (now - last_at) < MIN_SOLVE_TIME:
        _log_sus(db, username, ip, f"shares muy rapidos ({now-last_at}s)")
        return False, "Shares demasiado frecuentes."

    if shares_min >= MAX_SHARES_PER_MINUTE:
        _log_sus(db, username, ip, f"limite/min ({shares_min})")
        return False, f"Limite {MAX_SHARES_PER_MINUTE} shares/min."

    # CAMBIO 1: limite por hora eliminado - usar solo limite por bloque
    # hour_ago    = now - 3600
    # shares_hour = db.execute(...)
    # if shares_hour >= MAX_SHARES_PER_HOUR: return False

    return True, ""

def _log_sus(db, username, ip, reason):
    db.execute(
        "INSERT INTO suspicious_log (username,ip,reason,ts) VALUES (?,?,?,?)",
        (username, ip, reason, int(time.time()))
    )
    log.warning("SOSPECHOSO: %s ip=%s - %s", username, ip, reason)

def update_rate_counters(db, username):
    now     = int(time.time())
    cur_min = now // 60
    row     = db.execute(
        "SELECT shares_this_minute,minute_window FROM miners WHERE username=?",
        (username,)
    ).fetchone()
    if row:
        new_count = 1 if (row["minute_window"] or 0) != cur_min \
                    else (row["shares_this_minute"] or 0) + 1
        db.execute(
            "UPDATE miners SET last_share_at=?,shares_this_minute=?,"
            "minute_window=? WHERE username=?",
            (now, new_count, cur_min, username)
        )

# ═══════════════════════════════════════════════════════
#  DIFICULTAD DINAMICA
# ═══════════════════════════════════════════════════════
def recalculate_difficulty(db, last_index):
    cur_row = db.execute(
        "SELECT difficulty FROM blocks ORDER BY index_ DESC LIMIT 1"
    ).fetchone()
    current = float(cur_row["difficulty"]) if cur_row else float(INITIAL_DIFFICULTY)

    if last_index % DIFFICULTY_WINDOW != 0 or last_index < DIFFICULTY_WINDOW:
        return current

    rows  = db.execute("""
        SELECT solved_at FROM blocks WHERE solved_by IS NOT NULL
        ORDER BY index_ DESC LIMIT ?
    """, (DIFFICULTY_WINDOW,)).fetchall()
    times = [r["solved_at"] for r in rows if r["solved_at"]]
    if len(times) < 2:
        return current

    actual_time   = times[0] - times[-1]
    expected_time = BLOCK_TIME_SECONDS * (DIFFICULTY_WINDOW - 1)

    # Formula multiplicativa con dificultad flotante (como Bitcoin):
    # nueva = actual * (esperado / real)
    # Cap x1.5 / x0.67 para ajustes suaves en red chica
    ratio = actual_time / expected_time if expected_time > 0 else 1.0
    ratio = max(0.67, min(1.5, ratio))
    new_d = current * (1.0 / ratio)
    new_d = max(float(MIN_DIFFICULTY), min(float(MAX_DIFFICULTY), new_d))
    # Redondear a 2 decimales para legibilidad
    new_d = round(new_d, 2)

    if new_d != current:
        log.info("Dificultad: %.2f -> %.2f (ratio=%.3f, esperado=%ds, real=%ds)",
                 current, new_d, ratio, expected_time, actual_time)
    return new_d

# ═══════════════════════════════════════════════════════
#  FEE 0.5% -> FEE_ACCOUNT
# ═══════════════════════════════════════════════════════
def _apply_fee(db, from_user, gross):
    fee = round(gross * NETWORK_FEE_PCT, 8)
    net = round(gross - fee, 8)
    db.execute(
        "INSERT OR IGNORE INTO balances (username,balance) VALUES (?,0.0)",
        (FEE_ACCOUNT,)
    )
    db.execute(
        "UPDATE balances SET balance=balance+? WHERE username=?",
        (fee, FEE_ACCOUNT)
    )
    db.execute(
        "INSERT INTO fee_ledger (action,amount,from_user,note,ts)"
        " VALUES (?,?,?,?,?)",
        ("FEE_0.5PCT", fee, from_user,
         f"bruto:{gross:.4f} neto:{net:.4f}", int(time.time()))
    )
    return net, fee

# ═══════════════════════════════════════════════════════
#  RETIRO AUTOMATICO via Web3 (opcional)
# ═══════════════════════════════════════════════════════
def process_withdrawal_auto(wid, username, net_nkl, wallet_address, network):
    if not AUTO_WITHDRAWAL_ENABLED:
        return False, "Procesamiento manual requerido."
    if network not in ("BSC", "ETH", "POLYGON"):
        return False, f"Red {network} requiere procesamiento manual."
    try:
        from web3 import Web3
        from web3.middleware import geth_poa_middleware
        rpc = {"BSC":"https://bsc-dataseed.binance.org/",
               "ETH":"https://mainnet.infura.io/v3/YOUR_KEY",
               "POLYGON":"https://polygon-rpc.com/"}
        w3  = Web3(Web3.HTTPProvider(rpc[network]))
        if network == "BSC":
            w3.middleware_onion.inject(geth_poa_middleware, layer=0)
        if not w3.is_connected():
            return False, "Sin conexion a la blockchain."
        abi = [{"constant":False,"inputs":[
            {"name":"_to","type":"address"},
            {"name":"_value","type":"uint256"}
        ],"name":"transfer","outputs":[{"name":"","type":"bool"}],
        "type":"function"}]
        contract   = w3.eth.contract(
            address=Web3.to_checksum_address(NKL_CONTRACT_ADDRESS), abi=abi
        )
        sender     = Web3.to_checksum_address(BSC_SENDER_ADDRESS)
        recipient  = Web3.to_checksum_address(wallet_address)
        amount_wei = int(net_nkl * 10**18)
        nonce_val  = w3.eth.get_transaction_count(sender)
        txn = contract.functions.transfer(recipient, amount_wei).build_transaction({
            "chainId": w3.eth.chain_id, "gas": 100000,
            "gasPrice": w3.eth.gas_price, "nonce": nonce_val,
        })
        signed  = w3.eth.account.sign_transaction(txn, private_key=BSC_PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt["status"] == 1:
            tx_hex = tx_hash.hex()
            log.info("Retiro auto OK: %s -> %.4f NKL | TX: %s",
                     username, net_nkl, tx_hex[:20])
            return True, tx_hex
        return False, "Transaccion revertida."
    except ImportError:
        return False, "web3 no instalado: pip install web3"
    except Exception as e:
        log.warning("Error retiro auto #%d: %s", wid, str(e))
        return False, str(e)[:200]

# ═══════════════════════════════════════════════════════
#  AUTH
# ═══════════════════════════════════════════════════════
def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-API-Key") or request.args.get("api_key")
        if not key: abort(401, "API key requerida")
        row = get_db().execute(
            "SELECT username,banned FROM miners WHERE api_key=?", (key,)
        ).fetchone()
        if not row: abort(403, "API key invalida")
        if row["banned"]: abort(403, "Cuenta suspendida")
        request.miner_username = row["username"]
        return f(*args, **kwargs)
    return decorated

def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
            abort(403, "No autorizado")
        return f(*args, **kwargs)
    return decorated

def require_founder(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.headers.get("X-API-Key") != FOUNDER_KEY:
            abort(403, "Solo el fundador")
        return f(*args, **kwargs)
    return decorated

# ═══════════════════════════════════════════════════════
#  REGISTRO
# ═══════════════════════════════════════════════════════
@app.route("/register", methods=["POST"])
@limiter.limit("3 per hour")
def register():
    data     = request.get_json(silent=True) or {}
    username = data.get("username","").strip()
    if not username or not username.isalnum() or not 3<=len(username)<=32:
        return jsonify({"status":"error",
                        "message":"Username invalido (3-32 alfanumerico)"}), 400
    reserved = {FOUNDER_USER.lower(), FEE_ACCOUNT.lower(),
                PREMINE_ACCOUNT.lower(), "admin","system",
                "nkl","nucleo","founder","creador"}
    if username.lower() in reserved:
        return jsonify({"status":"error","message":"Username reservado"}), 403
    db = get_db()
    if db.execute("SELECT 1 FROM miners WHERE username=?", (username,)).fetchone():
        return jsonify({"status":"error","message":"Username ya registrado"}), 409
    key = secrets.token_hex(32)
    db.execute(
        "INSERT INTO miners (username,api_key,created_at) VALUES (?,?,?)",
        (username, key, int(time.time()))
    )
    db.execute(
        "INSERT OR IGNORE INTO balances (username,balance) VALUES (?,0.0)",
        (username,)
    )
    db.commit()
    log.info("Nuevo minero: %s ip=%s", username, request.remote_addr)
    return jsonify({"status":"ok","username":username,"api_key":key}), 201

# ═══════════════════════════════════════════════════════
#  JOB — v2.2: nonce_start unico por minero
# ═══════════════════════════════════════════════════════
def _get_miner_slot(db, username):
    """
    Devuelve un slot numerico unico y estable por minero.
    Usa el rowid de la tabla miners para no depender del orden de registro.
    El slot determina el rango de nonces: slot * NONCE_RANGE_SIZE
    Con 50M nonces por slot y hasta 100 mineros = 5B nonces distintos.
    """
    row = db.execute(
        "SELECT rowid FROM miners WHERE username=?", (username,)
    ).fetchone()
    if not row:
        return 0
    # Usar modulo 1000 para evitar nonces demasiado grandes
    return (row["rowid"] - 1) % 1000

@app.route("/get_job")
@require_api_key
@limiter.limit("120 per minute")
def get_job():
    db    = get_db()
    block = db.execute(
        "SELECT * FROM blocks WHERE solved_by IS NULL"
        " ORDER BY index_ DESC LIMIT 1"
    ).fetchone()
    if not block:
        # Auto-recuperacion: crear nuevo bloque si no hay ninguno abierto
        try:
            last = db.execute(
                "SELECT index_, difficulty FROM blocks ORDER BY index_ DESC LIMIT 1"
            ).fetchone()
            if last:
                new_index  = last["index_"] + 1
                new_diff   = float(last["difficulty"])
            else:
                new_index  = 1
                new_diff   = float(INITIAL_DIFFICULTY)
            new_reward = get_block_reward(new_index)
            db.execute(
                "INSERT OR IGNORE INTO blocks"
                " (index_,previous_hash,timestamp,difficulty,reward)"
                " VALUES (?,?,?,?,?)",
                (new_index, "auto_recovery", int(time.time()), new_diff, new_reward)
            )
            db.commit()
            log.warning("Auto-recovery: bloque #%d creado (no habia bloque abierto)", new_index)
            block = db.execute(
                "SELECT * FROM blocks WHERE solved_by IS NULL"
                " ORDER BY index_ DESC LIMIT 1"
            ).fetchone()
        except Exception as e:
            log.error("Auto-recovery fallido: %s", e)
        if not block:
            return jsonify({"status":"error","message":"Sin bloques disponibles"}), 503

    # Calcular nonce_start unico para este minero
    slot        = _get_miner_slot(db, request.miner_username)
    nonce_start = slot * NONCE_RANGE_SIZE
    nonce_end   = nonce_start + NONCE_RANGE_SIZE - 1

    return jsonify({
        "status": "ok",
        "job": {
            "index":          block["index_"],
            "previous_hash":  block["previous_hash"],
            "timestamp":      block["timestamp"],
            "difficulty":     block["difficulty"],
            "reward":         block["reward"],
            "memory_cost_kb": MEMORY_COST_KB,
            "argon_iter":     ARGON_ITERATIONS,
            "nonce_start":    nonce_start,
            "nonce_end":      nonce_end,
        }
    })

# ═══════════════════════════════════════════════════════
#  PPLNS — dificultad parcial (1 cero)
# ═══════════════════════════════════════════════════════
PARTIAL_DIFFICULTY = 1   # shares parciales: hash empieza con "0"
MAX_PARTIAL_PER_BLOCK = 500  # limite anti-spam por minero por bloque

def _pplns_distribute(db, block_index, reward):
    """
    Reparte la recompensa del bloque entre todos los mineros
    segun sus shares parciales acumulados en esa ronda.
    Incluye al resolvedor que ya insertó su share final.
    """
    share_rows = db.execute(
        "SELECT username, COUNT(*) AS cnt FROM shares"
        " WHERE block_index=? GROUP BY username",
        (block_index,)
    ).fetchall()

    total = sum(r["cnt"] for r in share_rows)
    if total == 0:
        return

    for r in share_rows:
        proporcion = r["cnt"] / total
        bruto      = round(reward * proporcion, 8)
        neto, _    = _apply_fee(db, r["username"], bruto)
        db.execute(
            "INSERT OR IGNORE INTO balances (username,balance) VALUES (?,0.0)",
            (r["username"],)
        )
        db.execute(
            "UPDATE balances SET balance=balance+? WHERE username=?",
            (neto, r["username"])
        )
        log.info("  PPLNS: %s | shares=%d/%d (%.1f%%) | %.4f NKL",
                 r["username"], r["cnt"], total, proporcion*100, neto)

# ═══════════════════════════════════════════════════════
#  SUBMIT SHARE PARCIAL — v2.4 PPLNS
# ═══════════════════════════════════════════════════════
@app.route("/submit_share", methods=["POST"])
@require_api_key
@limiter.limit("120 per minute")
def submit_share():
    """
    Acepta shares parciales (dificultad 1 = hash empieza con '0').
    No resuelve el bloque — solo acumula participacion del minero.
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"status":"error","message":"JSON invalido"}), 400

    username    = request.miner_username
    ip          = request.remote_addr
    nonce       = data.get("nonce")
    block_index = data.get("block_index")
    hash_result = data.get("hash")

    if nonce is None or block_index is None or not hash_result:
        return jsonify({"status":"error","message":"Faltan campos"}), 400
    try:
        block_index = int(block_index)
        nonce       = int(nonce)
    except (ValueError, TypeError):
        return jsonify({"status":"error","message":"Tipos invalidos"}), 400

    # Verificar que el hash cumple dificultad parcial
    if not hash_result.startswith("0" * PARTIAL_DIFFICULTY):
        return jsonify({"status":"rejected","message":"Hash parcial invalido"}), 400

    db = get_db()

    # Verificar que el bloque existe y no está resuelto
    block = db.execute(
        "SELECT * FROM blocks WHERE index_=? AND solved_by IS NULL",
        (block_index,)
    ).fetchone()
    if not block:
        return jsonify({"status":"error","message":"Bloque invalido o ya resuelto"}), 400

    # Anti-spam: limite de shares parciales por minero por bloque
    count = db.execute(
        "SELECT COUNT(*) FROM shares WHERE username=? AND block_index=?",
        (username, block_index)
    ).fetchone()[0]
    if count >= MAX_PARTIAL_PER_BLOCK:
        # Verificar si todos los mineros activos llegaron al limite
        # (minero activo = tuvo share en los ultimos 30 minutos)
        active_cutoff = int(time.time()) - 1800
        active_miners = db.execute(
            "SELECT COUNT(DISTINCT username) FROM shares"
            " WHERE block_index=? AND submitted_at>?",
            (block_index, active_cutoff)
        ).fetchone()[0]
        maxed_miners = db.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT username FROM shares WHERE block_index=?"
            "  GROUP BY username HAVING COUNT(*)>=?"
            ")",
            (block_index, MAX_PARTIAL_PER_BLOCK)
        ).fetchone()[0]
        if active_miners > 0 and maxed_miners >= active_miners:
            # Todos los mineros activos llegaron al limite — force-resolve
            log.warning("Bloque #%d trabado: force-resolve por shares saturados", block_index)
            now_fr    = int(time.time())
            reward_fr = get_block_reward(block_index)
            db.execute(
                "UPDATE blocks SET solved_by=?,solved_at=? WHERE index_=?",
                ("pool_auto", now_fr, block_index)
            )
            _pplns_distribute(db, block_index, reward_fr)
            new_diff_fr    = recalculate_difficulty(db, block_index)
            next_reward_fr = get_block_reward(block_index + 1)
            # Hash real del bloque forzado (determinístico, encadena la cadena)
            prev_row_fr = db.execute(
                "SELECT previous_hash FROM blocks WHERE index_=?", (block_index,)
            ).fetchone()
            prev_prev_fr = prev_row_fr["previous_hash"] if prev_row_fr else ""
            forced_hash = hashlib.sha256(
                f"{block_index}|{prev_prev_fr}|{now_fr}|pool_auto".encode()
            ).hexdigest()
            db.execute(
                "INSERT OR IGNORE INTO blocks"
                " (index_,previous_hash,timestamp,difficulty,reward)"
                " VALUES (?,?,?,?,?)",
                (block_index+1, forced_hash, now_fr, new_diff_fr, next_reward_fr)
            )
            db.commit()
            log.info("Bloque #%d force-resuelto | nuevo bloque #%d dif=%.2f",
                     block_index, block_index+1, new_diff_fr)
            return jsonify({"status":"ok","message":"Bloque forzado, nuevo bloque disponible"})
        return jsonify({"status":"rejected","message":"Limite de shares por bloque"}), 429

    # Nonce duplicado
    if db.execute(
        "SELECT 1 FROM shares WHERE nonce=? AND block_index=?",
        (str(nonce), block_index)
    ).fetchone():
        return jsonify({"status":"ok","message":"Nonce ya registrado"}), 200

    db.execute(
        "INSERT INTO shares (username,nonce,hash,block_index,reward,submitted_at)"
        " VALUES (?,?,?,?,0.0,?)",
        (username, str(nonce), hash_result, block_index, int(time.time()))
    )
    db.commit()
    return jsonify({"status":"ok","partial":True})

# ═══════════════════════════════════════════════════════
#  SUBMIT SOLUCION FINAL — v2.4 PPLNS
# ═══════════════════════════════════════════════════════
@app.route("/submit_solution", methods=["POST"])
@require_api_key
@limiter.limit("30 per minute")
def submit_solution():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"status":"error","message":"JSON invalido"}), 400

    username    = request.miner_username
    ip          = request.remote_addr
    nonce       = data.get("nonce")
    block_index = data.get("block_index")

    if nonce is None or block_index is None:
        return jsonify({"status":"error","message":"Faltan nonce o block_index"}), 400
    try:
        block_index = int(block_index)
        nonce       = int(nonce)
    except (ValueError, TypeError):
        return jsonify({"status":"error","message":"Tipos invalidos"}), 400

    db = get_db()

    allowed, reason = check_miner_rate(db, username, ip)
    if not allowed:
        return jsonify({"status":"rejected","message":reason}), 429

    block = db.execute(
        "SELECT * FROM blocks WHERE index_=? AND solved_by IS NULL",
        (block_index,)
    ).fetchone()
    if not block:
        return jsonify({"status":"error",
                        "message":"Bloque invalido o ya resuelto"}), 400

    # Validar PoW completo
    valid, hash_result = validate_pow_async(
        block["index_"], block["previous_hash"],
        block["timestamp"], nonce, block["difficulty"]
    )
    if not valid:
        log.warning("Solucion invalida: %s ip=%s", username, ip)
        return jsonify({"status":"rejected","message":"Hash invalido"}), 400

    reward = get_block_reward(block_index)
    now    = int(time.time())

    # Insertar share final del resolvedor (si no existe ya como parcial)
    if not db.execute(
        "SELECT 1 FROM shares WHERE nonce=? AND block_index=?",
        (str(nonce), block_index)
    ).fetchone():
        db.execute(
            "INSERT INTO shares (username,nonce,hash,block_index,reward,submitted_at)"
            " VALUES (?,?,?,?,?,?)",
            (username, str(nonce), hash_result, block_index, reward, now)
        )
    else:
        # Actualizar el share parcial existente con el hash final
        db.execute(
            "UPDATE shares SET hash=?,reward=? WHERE nonce=? AND block_index=?",
            (hash_result, reward, str(nonce), block_index)
        )

    # Cerrar el bloque
    db.execute(
        "UPDATE blocks SET solved_by=?,solved_at=? WHERE index_=?",
        (username, now, block_index)
    )

    # ── PPLNS: repartir recompensa segun shares acumulados ──
    _pplns_distribute(db, block_index, reward)

    update_rate_counters(db, username)

    new_diff    = recalculate_difficulty(db, block_index)
    next_reward = get_block_reward(block_index + 1)
    if next_reward < reward:
        log.info("HALVING bloque %d: %.4f -> %.4f NKL",
                 block_index+1, reward, next_reward)

    db.execute(
        "INSERT OR IGNORE INTO blocks"
        " (index_,previous_hash,timestamp,difficulty,reward)"
        " VALUES (?,?,?,?,?)",
        (block_index+1, hash_result, now, new_diff, next_reward)
    )

    cutoff = now - (60 * 86400)
    db.execute(
        "DELETE FROM shares WHERE submitted_at<? AND block_index<?",
        (cutoff, block_index - 10000)
    )

    db.commit()
    log.info("Bloque #%d | resolvedor=%s | reward=%.2f NKL | dif=%d | participantes=%d",
             block_index, username, reward, new_diff,
             db.execute("SELECT COUNT(DISTINCT username) FROM shares"
                       " WHERE block_index=?", (block_index,)).fetchone()[0])
    return jsonify({"status":"ok","hash":hash_result,"reward":reward})

# ═══════════════════════════════════════════════════════
#  STATS PUBLICAS
# ═══════════════════════════════════════════════════════
@app.route("/stats/pool")
def pool_stats():
    db            = get_db()
    total_shares  = db.execute("SELECT COUNT(*) FROM shares").fetchone()[0]
    active_miners = db.execute(
        "SELECT COUNT(DISTINCT username) FROM shares"
    ).fetchone()[0]
    blocks_solved = db.execute(
        "SELECT COUNT(*) FROM blocks WHERE solved_by IS NOT NULL"
    ).fetchone()[0]
    total_issued  = db.execute(
        "SELECT COALESCE(SUM(reward),0) FROM blocks WHERE solved_by IS NOT NULL"
    ).fetchone()[0]
    cur = db.execute(
        "SELECT index_,difficulty,reward FROM blocks"
        " WHERE solved_by IS NULL ORDER BY index_ DESC LIMIT 1"
    ).fetchone()
    era = (cur["index_"] // HALVING_INTERVAL) if cur else 0
    return jsonify({
        "status":           "ok",
        "total_shares":     total_shares,
        "active_miners":    active_miners,
        "blocks_solved":    blocks_solved,
        "total_issued_nkl": round(total_issued, 2),
        "total_supply":     TOTAL_SUPPLY,
        "mineable_supply":  MINEABLE_SUPPLY,
        "pct_mined":        round(total_issued/MINEABLE_SUPPLY*100, 6),
        "current_block": {
            "index":      cur["index_"]     if cur else None,
            "difficulty": cur["difficulty"] if cur else None,
            "reward":     cur["reward"]     if cur else None,
            "era":        era,
        },
        "next_halving_block": (era+1) * HALVING_INTERVAL,
        "auto_withdrawal":    AUTO_WITHDRAWAL_ENABLED,
    })

@app.route("/stats/leaderboard")
def leaderboard():
    db   = get_db()
    rows = db.execute("""
        SELECT b.username, b.balance,
               COUNT(s.id)         AS shares,
               MAX(s.submitted_at) AS last_share
        FROM balances b
        LEFT JOIN shares s ON s.username=b.username
        WHERE b.username NOT IN (?,?,?)
        GROUP BY b.username ORDER BY b.balance DESC LIMIT 50
    """, (FOUNDER_USER, FEE_ACCOUNT, PREMINE_ACCOUNT)).fetchall()
    return jsonify({
        "status": "ok",
        "leaderboard": [
            {"username":   r["username"],
             "shares":     r["shares"] or 0,
             "balance":    round(r["balance"], 5),
             "last_share": r["last_share"]}
            for r in rows
        ]
    })

@app.route("/stats/me")
@require_api_key
def my_stats():
    db  = get_db()
    bal = db.execute(
        "SELECT balance FROM balances WHERE username=?",
        (request.miner_username,)
    ).fetchone()
    s   = db.execute(
        "SELECT COUNT(*) AS n, MAX(submitted_at) AS last"
        " FROM shares WHERE username=?",
        (request.miner_username,)
    ).fetchone()
    return jsonify({
        "status":     "ok",
        "username":   request.miner_username,
        "balance":    round(bal["balance"] if bal else 0, 5),
        "shares":     s["n"],
        "last_share": s["last"]
    })

# ═══════════════════════════════════════════════════════
#  RETIROS
# ═══════════════════════════════════════════════════════
@app.route("/withdrawal/request", methods=["POST"])
@require_api_key
@limiter.limit("5 per hour")
def withdrawal_request():
    data           = request.get_json(silent=True) or {}
    username       = request.miner_username
    amount         = data.get("amount")
    wallet_address = data.get("wallet_address","").strip()
    network        = data.get("network","BSC").upper().strip()

    if not isinstance(amount,(int,float)) or amount < MIN_WITHDRAWAL:
        return jsonify({"status":"error",
                        "message":f"Minimo {MIN_WITHDRAWAL:,.0f} NKL"}), 400
    if not wallet_address or len(wallet_address) < 10:
        return jsonify({"status":"error","message":"Wallet invalida"}), 400
    if network not in NETWORKS_ALLOWED:
        return jsonify({"status":"error",
                        "message":f"Red invalida: {', '.join(NETWORKS_ALLOWED)}"}), 400

    db      = get_db()
    bal_row = db.execute(
        "SELECT balance,locked FROM balances WHERE username=?", (username,)
    ).fetchone()
    if not bal_row:
        return jsonify({"status":"error","message":"Cuenta no encontrada"}), 404
    if bal_row["locked"]:
        return jsonify({"status":"error","message":"Cuenta bloqueada"}), 403
    if bal_row["balance"] < amount:
        return jsonify({"status":"error",
                        "message":f"Saldo insuficiente ({bal_row['balance']:.5f} NKL)"}), 400

    if db.execute(
        "SELECT 1 FROM withdrawals WHERE username=? AND status='pending'",
        (username,)
    ).fetchone():
        return jsonify({"status":"error",
                        "message":"Ya tenes un retiro pendiente"}), 400

    fee_nkl = round(amount * NETWORK_FEE_PCT, 8)
    net_nkl = round(amount - fee_nkl, 8)

    _apply_fee(db, username, amount)
    db.execute(
        "UPDATE balances SET balance=balance-? WHERE username=?",
        (amount, username)
    )
    db.execute("""
        INSERT INTO withdrawals
        (username,amount_nkl,fee_nkl,net_nkl,wallet_address,
         network,status,requested_at)
        VALUES (?,?,?,?,?,?,'pending',?)
    """, (username, amount, fee_nkl, net_nkl, wallet_address,
          network, int(time.time())))
    db.commit()

    wid = db.execute(
        "SELECT id FROM withdrawals WHERE username=?"
        " ORDER BY requested_at DESC LIMIT 1",
        (username,)
    ).fetchone()["id"]

    log.info("Retiro #%d: %s | %.4f -> %.4f NKL | %s | %s...",
             wid, username, amount, net_nkl, network, wallet_address[:12])

    success, result = process_withdrawal_auto(
        wid, username, net_nkl, wallet_address, network
    )
    if success:
        db.execute("""
            UPDATE withdrawals
            SET status='completed',tx_hash=?,processed_at=?,
                auto_processed=1,admin_note='Automatico'
            WHERE id=?
        """, (result, int(time.time()), wid))
        db.commit()
        return jsonify({
            "status":"ok","message":"Retiro procesado automaticamente.",
            "amount_nkl":amount,"fee_nkl":fee_nkl,"net_nkl":net_nkl,
            "tx_hash":result,"network":network,"auto":True
        }), 201
    else:
        db.execute(
            "UPDATE withdrawals SET error_msg=? WHERE id=?", (result, wid)
        )
        db.commit()
        return jsonify({
            "status":"ok",
            "message":"Retiro en cola. Sera procesado manualmente.",
            "amount_nkl":amount,"fee_nkl":fee_nkl,"net_nkl":net_nkl,
            "network":network,"auto":False
        }), 201

@app.route("/withdrawal/history")
@require_api_key
def withdrawal_history():
    db   = get_db()
    rows = db.execute("""
        SELECT id,amount_nkl,fee_nkl,net_nkl,wallet_address,network,
               status,tx_hash,requested_at,processed_at,admin_note,auto_processed
        FROM withdrawals WHERE username=?
        ORDER BY requested_at DESC LIMIT 50
    """, (request.miner_username,)).fetchall()
    bal  = db.execute(
        "SELECT balance FROM balances WHERE username=?",
        (request.miner_username,)
    ).fetchone()
    return jsonify({
        "status":        "ok",
        "balance":       round(bal["balance"] if bal else 0, 5),
        "min_withdrawal":MIN_WITHDRAWAL,
        "fee_pct":       NETWORK_FEE_PCT * 100,
        "auto_enabled":  AUTO_WITHDRAWAL_ENABLED,
        "withdrawals":   [dict(r) for r in rows]
    })

@app.route("/withdrawal/cancel/<int:wid>", methods=["POST"])
@require_api_key
def withdrawal_cancel(wid):
    username = request.miner_username
    db       = get_db()
    row      = db.execute(
        "SELECT * FROM withdrawals WHERE id=? AND username=? AND status='pending'",
        (wid, username)
    ).fetchone()
    if not row:
        return jsonify({"status":"error","message":"Retiro no encontrado"}), 404
    db.execute(
        "UPDATE withdrawals SET status='cancelled',processed_at=? WHERE id=?",
        (int(time.time()), wid)
    )
    db.execute(
        "UPDATE balances SET balance=balance+? WHERE username=?",
        (row["net_nkl"], username)
    )
    db.commit()
    return jsonify({"status":"ok","returned_nkl":row["net_nkl"]})

# ═══════════════════════════════════════════════════════
#  FUNDADOR
# ═══════════════════════════════════════════════════════
@app.route("/founder/premine")
@require_founder
def founder_premine():
    db  = get_db()
    bal = db.execute(
        "SELECT balance FROM balances WHERE username=?", (PREMINE_ACCOUNT,)
    ).fetchone()
    ledger = db.execute(
        "SELECT * FROM premine_ledger ORDER BY ts DESC LIMIT 50"
    ).fetchall()
    return jsonify({
        "status":  "ok",
        "account": PREMINE_ACCOUNT,
        "balance": round(bal["balance"] if bal else 0, 2),
        "locked":  True,
        "note":    "30% premine del creador. Cuenta publica e inmovil.",
        "ledger":  [dict(r) for r in ledger]
    })

@app.route("/founder/fees")
@require_founder
def founder_fees():
    db    = get_db()
    bal   = db.execute(
        "SELECT balance FROM balances WHERE username=?", (FEE_ACCOUNT,)
    ).fetchone()
    total = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM fee_ledger WHERE action='FEE_0.5PCT'"
    ).fetchone()[0]
    recent = db.execute(
        "SELECT * FROM fee_ledger ORDER BY ts DESC LIMIT 100"
    ).fetchall()
    return jsonify({
        "status":          "ok",
        "account":         FEE_ACCOUNT,
        "balance":         round(bal["balance"] if bal else 0, 2),
        "total_collected": round(total, 2),
        "ledger":          [dict(r) for r in recent]
    })

@app.route("/founder/fees/withdraw", methods=["POST"])
@require_founder
def founder_fees_withdraw():
    data           = request.get_json(silent=True) or {}
    wallet_address = data.get("wallet_address","").strip()
    network        = data.get("network","BSC").upper()
    if not wallet_address:
        return jsonify({"status":"error","message":"wallet_address requerida"}), 400
    db  = get_db()
    bal = db.execute(
        "SELECT balance FROM balances WHERE username=?", (FEE_ACCOUNT,)
    ).fetchone()
    amount = bal["balance"] if bal else 0
    if amount <= 0:
        return jsonify({"status":"error","message":"Sin fees acumulados"}), 400
    db.execute(
        "UPDATE balances SET balance=0 WHERE username=?", (FEE_ACCOUNT,)
    )
    db.execute(
        "INSERT INTO fee_ledger (action,amount,from_user,note,ts)"
        " VALUES (?,?,?,?,?)",
        ("FOUNDER_WITHDRAW", amount, FOUNDER_USER,
         f"Retiro fees -> {wallet_address} ({network})", int(time.time()))
    )
    db.commit()
    log.info("Fundador retiro fees: %.4f NKL -> %s", amount, wallet_address)
    return jsonify({
        "status":"ok","amount":amount,
        "wallet_address":wallet_address,"network":network,
        "note":"Procesar manualmente desde tu wallet."
    })

@app.route("/founder/distribute", methods=["POST"])
@require_founder
def founder_distribute():
    data      = request.get_json(silent=True) or {}
    recipient = data.get("to","").strip()
    amount    = data.get("amount")
    note      = data.get("note","")
    if not recipient or not isinstance(amount,(int,float)) or amount<=0:
        return jsonify({"status":"error","message":"Parametros invalidos"}), 400
    db  = get_db()
    bal = db.execute(
        "SELECT balance FROM balances WHERE username=?", (FOUNDER_USER,)
    ).fetchone()
    if not bal or bal["balance"] < amount:
        return jsonify({"status":"error","message":"Saldo insuficiente"}), 400
    if not db.execute(
        "SELECT 1 FROM miners WHERE username=?", (recipient,)
    ).fetchone():
        return jsonify({"status":"error","message":"Destinatario no existe"}), 404
    db.execute(
        "UPDATE balances SET balance=balance-? WHERE username=?",
        (amount, FOUNDER_USER)
    )
    db.execute(
        "INSERT OR IGNORE INTO balances (username,balance) VALUES (?,0)", (recipient,)
    )
    db.execute(
        "UPDATE balances SET balance=balance+? WHERE username=?",
        (amount, recipient)
    )
    db.commit()
    return jsonify({"status":"ok","distributed":amount,"to":recipient,"note":note})

# ═══════════════════════════════════════════════════════
#  ADMIN
# ═══════════════════════════════════════════════════════
@app.route("/admin/withdrawals/pending")
@require_admin
def admin_withdrawals_pending():
    db   = get_db()
    rows = db.execute("""
        SELECT w.*, b.balance AS current_balance
        FROM withdrawals w JOIN balances b ON b.username=w.username
        WHERE w.status='pending' ORDER BY w.requested_at ASC
    """).fetchall()
    return jsonify({"status":"ok","count":len(rows),
                    "pending":[dict(r) for r in rows]})

@app.route("/admin/withdrawals/process/<int:wid>", methods=["POST"])
@require_admin
def admin_process_withdrawal(wid):
    data    = request.get_json(silent=True) or {}
    tx_hash = data.get("tx_hash","").strip()
    if not tx_hash:
        return jsonify({"status":"error","message":"tx_hash requerido"}), 400
    db  = get_db()
    row = db.execute(
        "SELECT 1 FROM withdrawals WHERE id=? AND status='pending'", (wid,)
    ).fetchone()
    if not row:
        return jsonify({"status":"error","message":"No encontrado"}), 404
    db.execute("""
        UPDATE withdrawals
        SET status='completed',tx_hash=?,processed_at=?,
            admin_note=?,auto_processed=0
        WHERE id=?
    """, (tx_hash, int(time.time()), data.get("note","Manual"), wid))
    db.commit()
    log.info("Retiro #%d completado | TX: %s", wid, tx_hash[:20])
    return jsonify({"status":"ok","tx_hash":tx_hash})

@app.route("/admin/withdrawals/reject/<int:wid>", methods=["POST"])
@require_admin
def admin_reject_withdrawal(wid):
    data = request.get_json(silent=True) or {}
    db   = get_db()
    row  = db.execute(
        "SELECT * FROM withdrawals WHERE id=? AND status='pending'", (wid,)
    ).fetchone()
    if not row:
        return jsonify({"status":"error","message":"No encontrado"}), 404
    db.execute(
        "UPDATE withdrawals SET status='rejected',processed_at=?,admin_note=?"
        " WHERE id=?",
        (int(time.time()), data.get("note","Rechazado"), wid)
    )
    db.execute(
        "UPDATE balances SET balance=balance+? WHERE username=?",
        (row["net_nkl"], row["username"])
    )
    db.commit()
    return jsonify({"status":"ok","returned":row["net_nkl"]})

@app.route("/admin/set_difficulty", methods=["POST"])
@require_admin
def set_difficulty():
    data = request.get_json(silent=True) or {}
    d    = data.get("difficulty")
    if not isinstance(d,(int,float)) or not MIN_DIFFICULTY<=d<=MAX_DIFFICULTY:
        return jsonify({"status":"error","message":"Dificultad invalida"}), 400
    db = get_db()
    db.execute("UPDATE blocks SET difficulty=? WHERE solved_by IS NULL", (d,))
    db.commit()
    return jsonify({"status":"ok","difficulty":d})

@app.route("/admin/ban/<string:username>", methods=["POST"])
@require_admin
def admin_ban(username):
    db = get_db()
    if not db.execute(
        "SELECT 1 FROM miners WHERE username=?", (username,)
    ).fetchone():
        return jsonify({"status":"error","message":"No encontrado"}), 404
    db.execute("UPDATE miners SET banned=1 WHERE username=?", (username,))
    db.commit()
    log.info("Baneado: %s", username)
    return jsonify({"status":"ok"})

@app.route("/admin/unban/<string:username>", methods=["POST"])
@require_admin
def admin_unban(username):
    db = get_db()
    db.execute("UPDATE miners SET banned=0 WHERE username=?", (username,))
    db.commit()
    return jsonify({"status":"ok"})

@app.route("/admin/suspicious")
@require_admin
def admin_suspicious():
    db   = get_db()
    rows = db.execute(
        "SELECT * FROM suspicious_log ORDER BY ts DESC LIMIT 200"
    ).fetchall()
    return jsonify({"status":"ok","log":[dict(r) for r in rows]})

@app.route("/admin/network_status")
@require_admin
def network_status():
    db   = get_db()
    rows = db.execute("""
        SELECT index_,difficulty,reward,solved_by,solved_at,
               (solved_at-timestamp) AS solve_time
        FROM blocks WHERE solved_by IS NOT NULL
        ORDER BY index_ DESC LIMIT 20
    """).fetchall()
    return jsonify({"status":"ok","recent_blocks":[dict(r) for r in rows]})

# ═══════════════════════════════════════════════════════
#  EXPLORADOR
# ═══════════════════════════════════════════════════════
@app.route("/explorer/blocks")
def explorer_blocks():
    page   = max(1, request.args.get("page",1,type=int))
    limit  = min(50, request.args.get("limit",20,type=int))
    offset = (page-1)*limit
    db     = get_db()
    rows   = db.execute("""
        SELECT index_,previous_hash,difficulty,reward,solved_by,
               solved_at,timestamp,(solved_at-timestamp) AS solve_time
        FROM blocks WHERE solved_by IS NOT NULL
        ORDER BY index_ DESC LIMIT ? OFFSET ?
    """, (limit,offset)).fetchall()
    total  = db.execute(
        "SELECT COUNT(*) FROM blocks WHERE solved_by IS NOT NULL"
    ).fetchone()[0]
    return jsonify({"status":"ok","total":total,"page":page,
                    "blocks":[dict(r) for r in rows]})

@app.route("/explorer/block/<int:index>")
def explorer_block_detail(index):
    db    = get_db()
    block = db.execute("SELECT * FROM blocks WHERE index_=?", (index,)).fetchone()
    if not block:
        return jsonify({"status":"error","message":"No encontrado"}), 404
    shares = db.execute(
        "SELECT username,hash,submitted_at FROM shares"
        " WHERE block_index=? ORDER BY submitted_at ASC", (index,)
    ).fetchall()
    return jsonify({"status":"ok","block":dict(block),
                    "shares":[dict(s) for s in shares]})

@app.route("/explorer/address/<string:username>")
def explorer_address(username):
    db = get_db()
    if not db.execute(
        "SELECT 1 FROM miners WHERE username=?", (username,)
    ).fetchone():
        return jsonify({"status":"error","message":"No encontrado"}), 404
    bal    = db.execute(
        "SELECT balance,locked FROM balances WHERE username=?", (username,)
    ).fetchone()
    recent = db.execute("""
        SELECT block_index,hash,reward,submitted_at FROM shares
        WHERE username=? ORDER BY submitted_at DESC LIMIT 30
    """, (username,)).fetchall()
    total_earned = db.execute(
        "SELECT COALESCE(balance,0) FROM balances WHERE username=?", (username,)
    ).fetchone()
    total_earned = total_earned[0] if total_earned else 0.0
    blocks_found = db.execute(
        "SELECT COUNT(*) FROM blocks WHERE solved_by=?", (username,)
    ).fetchone()[0]
    return jsonify({
        "status":"ok","username":username,
        "balance":       round(bal["balance"] if bal else 0, 5),
        "locked":        bool(bal["locked"]) if bal else False,
        "total_earned":  round(total_earned, 5),
        "blocks_found":  blocks_found,
        "recent_shares": [dict(r) for r in recent]
    })

@app.route("/explorer/search")
def explorer_search():
    q  = request.args.get("q","").strip()
    if not q: return jsonify({"status":"error","message":"Falta q"}), 400
    db = get_db()
    if q.isdigit():
        b = db.execute("SELECT * FROM blocks WHERE index_=?", (int(q),)).fetchone()
        if b: return jsonify({"type":"block","data":dict(b)})
    if len(q)==64 and all(c in "0123456789abcdef" for c in q.lower()):
        s = db.execute("SELECT * FROM shares WHERE hash=?", (q,)).fetchone()
        if s: return jsonify({"type":"tx","data":dict(s)})
        b = db.execute(
            "SELECT * FROM blocks WHERE previous_hash=?", (q,)
        ).fetchone()
        if b: return jsonify({"type":"block","data":dict(b)})
    m = db.execute("SELECT username FROM miners WHERE username=?", (q,)).fetchone()
    if m: return jsonify({"type":"address","username":m["username"]})
    return jsonify({"status":"error","message":"Sin resultados"}), 404

@app.route("/explorer/search/date")
def explorer_search_date():
    date_from = request.args.get("from", "")
    date_to   = request.args.get("to", "")
    if not date_from:
        return jsonify({"status":"error","message":"Falta fecha de inicio"}), 400
    try:
        ts_from = int(datetime.strptime(date_from, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())
        if date_to:
            ts_to = int(datetime.strptime(date_to, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())
        else:
            ts_to = ts_from + 86400
    except ValueError:
        return jsonify({"status":"error","message":"Formato de fecha invalido"}), 400
    db = get_db()
    rows = db.execute("""
        SELECT index_, solved_by, reward, solved_at FROM blocks
        WHERE solved_by IS NOT NULL AND solved_at BETWEEN ? AND ?
        ORDER BY index_ ASC LIMIT 200
    """, (ts_from, ts_to)).fetchall()
    blocks = [dict(r) for r in rows]
    return jsonify({"status":"ok","count":len(blocks),"blocks":blocks})

# ═══════════════════════════════════════════════════════
#  PAGINAS HTML
# ═══════════════════════════════════════════════════════
@app.route("/")
@app.route("/dashboard")
def dashboard():        return send_file("dashboard.html")
@app.route("/explorer")
def explorer_page():    return send_file("explorer.html")
@app.route("/withdrawals")
def withdrawals_page(): return send_file("withdrawals.html")
@app.route("/static/<path:filename>")
def static_files(filename):
    safe = os.path.basename(filename)
    path = os.path.join("static", safe)
    if not os.path.exists(path): abort(404)
    return send_file(path)

# ═══════════════════════════════════════════════════════
#  ERROR HANDLERS
# ═══════════════════════════════════════════════════════
@app.errorhandler(400)
def bad_request(e):
    return jsonify({"status":"error","message":str(e)}), 400
@app.errorhandler(401)
def unauthorized(e):
    return jsonify({"status":"error","message":"API key requerida"}), 401
@app.errorhandler(403)
def forbidden(e):
    return jsonify({"status":"error","message":str(e)}), 403
@app.errorhandler(404)
def not_found(e):
    return jsonify({"status":"error","message":"No encontrado"}), 404
@app.errorhandler(429)
def rate_limited(e):
    return jsonify({"status":"error","message":"Rate limit excedido"}), 429
@app.errorhandler(500)
def server_error(e):
    log.exception("Error interno")
    return jsonify({"status":"error","message":"Error interno"}), 500

# ═══════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════
if __name__ == "__main__":
    print_tokenomics()
    init_db()
    log.info("NKL Pool v2.2 -> http://0.0.0.0:5000/dashboard")
    log.info("Auto-withdrawal: %s",
             "ACTIVO" if AUTO_WITHDRAWAL_ENABLED else "MANUAL")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
# ═══════════════════════════════════════════════════════
#  CERT ANCHOR — Anclaje blockchain para Núcleo CERT
# ═══════════════════════════════════════════════════════
@app.route("/api/cert-anchor", methods=["POST"])
@require_founder
def cert_anchor():
    """
    Ancla el hash SHA-256 de un documento CERT en la blockchain NKL.
    Llamado internamente por Núcleo CERT al confirmar un trámite.
    No modifica bloques ni minería — registra en tabla cert_anchors.
    Compatible con bridge BEP-20 futuro (tabla independiente).
    """
    data = request.get_json(silent=True) or {}
    hash_doc    = data.get("hash", "").strip().lower()
    numero_cert = data.get("numero_cert", "").strip()

    if not hash_doc or len(hash_doc) != 64:
        return jsonify({"ok": False, "error": "hash inválido"}), 400
    if not numero_cert:
        return jsonify({"ok": False, "error": "numero_cert requerido"}), 400

    conn = get_db()
    try:
        # Obtener bloque actual
        row = conn.execute("SELECT MAX(index_) FROM blocks").fetchone()
        block_index = row[0] if row and row[0] is not None else 0

        # Generar tx_id determinístico
        import hashlib, time
        ts = int(time.time())
        raw = f"{hash_doc}:{block_index}:{ts}:{numero_cert}"
        tx_id = "NKL" + hashlib.sha256(raw.encode()).hexdigest()[:40]

        conn.execute(
            "INSERT INTO cert_anchors (tx_id, numero_cert, hash_doc, block_index, timestamp) VALUES (?,?,?,?,?)",
            (tx_id, numero_cert, hash_doc, block_index, ts)
        )
        conn.commit()
        log.info("CERT anchor: %s → bloque %d tx %s", numero_cert, block_index, tx_id)
        return jsonify({"ok": True, "tx_id": tx_id, "block_index": block_index})

    except Exception as e:
        log.exception("Error cert-anchor")
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/cert-anchor-v2", methods=["POST"])
def cert_anchor_v2():
    """Version sin require_founder - valida key directo contra DB"""
    api_key = request.headers.get("X-API-Key", "")
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT username FROM miners WHERE api_key=? AND is_founder=1",
            (api_key,)
        ).fetchone()
        if not row:
            return jsonify({"ok": False, "error": "No autorizado"}), 403

        data = request.get_json(silent=True) or {}
        hash_doc    = data.get("hash", "").strip().lower()
        numero_cert = data.get("numero_cert", "").strip()

        if not hash_doc or len(hash_doc) != 64:
            return jsonify({"ok": False, "error": "hash inválido"}), 400
        if not numero_cert:
            return jsonify({"ok": False, "error": "numero_cert requerido"}), 400

        blk = conn.execute("SELECT MAX(index_) FROM blocks").fetchone()
        block_index = blk[0] if blk and blk[0] is not None else 0

        import hashlib, time
        ts = int(time.time())
        raw = f"{hash_doc}:{block_index}:{ts}:{numero_cert}"
        tx_id = "NKL" + hashlib.sha256(raw.encode()).hexdigest()[:40]

        conn.execute(
            "INSERT OR IGNORE INTO cert_anchors (tx_id, numero_cert, hash_doc, block_index, timestamp) VALUES (?,?,?,?,?)",
            (tx_id, numero_cert, hash_doc, block_index, ts)
        )
        conn.commit()
        return jsonify({"ok": True, "tx_id": tx_id, "block_index": block_index})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/cert-anchor/<tx_id>", methods=["GET"])
def cert_anchor_get(tx_id):
    """Consulta pública de anclaje CERT — sin autenticación, solo lectura"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT tx_id, numero_cert, hash_doc, block_index, timestamp FROM cert_anchors WHERE tx_id=?",
            (tx_id,)
        ).fetchone()
        if not row:
            return jsonify({"ok": False, "error": "TX no encontrada"}), 404
        return jsonify({
            "ok": True,
            "tx_id": row["tx_id"],
            "numero_cert": row["numero_cert"],
            "hash_documento": row["hash_doc"],
            "block_index": row["block_index"],
            "timestamp": row["timestamp"],
            "explorer": f"https://explorer.nucleonkl.com/block/{row['block_index']}"
        })
    finally:
        conn.close()

