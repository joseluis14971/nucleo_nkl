# -*- coding: utf-8 -*-
"""
nkl_constants.py - Parametros de la red Nucleo NKL v2.2
Fuente unica de verdad. Importar en server.py y miner.py.
"""

# ═══════════════════════════════════════════════
#  EMISION
# ═══════════════════════════════════════════════
TOTAL_SUPPLY        = 100_000_000_000
FOUNDER_RESERVE_PCT = 0.30
FOUNDER_RESERVE     = int(TOTAL_SUPPLY * FOUNDER_RESERVE_PCT)  # 30,000,000,000
MINEABLE_SUPPLY     = TOTAL_SUPPLY - FOUNDER_RESERVE           # 70,000,000,000

# ═══════════════════════════════════════════════
#  CUENTAS DEL SISTEMA — fijas, no cambiar
# ═══════════════════════════════════════════════
FOUNDER_USER_DEFAULT = "creador"     # cuenta operativa del fundador
PREMINE_ACCOUNT      = "nkl_premine" # 30B NKL bloqueados — solo lectura publica
FEE_ACCOUNT          = "nkl_fees"    # recibe 0.5% de cada retiro

# ═══════════════════════════════════════════════
#  COMISION DE RED
# ═══════════════════════════════════════════════
NETWORK_FEE_PCT = 0.005   # 0.5% por retiro -> FEE_ACCOUNT

# ═══════════════════════════════════════════════
#  BLOQUES — 1 bloque cada 9 minutos
# ═══════════════════════════════════════════════
BLOCK_TIME_MINUTES = 9
BLOCK_TIME_SECONDS = BLOCK_TIME_MINUTES * 60   # 540 s
MINING_YEARS       = 30
BLOCKS_PER_YEAR    = int(365.25 * 24 * 60 / BLOCK_TIME_MINUTES)  # 58,440
TOTAL_BLOCKS       = BLOCKS_PER_YEAR * MINING_YEARS               # 1,753,200

# ═══════════════════════════════════════════════
#  HALVING — cada 3 anos, reduccion 30%
#  10 halvings en 30 anos
# ═══════════════════════════════════════════════
HALVING_INTERVAL  = BLOCKS_PER_YEAR * 3   # 175,320 bloques = 3 anos
HALVING_REDUCTION = 0.70                  # cada era = 70% de la anterior
N_HALVINGS        = 10

_serie = sum(HALVING_REDUCTION ** i for i in range(N_HALVINGS))
INITIAL_BLOCK_REWARD = round(MINEABLE_SUPPLY / (HALVING_INTERVAL * _serie), 4)
# = 123,262.84 NKL/bloque en era 0

def get_block_reward(block_index: int) -> float:
    era = block_index // HALVING_INTERVAL
    if era >= N_HALVINGS:
        return 0.0
    return round(INITIAL_BLOCK_REWARD * (HALVING_REDUCTION ** era), 4)

# ═══════════════════════════════════════════════
#  ALGORITMO ANTI-ASIC — NKL-Argon
#  CRITICO: IDENTICO en server.py y miner.py
#  64KB/2iter = ~5 H/s en CPU, resistente a ASIC
# ═══════════════════════════════════════════════
MEMORY_COST_KB   = 64
ARGON_ITERATIONS = 2

# ═══════════════════════════════════════════════
#  DIFICULTAD
# ═══════════════════════════════════════════════
INITIAL_DIFFICULTY = 2     # inicio bajo, se ajusta automaticamente
DIFFICULTY_WINDOW = 25  # recalcular cada 50 bloques
MIN_DIFFICULTY     = 1
MAX_DIFFICULTY     = 24

# ═══════════════════════════════════════════════
#  RED
# ═══════════════════════════════════════════════
COIN_SYMBOL       = "NKL"
COIN_NAME         = "Nucleo"
POOL_DEFAULT_PORT = 5000

def print_tokenomics():
    print("=" * 64)
    print(f"  {COIN_NAME} ({COIN_SYMBOL}) - Tokenomics v2.2")
    print("=" * 64)
    print(f"  Emision total          : {TOTAL_SUPPLY:,} NKL")
    print(f"  Premine creador 30%    : {FOUNDER_RESERVE:,} NKL  [{PREMINE_ACCOUNT}]")
    print(f"  Para mineros 70%       : {MINEABLE_SUPPLY:,} NKL")
    print(f"  Comision de red        : {NETWORK_FEE_PCT*100:.1f}% -> [{FEE_ACCOUNT}]")
    print(f"  Tiempo por bloque      : {BLOCK_TIME_MINUTES} min")
    print(f"  Halving cada           : {HALVING_INTERVAL:,} bloques (3 anos) -30%")
    print(f"  Total halvings         : {N_HALVINGS} en 30 anos")
    print(f"  Algoritmo anti-ASIC    : NKL-Argon {MEMORY_COST_KB}KB/{ARGON_ITERATIONS}iter")
    print(f"  Dificultad inicial     : {INITIAL_DIFFICULTY} (ajuste cada {DIFFICULTY_WINDOW} bloques)")
    print()
    for i in range(N_HALVINGS):
        r  = get_block_reward(i * HALVING_INTERVAL)
        yr = i * 3
        print(f"  Era {i:>2} (ano {yr:>2})  ->  {r:>14,.4f} NKL/bloque")
    print("=" * 64)

if __name__ == "__main__":
    print_tokenomics()