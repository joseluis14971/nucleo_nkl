# -*- coding: utf-8 -*-
"""
Nucleo NKL - Minero CPU v2.7
nucleonkl.com

Cambios v2.7:
  - VARDIFF: el pool asigna a cada minero su dificultad de share (share_difficulty)
    y la ajusta sola para que mande ~1 share cada 30 s. Sin tope de shares.
  - Cada share pesa 16^share_difficulty en el reparto PPLNS (mismo cobro, menos trafico)
  - El miner manda share_difficulty en cada share; el pool verifica el hash completo

Cambios v2.6:
  - FIX CRITICO: mine_chunk ahora usa target numerico igual que el server
    (resuelve TypeError: can't multiply sequence by non-int of type 'float')
  - La validacion del hash es identica al server: hash_int <= target
  - Shares parciales tambien usan target numerico (PARTIAL_DIFFICULTY=1)
  - Dificultad flotante soportada completamente (ej: 4.07, 4.5, etc.)
  - Log de bloque muestra dificultad con 2 decimales

Cambios v2.4 (base):
  - Usa nonce_start/nonce_end del server para evitar colisiones entre mineros
  - Al recibir "bloque ya resuelto" pide nuevo job inmediatamente
  - POOL_URL apunta al pool oficial

Cambios v2.3:
  - WALLET_FILE y miner.log siempre se guardan junto al .exe
  - Deteccion automatica si el usuario corre el .exe desde adentro del ZIP
"""

import requests, time, hashlib, os, json, sys, random
import struct, threading, queue, argparse, logging, math

# ═══════════════════════════════════════════════════════
#  RUTA BASE — siempre junto al .exe o al .py
# ═══════════════════════════════════════════════════════
if getattr(sys, 'frozen', False):
    BASE_PATH = os.path.dirname(sys.executable)
else:
    BASE_PATH = os.path.dirname(os.path.abspath(__file__))

# ═══════════════════════════════════════════════════════
#  DETECCION DE ZIP — avisa si corre desde ruta temporal
# ═══════════════════════════════════════════════════════
_rutas_temp = ["Temp", "temp", "\\tmp", "AppData\\Local\\Temp", "AppData\\Roaming\\WinRAR"]
if any(x in BASE_PATH for x in _rutas_temp):
    print()
    print("  +==================================================+")
    print("  |  ERROR: EXTRAÉ EL ZIP PRIMERO                   |")
    print("  |                                                  |")
    print("  |  Estas ejecutando el miner desde adentro        |")
    print("  |  del ZIP. Tu wallet NO se va a guardar.         |")
    print("  |                                                  |")
    print("  |  SOLUCION:                                       |")
    print("  |  1. Cerra este programa                         |")
    print("  |  2. Clic derecho en NucleoNKL_Miner.zip         |")
    print("  |  3. Elegir: Extraer en NucleoNKL_Miner\\        |")
    print("  |  4. Abri la carpeta y ejecuta el .exe           |")
    print("  |                                                  |")
    print("  |  Soporte: t.me/nucleonkl                        |")
    print("  +==================================================+")
    print()
    input("  Presiona Enter para salir...")
    sys.exit(1)

# ═══════════════════════════════════════════════════════
#  CONFIGURACION
# ═══════════════════════════════════════════════════════
POOL_URL    = "http://173.212.228.11:5000"
WALLET_FILE = os.path.join(BASE_PATH, "mi_wallet_nkl.json")
LOG_FILE    = os.path.join(BASE_PATH, "miner.log")

# ═══════════════════════════════════════════════════════
#  ALGORITMO — IDENTICO al servidor
# ═══════════════════════════════════════════════════════
MEMORY_COST_KB   = 64
ARGON_ITERATIONS = 2
_BLOCK_SIZE      = 64
_NUM_BLOCKS      = (MEMORY_COST_KB * 1024) // _BLOCK_SIZE

STATS_INTERVAL = 15
JOB_REFRESH    = 3

# ═══════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8")
    ]
)
log = logging.getLogger("nkl")

# ═══════════════════════════════════════════════════════
#  NKL-ARGON — IDENTICO al server.py
# ═══════════════════════════════════════════════════════
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
            j = struct.unpack("<I", memory[i*_BLOCK_SIZE:i*_BLOCK_SIZE+4])[0] % _NUM_BLOCKS
            for b in range(_BLOCK_SIZE):
                memory[i*_BLOCK_SIZE+b] ^= memory[j*_BLOCK_SIZE+b]
            nb = hashlib.sha256(memory[i*_BLOCK_SIZE:i*_BLOCK_SIZE+_BLOCK_SIZE]).digest()*2
            memory[i*_BLOCK_SIZE:i*_BLOCK_SIZE+_BLOCK_SIZE] = nb
    return hashlib.sha256(memory).hexdigest()

# ═══════════════════════════════════════════════════════
#  CONVERSION DIFICULTAD -> TARGET — IDENTICO al server.py
#  Soporta dificultad flotante (ej: 4.07, 4.5, etc.)
# ═══════════════════════════════════════════════════════
def difficulty_to_target(difficulty):
    """
    Convierte dificultad flotante a target numerico.
    Identico a _difficulty_to_target() del server.
    target = 2^(256 - 4*D)
    Un hash es valido si hash_int <= target.
    """
    exp = 256.0 - 4.0 * float(difficulty)
    if exp <= 0:
        return 0
    exp_int  = int(exp)
    exp_frac = exp - exp_int
    target   = (1 << exp_int)
    if exp_frac > 0:
        target = int(target * (2 ** exp_frac))
    return target - 1

# ═══════════════════════════════════════════════════════
#  STATS
# ═══════════════════════════════════════════════════════
class MinerStats:
    def __init__(self):
        self.hashes=0; self.shares=0; self.rejected=0; self.parciales=0
        self.start=time.time(); self._lock=threading.Lock()

    def add_hashes(self, n):
        with self._lock: self.hashes += n

    def add_share(self, ok):
        with self._lock:
            if ok: self.shares += 1
            else:  self.rejected += 1

    def add_partial(self):
        with self._lock: self.parciales += 1

    def hashrate_str(self):
        hr = self.hashes / max(time.time()-self.start, 1)
        if hr >= 1e6: return f"{hr/1e6:.3f} MH/s"
        if hr >= 1e3: return f"{hr/1e3:.2f} KH/s"
        return f"{hr:.1f} H/s"

    def print_stats(self):
        log.info("Hashrate: %s | Bloques: %d | Parciales: %d | Rechazados: %d",
                 self.hashrate_str(), self.shares, self.parciales, self.rejected)

# ═══════════════════════════════════════════════════════
#  WALLET
# ═══════════════════════════════════════════════════════
def load_wallet(user_arg, key_arg):
    if user_arg and key_arg:
        log.info("Credenciales cargadas: %s", user_arg)
        return {"username": user_arg, "api_key": key_arg}

    if os.path.exists(WALLET_FILE):
        try:
            with open(WALLET_FILE, encoding="utf-8") as f:
                cfg = json.load(f)
            if cfg.get("username") and cfg.get("api_key"):
                log.info("Wallet cargada: %s", cfg["username"])
                return cfg
        except Exception:
            pass
        log.warning("Wallet corrupta, re-configurando...")

    return _setup_wizard()

def _setup_wizard():
    print()
    print("  +==================================================+")
    print("  |   NUCLEO NKL - Minero CPU v2.7                  |")
    print("  |   nucleonkl.com                                 |")
    print("  +==================================================+")
    print()
    print("  [1] Soy nuevo - crear mi wallet NKL")
    print("  [2] Ya tengo cuenta - recuperar mi wallet")
    print()
    while True:
        op = input("  Opcion (1 o 2): ").strip()
        if op in ("1","2"): break
        print("  Ingresa 1 o 2")
    return _new_wallet() if op=="1" else _recover_wallet()

def _new_wallet():
    print()
    while True:
        username = input("  -> Tu usuario (letras/numeros, 3-32 chars): ").strip()
        if not username: continue
        if not username.isalnum():
            print("  Solo letras y numeros, sin espacios.")
            continue
        if not 3 <= len(username) <= 32:
            print("  Entre 3 y 32 caracteres.")
            continue
        break
    return _register(username)

def _recover_wallet():
    print()
    print("  Ingresa tus credenciales anteriores.")
    print("  La API key estaba en el archivo mi_wallet_nkl.json")
    print("  Tambien podes verla en: nucleonkl.com/dashboard")
    print()
    while True:
        username = input("  -> Tu usuario NKL: ").strip()
        if username and username.isalnum() and 3<=len(username)<=32: break
        print("  Usuario invalido.")
    while True:
        api_key = input("  -> Tu API key: ").strip()
        if len(api_key) >= 20: break
        print("  API key muy corta.")
    print()
    print(f"  Verificando en {POOL_URL}...")
    try:
        r = requests.get(
            f"{POOL_URL}/stats/me",
            headers={"X-API-Key": api_key},
            timeout=10
        )
        data = r.json()
        if data.get("status")=="ok" and data.get("username")==username:
            cfg = {"username": username, "api_key": api_key}
            with open(WALLET_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            print()
            print("  +==================================================+")
            print(f"  | Wallet recuperada: {username:<30}|")
            print(f"  | Balance: {data.get('balance',0):<40}|")
            print("  +==================================================+")
            print()
            return cfg
        else:
            print("  Usuario o API key incorrectos.")
            print()
            return _recover_wallet()
    except requests.exceptions.ConnectionError:
        print(f"  No se pudo conectar a {POOL_URL}")
        print("  Verifica tu conexion a internet.")
        sys.exit(1)
    except Exception as e:
        print(f"  Error: {e}")
        sys.exit(1)

def _register(username):
    print(f"\n  Registrando '{username}' en el pool...")
    for attempt in range(1, 6):
        try:
            r    = requests.post(
                f"{POOL_URL}/register",
                json={"username": username},
                timeout=10
            )
            data = r.json()
            if data.get("status") == "ok":
                cfg = {"username": data["username"], "api_key": data["api_key"]}
                with open(WALLET_FILE, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2)
                print()
                print("  +==================================================+")
                print(f"  | Wallet creada: {cfg['username']:<35}|")
                print(f"  | API Key: {cfg['api_key'][:28]}...  |")
                print("  |                                                  |")
                print("  | GUARDA el archivo mi_wallet_nkl.json            |")
                print("  |    Es tu unico acceso a tus NKL minados          |")
                print(f"  |    Ubicacion: {WALLET_FILE[:35]:<35}|")
                print("  |                                                  |")
                print("  | Para minar desde otra PC:                        |")
                print(f"  | miner --user {cfg['username']:<20} --key <key>|")
                print("  +==================================================+")
                print()
                return cfg
            elif data.get("message","").lower().find("existe") >= 0:
                print(f"\n  El usuario '{username}' ya existe.")
                print("  Usa la opcion 2 para recuperar tu wallet.")
                print()
                username = input("  -> Nuevo usuario: ").strip()
                if not username.isalnum() or not 3<=len(username)<=32:
                    username = "Miner" + str(int(time.time()))[-4:]
            else:
                print(f"  Error: {data.get('message','?')}")
                sys.exit(1)
        except requests.exceptions.ConnectionError:
            wait = attempt * 5
            print(f"  Sin conexion con {POOL_URL}")
            if attempt < 5:
                print(f"  Reintentando en {wait}s... ({attempt}/5)")
                time.sleep(wait)
            else:
                print("  Verifica tu conexion a internet.")
                sys.exit(1)
        except Exception as e:
            print(f"  Error: {e}")
            sys.exit(1)

# ═══════════════════════════════════════════════════════
#  POOL CLIENT
# ═══════════════════════════════════════════════════════
class PoolClient:
    def __init__(self, username, api_key):
        self.username = username
        self.headers  = {"X-API-Key": api_key, "Content-Type": "application/json"}

    def get_job(self):
        try:
            r = requests.get(f"{POOL_URL}/get_job",
                            headers=self.headers, timeout=10,
                            allow_redirects=True)
            if r.status_code == 200:
                return r.json().get("job")
            elif r.status_code == 401:
                log.error("API key invalida. Borra %s y reinicia.", WALLET_FILE)
                sys.exit(1)
            elif r.status_code == 403:
                log.error("Cuenta suspendida.")
                sys.exit(1)
            elif r.status_code == 503:
                log.warning("Pool sin bloques disponibles...")
        except requests.exceptions.ConnectionError:
            log.warning("Sin conexion con %s", POOL_URL)
        except Exception as e:
            log.warning("get_job error: %s", e)
        return None

    def submit_partial(self, nonce, block_index, hash_result, share_difficulty=1.0):
        """Manda un share parcial al pool (v2.7: incluye share_difficulty)."""
        try:
            r = requests.post(
                f"{POOL_URL}/submit_share",
                json={"nonce": nonce, "block_index": block_index,
                      "hash": hash_result, "share_difficulty": share_difficulty},
                headers=self.headers, timeout=5
            )
            return r.status_code == 200
        except Exception:
            return False

    def submit(self, nonce, block_index, found_hash, stats):
        try:
            r = requests.post(
                f"{POOL_URL}/submit_solution",
                json={"nonce": nonce, "block_index": block_index},
                headers=self.headers, timeout=15
            )
            data = r.json()
            ok   = data.get("status") == "ok"
            stats.add_share(ok)
            if ok:
                log.info("Share aceptado! +%.4f NKL | %s...",
                         data.get("reward",0), found_hash[:14])
            else:
                msg = data.get("message","?")
                log.warning("Share rechazado: %s", msg)
                if "ya resuelto" in msg or "invalido" in msg.lower():
                    return "new_job"
            return ok
        except Exception as e:
            log.warning("submit error: %s", e)
            stats.add_share(False)
            return False

# ═══════════════════════════════════════════════════════
#  MINING — CPU multihilo con target numerico
#  v2.6: usa difficulty_to_target() identico al server
#        soporta dificultad flotante sin errores
# ═══════════════════════════════════════════════════════
def mine_chunk(job, nonce_start, nonce_end, result_q, partial_q, stop):
    # ── CAMBIO PRINCIPAL v2.6 ──────────────────────────────────────
    # En lugar de "0" * difficulty (falla con float),
    # usamos target numerico identico al server.
    # Para shares parciales usamos PARTIAL_DIFFICULTY = 1
    # ──────────────────────────────────────────────────────────────
    difficulty     = job["difficulty"]          # puede ser float: 4.07
    target         = difficulty_to_target(difficulty)  # target numerico para solucion completa
    share_diff     = float(job.get("share_difficulty", 1.0))   # v2.7: asignada por el pool
    share_target   = difficulty_to_target(share_diff)

    for nonce in range(nonce_start, nonce_end):
        if stop.is_set(): return
        h        = nkl_hash(job["index"], job["previous_hash"], job["timestamp"], nonce)
        hash_int = int(h, 16)

        if hash_int <= target:
            # Solucion completa — target numerico igual que el server
            result_q.put((nonce, h))
            return
        elif hash_int <= share_target:
            # Share parcial — cumple la dificultad de share asignada (vardiff)
            partial_q.put((nonce, h))

# ═══════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════
def main():
    global POOL_URL
    _default_pool = POOL_URL

    parser = argparse.ArgumentParser(description="Nucleo NKL Miner v2.7")
    parser.add_argument("--pool",    default=_default_pool,
                        help="URL del pool")
    parser.add_argument("--user",    default="",
                        help="Usuario (para segunda PC con mismo wallet)")
    parser.add_argument("--key",     default="",
                        help="API key (para segunda PC con mismo wallet)")
    parser.add_argument("--threads", type=int,
                        default=max(1, os.cpu_count()-1),
                        help="Hilos CPU a usar")
    parser.add_argument("--chunk",   type=int, default=20,
                        help="Nonces por hilo por ronda")
    args = parser.parse_args()

    POOL_URL = args.pool

    cfg    = load_wallet(args.user, args.key)
    client = PoolClient(cfg["username"], cfg["api_key"])
    stats  = MinerStats()

    try:
        import requests as _req
        _r   = _req.get(f"{POOL_URL}/stats/pool", timeout=5, allow_redirects=True)
        _dif = _r.json().get("current_block", {}).get("difficulty", "?")
    except Exception:
        _dif = "?"

    print()
    print("  +==================================================+")
    print("  |   NUCLEO NKL - Minero CPU v2.7                  |")
    print(f"  |   Pool       : {POOL_URL:<34}|")
    print(f"  |   Algoritmo  : NKL-Argon {MEMORY_COST_KB}KB/{ARGON_ITERATIONS}iter anti-ASIC    |")
    print(f"  |   Hilos      : {str(args.threads):<34}|")
    print(f"  |   Dificultad : {str(_dif):<34}|")
    print("  +==================================================+")
    print()

    def stats_loop():
        while True:
            time.sleep(STATS_INTERVAL)
            stats.print_stats()
    threading.Thread(target=stats_loop, daemon=True).start()

    log.info("Minando como: %s | Pool: %s", cfg["username"], POOL_URL)

    current_job   = None
    last_job_time = 0
    nonce         = 0

    while True:
        try:
            now = time.time()

            if current_job is None or (now - last_job_time) >= JOB_REFRESH:
                new_job = client.get_job()
                if new_job:
                    if current_job is None or new_job["index"] != current_job["index"]:
                        current_job   = new_job
                        nonce         = random.randint(0, 2**31)
                        last_job_time = now
                        log.info("Bloque #%d | Dif: %.2f | ShareDif: %.2f | Reward: %.2f NKL",
                                 current_job["index"],
                                 float(current_job["difficulty"]),
                                 float(current_job.get("share_difficulty", 1.0)),
                                 current_job.get("reward", 0))
                    else:
                        # Mismo bloque: actualizar dificultad de share (vardiff) Y
                        # la dificultad de red del bloque (F6: ajuste de emergencia
                        # del server puede bajarla sin esperar un indice nuevo)
                        old_diff = float(current_job.get("difficulty", 0))
                        current_job["share_difficulty"] = new_job.get("share_difficulty", 1.0)
                        current_job["difficulty"]        = new_job.get("difficulty", current_job["difficulty"])
                        current_job["reward"]            = new_job.get("reward", current_job.get("reward", 0))
                        new_diff = float(current_job["difficulty"])
                        if abs(new_diff - old_diff) >= 0.01:
                            log.info("Dificultad de red actualizada: %.2f -> %.2f (bloque #%d)",
                                     old_diff, new_diff, current_job["index"])
                        last_job_time = now
                else:
                    time.sleep(5)
                    continue

            if current_job is None:
                time.sleep(2)
                continue

            result_q   = queue.Queue()
            partial_q  = queue.Queue()
            stop_event = threading.Event()
            threads    = []
            for t in range(args.threads):
                s  = nonce + t * args.chunk
                th = threading.Thread(
                    target=mine_chunk,
                    args=(current_job, s, s + args.chunk,
                          result_q, partial_q, stop_event),
                    daemon=True
                )
                th.start()
                threads.append(th)
            for th in threads:
                th.join()

            chunk_total = args.threads * args.chunk
            stats.add_hashes(chunk_total)
            nonce += chunk_total

            # Enviar shares parciales encontrados en este chunk
            while not partial_q.empty():
                p_nonce, p_hash = partial_q.get()
                if client.submit_partial(p_nonce, current_job["index"], p_hash,
                                         current_job.get("share_difficulty", 1.0)):
                    stats.add_partial()

            if not result_q.empty():
                found_nonce, found_hash = result_q.get()
                stop_event.set()
                log.info("Solucion encontrada! nonce=%d", found_nonce)
                result = client.submit(found_nonce, current_job["index"],
                                       found_hash, stats)
                current_job   = None
                last_job_time = 0
                nonce         = 0

            time.sleep(0.01)

        except KeyboardInterrupt:
            print()
            log.info("Minero detenido.")
            stats.print_stats()
            print()
            print(f"  Tu saldo NKL: {POOL_URL}/dashboard")
            print()
            sys.exit(0)
        except Exception as e:
            log.exception("Error: %s", e)
            time.sleep(3)

if __name__ == "__main__":
    main()
