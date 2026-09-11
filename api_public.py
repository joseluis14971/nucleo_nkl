# API pública para MiningPoolStats / MiningBoard / agregadores (v2, 10/09/2026)
# Se registra desde server.py con:  from api_public import register; register(app, DB_PATH)
#
# Convención NKL-Argon (ver _difficulty_to_target en server.py):
#   expected_hashes_per_block = 16 ** difficulty
#   network_hashrate = 16**D / avg_block_time_s   (D y tiempos tomados de los últimos bloques reales)
#   pool_hashrate    = network_hashrate * (bloques del pool / bloques de red en 24h)
# Precio: solo de mercado real. Hasta que NKL cotice en un exchange/DEX, todos los campos van en null.

import sqlite3
import time
from flask import jsonify

BLOCK_TIME_TARGET = 540      # segundos
HASHRATE_UNIT = "H/s"
BLOCK_WINDOW = 50            # bloques usados para promediar tiempo y dificultad
ACTIVE_WINDOW = 7200         # ventana para "minero activo" (2 h)
SHARE_HASHES = 16            # una share = hash con 1 cero hex = ~16 hashes (PARTIAL_DIFFICULTY=1)

COIN_INFO = {
    "coin": "Núcleo NKL",
    "ticker": "NKL",
    "algorithm": "NKL-Argon",
    "algorithm_detail": "Argon2 memory-hard hash + SHA-256 block verification; CPU-only",
    "block_time_target_s": BLOCK_TIME_TARGET,
    "hashrate_unit": HASHRATE_UNIT,
    "difficulty_formula": "expected_hashes_per_block = 16 ** difficulty (difficulty is a float; D=N means N leading hex zeros)",
    "network_hashrate_formula": "16 ** avg_difficulty / avg_block_time_s over last %d blocks" % BLOCK_WINDOW,
    "pool_hashrate_formula": "network_hashrate * (pool_blocks_24h / network_blocks_24h)",
    "website": "https://nucleonkl.com",
    "explorer_url": "https://explorer.nucleonkl.com",
    "pool_url": "https://pool.nucleonkl.com",
    "source": "https://github.com/joseluis14971/nucleo_nkl",
}

PRICE_NULL = {
    "price_usd": None,
    "price_source": None,
    "volume_24h_usd": None,
    "last_trade_at": None,
    "circulating_supply": None,
    "price_note": "NKL is not traded on any exchange/DEX yet; price fields stay null until a real market exists.",
}


def register(app, db_path):

    def conn_():
        c = sqlite3.connect(db_path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def network_stats(conn):
        """Hashrate de red desde bloques reales."""
        rows = conn.execute(
            "SELECT index_, difficulty, COALESCE(solved_at, timestamp) AS t "
            "FROM blocks WHERE solved_by IS NOT NULL ORDER BY index_ DESC LIMIT ?",
            (BLOCK_WINDOW,)).fetchall()
        if len(rows) < 2:
            d = rows[0]["difficulty"] if rows else 1.0
            return {"difficulty": d, "avg_difficulty": d, "avg_block_time_s": None,
                    "network_hashrate": None, "expected_hashes_per_block": 16 ** float(d),
                    "blocks_sampled": len(rows)}
        newest, oldest = rows[0], rows[-1]
        span = max(1, newest["t"] - oldest["t"])
        avg_bt = span / (len(rows) - 1)
        avg_d = sum(float(r["difficulty"]) for r in rows) / len(rows)
        expected = 16 ** avg_d
        return {
            "difficulty": float(newest["difficulty"]),
            "avg_difficulty": round(avg_d, 4),
            "avg_block_time_s": round(avg_bt, 1),
            "expected_hashes_per_block": round(expected, 2),
            "network_hashrate": round(expected / avg_bt, 2),
            "blocks_sampled": len(rows),
        }

    def block_counts_24h(conn, now):
        # Todos los bloques de la cadena pasan por esta DB; los del pool son los que tienen solved_by.
        net = conn.execute("SELECT COUNT(*) AS n FROM blocks WHERE COALESCE(solved_at, timestamp) >= ?",
                           (now - 86400,)).fetchone()["n"]
        pool = conn.execute("SELECT COUNT(*) AS n FROM blocks WHERE solved_by IS NOT NULL "
                            "AND COALESCE(solved_at, timestamp) >= ?", (now - 86400,)).fetchone()["n"]
        return net, pool

    @app.route("/api/stats")
    def api_stats():
        conn = conn_()
        now = int(time.time())
        net = network_stats(conn)
        net_24h, pool_24h = block_counts_24h(conn, now)
        pool_share = (pool_24h / net_24h) if net_24h else 0.0
        pool_hr = round(net["network_hashrate"] * pool_share, 2) if net["network_hashrate"] is not None else None
        def _active(win):
            return conn.execute(
                "SELECT COUNT(DISTINCT username) AS n FROM shares WHERE submitted_at >= ?",
                (now - win,)).fetchone()["n"]
        m2h, m24h, m72h = _active(7200), _active(86400), _active(259200)
        registered = conn.execute(
            "SELECT COUNT(*) AS n FROM miners WHERE is_system=0 AND banned=0").fetchone()["n"]
        last = conn.execute(
            "SELECT index_, COALESCE(solved_at, timestamp) AS t, reward, difficulty FROM blocks "
            "ORDER BY index_ DESC LIMIT 1").fetchone()
        conn.close()
        out = dict(COIN_INFO)
        out.update({
            "pool": "Nucleo NKL Pool",
            "payout": "PPLNS",
            "fee_percent": 0,
            # --- pool ---
            "pool_hashrate": pool_hr,
            "pool_hashrate_unit": HASHRATE_UNIT,
            "pool_blocks_24h": pool_24h,
            "pool_share_of_network_24h": round(pool_share, 4),
            "miners": m24h,
            "workers": m24h,
            "miners_window": "24h",
            "miners_2h": m2h,
            "miners_24h": m24h,
            "miners_72h": m72h,
            "registered_miners": registered,
            # --- network ---
            "network_hashrate": net["network_hashrate"],
            "network_hashrate_unit": HASHRATE_UNIT,
            "network_difficulty": net["difficulty"],
            "network_avg_difficulty": net["avg_difficulty"],
            "network_avg_block_time_s": net["avg_block_time_s"],
            "network_expected_hashes_per_block": net["expected_hashes_per_block"],
            "network_blocks_24h": net_24h,
            "network_blocks_sampled": net["blocks_sampled"],
            "height": last["index_"] if last else 0,
            "last_block_time": last["t"] if last else 0,
            "last_block_reward": last["reward"] if last else 0,
            "timestamp": now,
        })
        out.update(PRICE_NULL)
        return jsonify(out)

    @app.route("/api/network")
    def api_network():
        conn = conn_()
        now = int(time.time())
        net = network_stats(conn)
        net_24h, _ = block_counts_24h(conn, now)
        last = conn.execute(
            "SELECT index_, COALESCE(solved_at, timestamp) AS t, reward FROM blocks "
            "ORDER BY index_ DESC LIMIT 1").fetchone()
        conn.close()
        out = dict(COIN_INFO)
        out.update({
            "height": last["index_"] if last else 0,
            "last_block_time": last["t"] if last else 0,
            "last_block_reward": last["reward"] if last else 0,
            "difficulty": net["difficulty"],
            "avg_difficulty": net["avg_difficulty"],
            "avg_block_time_s": net["avg_block_time_s"],
            "expected_hashes_per_block": net["expected_hashes_per_block"],
            "network_hashrate": net["network_hashrate"],
            "network_hashrate_unit": HASHRATE_UNIT,
            "blocks_24h": net_24h,
            "blocks_sampled": net["blocks_sampled"],
            "timestamp": now,
        })
        out.update(PRICE_NULL)
        return jsonify(out)

    @app.route("/api/blocks")
    def api_blocks():
        conn = conn_()
        rows = conn.execute(
            "SELECT index_, previous_hash, timestamp, solved_at, difficulty, reward, solved_by "
            "FROM blocks WHERE solved_by IS NOT NULL ORDER BY index_ DESC LIMIT 50").fetchall()
        conn.close()
        return jsonify([{
            "height": r["index_"],
            "previous_hash": r["previous_hash"],
            "timestamp": r["solved_at"] or r["timestamp"],
            "difficulty": float(r["difficulty"]),
            "expected_hashes": round(16 ** float(r["difficulty"]), 2),
            "reward": r["reward"],
            "miner": r["solved_by"],
        } for r in rows])

    @app.route("/api/miners")
    def api_miners():
        """Hashrate por minero = hashrate del pool repartido por proporción de shares (10 min)."""
        conn = conn_()
        now = int(time.time())
        net = network_stats(conn)
        net_24h, pool_24h = block_counts_24h(conn, now)
        pool_hr = (net["network_hashrate"] or 0.0) * ((pool_24h / net_24h) if net_24h else 0.0)
        rows = conn.execute(
            "SELECT username, COUNT(*) AS n FROM shares WHERE submitted_at >= ? "
            "GROUP BY username ORDER BY n DESC", (now - ACTIVE_WINDOW,)).fetchall()
        conn.close()
        total = sum(r["n"] for r in rows) or 1
        return jsonify({
            "hashrate_unit": HASHRATE_UNIT,
            "pool_hashrate": round(pool_hr, 2),
            "window_s": ACTIVE_WINDOW,
            "miners": [{
                "miner": r["username"],
                "share_fraction": round(r["n"] / total, 4),
                "hashrate": round(pool_hr * r["n"] / total, 2),
                "shares_window": r["n"],
            } for r in rows],
        })
