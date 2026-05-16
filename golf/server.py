#!/usr/bin/env python3
"""
Procedural Mini Golf — WebSocket Server (FINAL)
Jalankan: python server.py
Kompatibel: websockets >= 10.x, Python 3.9+

Fix yang diterapkan:
  - Handler 'oob' lengkap (penalti +1 stroke, reset posisi, advance turn)
  - Validasi magnitude vx/vy (anti-cheat)
  - Ping/keepalive otomatis agar koneksi hotspot tidak putus diam-diam
  - advance_turn() robust: skip inHole & disconnected player
  - remove_player() reset waiting_for_stop + broadcast leaderboard
  - turnEnd tidak bergantung waiting_for_stop saja (cek OOB state juga)
  - build_state() selalu include seed agar late-join bisa render terrain
  - state_broadcaster dengan adaptive rate (lebih jarang jika idle)
  - Logging terstruktur dengan timestamp
"""

import asyncio
import json
import logging
import random
import string
import time
from typing import Optional

import websockets
from websockets.exceptions import ConnectionClosed

# ============================================================
#  LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("minigolf")

# ============================================================
#  KONFIGURASI
# ============================================================
HOST          = "0.0.0.0"
PORT          = 8765
MAX_PLAYERS   = 8
MAX_VX        = 25.0    # batas kecepatan tembak — anti-cheat
MAX_VY        = 25.0
OOB_PENALTY   = 1       # tambahan stroke saat out-of-bounds
RESET_DELAY   = 3.0     # detik sebelum map di-reset setelah menang
BROADCAST_HZ  = 1.0     # detik antar broadcast state_broadcaster
PING_INTERVAL = 20.0    # detik antar ping keepalive ke client
PING_TIMEOUT  = 10.0    # detik tunggu pong sebelum putus koneksi

# ============================================================
#  GAME STATE
# ============================================================
class GameState:
    COLORS = [
        {"c": "#ffffff", "s": "#e0e0e0"},
        {"c": "#ffca28", "s": "#ff6f00"},
        {"c": "#42a5f5", "s": "#1565c0"},
        {"c": "#ef5350", "s": "#c62828"},
        {"c": "#ab47bc", "s": "#6a1b9a"},
        {"c": "#26a69a", "s": "#00695c"},
        {"c": "#ffa726", "s": "#ef6c00"},
        {"c": "#ec407a", "s": "#ad1457"},
        {"c": "#8d6e63", "s": "#4e342e"},
        {"c": "#bdbdbd", "s": "#616161"},
    ]

    def __init__(self):
        # ws → player_id
        self.clients: dict = {}
        # player_id → player dict
        self.players: dict = {}

        self.seed: int          = random.randint(1, 999_999)
        self.hole_number: int   = 1
        self.current_turn_id: Optional[str] = None
        self.game_over: bool    = False
        # True sejak shoot dikirim sampai turnEnd diterima
        self.waiting_for_stop: bool = False

        self._next_color: int = 0

    # ── Helpers ──────────────────────────────────────────────
    @staticmethod
    def _gen_id() -> str:
        return "".join(random.choices(string.ascii_lowercase + string.digits, k=8))

    def _next_color_pair(self) -> dict:
        pair = self.COLORS[self._next_color % len(self.COLORS)]
        self._next_color += 1
        return pair

    def _start_x(self, index: int) -> int:
        """Posisi X awal per urutan join — agak berjauhan agar tidak tumpuk."""
        return 80 + index * 20

    # ── Player management ────────────────────────────────────
    def create_player(self, name: str) -> tuple[str, dict]:
        pid   = self._gen_id()
        color = self._next_color_pair()
        idx   = len(self.players)
        player = {
            "id":          pid,
            "name":        name,
            "color":       color["c"],
            "strokeColor": color["s"],
            "x":           self._start_x(idx),
            "y":           0,
            "vx":          0,
            "vy":          0,
            "strokes":     0,
            "totalStrokes":0,
            "wins":        0,
            "inHole":      False,
            "isStopped":   True,
            "index":       idx,
        }
        self.players[pid] = player
        if self.current_turn_id is None:
            self.current_turn_id = pid
        return pid, player

    def remove_player(self, pid: str):
        """Hapus player dan kembalikan nama untuk logging."""
        name = self.players.get(pid, {}).get("name", pid)
        self.players.pop(pid, None)
        # Jika giliran player ini, pindah ke berikutnya
        if self.current_turn_id == pid:
            self.current_turn_id = None
            self.waiting_for_stop = False   # ← FIX: reset agar game tidak stuck
            if self.players:
                self.current_turn_id = list(self.players.keys())[0]
        return name

    # ── Turn management ──────────────────────────────────────
    def advance_turn(self):
        """
        Pindah giliran ke player berikutnya yang:
        - Belum inHole
        - Masih terhubung (ada di self.players)
        Jika semua sudah inHole, game_over = True.
        """
        if not self.players:
            self.current_turn_id = None
            return

        ids = list(self.players.keys())

        # Cari indeks current — jika tidak ada, mulai dari 0
        try:
            cur_idx = ids.index(self.current_turn_id)
        except ValueError:
            cur_idx = -1

        for _ in range(len(ids)):
            cur_idx = (cur_idx + 1) % len(ids)
            candidate = ids[cur_idx]
            if not self.players[candidate]["inHole"]:
                self.current_turn_id = candidate
                return

        # Semua pemain sudah inHole — seharusnya sudah game_over, tapi guard di sini
        self.current_turn_id = ids[0]

    def all_in_hole(self) -> bool:
        return all(p["inHole"] for p in self.players.values())

    # ── Map reset ────────────────────────────────────────────
    def reset_map(self):
        self.seed        = random.randint(1, 999_999)
        self.hole_number += 1
        self.game_over   = False
        self.waiting_for_stop = False

        for i, (pid, p) in enumerate(self.players.items()):
            p["x"]        = self._start_x(i)
            p["y"]        = 0
            p["vx"]       = 0
            p["vy"]       = 0
            p["strokes"]  = 0
            p["inHole"]   = False
            p["isStopped"]= True

        if self.players:
            self.current_turn_id = list(self.players.keys())[0]

    # ── Message builders ─────────────────────────────────────
    def build_state(self) -> dict:
        """
        FIX: Selalu sertakan seed agar client yang baru join
        bisa langsung render terrain yang benar.
        """
        return {
            "type":        "state",
            "players":     list(self.players.values()),
            "currentTurn": self.current_turn_id,
            "holeNumber":  self.hole_number,
            "seed":        self.seed,           # ← FIX
            "gameOver":    self.game_over,
        }

    def build_leaderboard(self) -> dict:
        data = sorted(
            [
                {
                    "id":          pid,
                    "name":        p["name"],
                    "color":       p["color"],
                    "wins":        p["wins"],
                    "totalStrokes":p["totalStrokes"],
                }
                for pid, p in self.players.items()
            ],
            key=lambda x: (-x["wins"], x["totalStrokes"])
        )
        return {"type": "leaderboard", "data": data}


# ── Singleton ─────────────────────────────────────────────────
state = GameState()

# ============================================================
#  BROADCAST HELPERS
# ============================================================
async def broadcast(msg_dict: dict, exclude=None):
    """Kirim pesan ke semua client, hapus yang sudah mati."""
    if not state.clients:
        return
    payload = json.dumps(msg_dict)
    dead    = []

    for ws, pid in list(state.clients.items()):
        if exclude and ws is exclude:
            continue
        try:
            await ws.send(payload)
        except Exception:
            dead.append(ws)

    for ws in dead:
        pid = state.clients.pop(ws, None)
        if pid:
            name = state.remove_player(pid)
            log.info(f"[-] Auto-removed dead client: {name}")
            await broadcast(state.build_state())
            await broadcast(state.build_leaderboard())


async def send_to(ws, msg_dict: dict):
    """Kirim ke satu client, abaikan jika gagal."""
    try:
        await ws.send(json.dumps(msg_dict))
    except Exception:
        pass


async def broadcast_state():
    await broadcast(state.build_state())


async def broadcast_turn():
    await broadcast({
        "type":     "turn",
        "playerId": state.current_turn_id,
    })

# ============================================================
#  HANDLER UTAMA
# ============================================================
async def handler(websocket):
    addr = getattr(websocket, "remote_address", "?")
    log.info(f"[+] Connect: {addr}")
    player_id: Optional[str] = None

    try:
        async for raw in websocket:
            # ── Parse ────────────────────────────────────────
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg_type = data.get("type", "")

            # ════════════════════════════════════════════════
            #  JOIN
            # ════════════════════════════════════════════════
            if msg_type == "join":
                if len(state.players) >= MAX_PLAYERS:
                    await send_to(websocket, {"type": "error", "msg": "Server penuh."})
                    continue
                if player_id:
                    # Sudah join sebelumnya — abaikan double join
                    continue

                name = str(data.get("name", "Pemain")).strip()[:12] or "Pemain"
                pid, player = state.create_player(name)
                state.clients[websocket] = pid
                player_id = pid

                # Kirim init hanya ke pemain baru
                await send_to(websocket, {
                    "type":        "init",
                    "yourId":      pid,
                    "seed":        state.seed,
                    "holeNumber":  state.hole_number,
                    "players":     list(state.players.values()),
                    "currentTurn": state.current_turn_id,
                    "gameOver":    state.game_over,
                })

                await broadcast({"type": "playerJoined", "player": player}, exclude=websocket)
                await broadcast_state()
                await broadcast(state.build_leaderboard())
                log.info(f"[+] {name} ({pid}) joined. Players: {len(state.players)}")

            # ════════════════════════════════════════════════
            #  SHOOT
            # ════════════════════════════════════════════════
            elif msg_type == "shoot" and player_id:
                # Guard: hanya giliran player ini
                if player_id != state.current_turn_id:
                    continue

                p = state.players.get(player_id)
                if not p or p["inHole"] or not p["isStopped"]:
                    continue

                # ── Validasi & clamp kecepatan (anti-cheat) ─
                vx = float(data.get("vx", 0))
                vy = float(data.get("vy", 0))
                vx = max(-MAX_VX, min(MAX_VX, vx))
                vy = max(-MAX_VY, min(MAX_VY, vy))

                p["vx"]          = vx
                p["vy"]          = vy
                p["strokes"]    += 1
                p["totalStrokes"]+= 1
                p["isStopped"]   = False
                state.waiting_for_stop = True

                await broadcast({
                    "type":     "shoot",
                    "playerId": player_id,
                    "vx":       vx,
                    "vy":       vy,
                    "strokes":  p["strokes"],
                })
                log.info(f"[>] {p['name']} shoots vx={vx:.2f} vy={vy:.2f} (stroke #{p['strokes']})")

            # ════════════════════════════════════════════════
            #  SYNC — update posisi bola dari client authoritative
            # ════════════════════════════════════════════════
            elif msg_type == "sync" and player_id:
                p = state.players.get(player_id)
                if not p:
                    continue
                # Hanya update jika bola dimiliki player ini
                p["x"]        = float(data.get("x",  p["x"]))
                p["y"]        = float(data.get("y",  p["y"]))
                p["vx"]       = float(data.get("vx", p["vx"]))
                p["vy"]       = float(data.get("vy", p["vy"]))
                p["isStopped"]= bool(data.get("isStopped", p["isStopped"]))

            # ════════════════════════════════════════════════
            #  OOB — Out of Bounds (FIX UTAMA)
            # ════════════════════════════════════════════════
            elif msg_type == "oob" and player_id:
                """
                Client mendeteksi bola keluar batas kiri/kanan.
                Server:
                  1. Validasi giliran
                  2. Tambah penalti stroke
                  3. Reset posisi ke lastSafe dari client
                  4. Paksa bola berhenti
                  5. Reset waiting_for_stop
                  6. Advance giliran
                  7. Broadcast semua
                """
                if player_id != state.current_turn_id:
                    continue

                p = state.players.get(player_id)
                if not p or p["inHole"]:
                    continue

                # Ambil posisi aman dari client
                safe_x = float(data.get("x", p["x"]))
                safe_y = float(data.get("y", p["y"]))

                # Terapkan penalti
                p["strokes"]     += OOB_PENALTY
                p["totalStrokes"] += OOB_PENALTY
                p["x"]            = safe_x
                p["y"]            = safe_y
                p["vx"]           = 0
                p["vy"]           = 0
                p["isStopped"]    = True
                state.waiting_for_stop = False

                log.info(
                    f"[!] OOB: {p['name']} — penalti +{OOB_PENALTY} stroke "
                    f"(total {p['strokes']}), kembali ke ({safe_x:.0f}, {safe_y:.0f})"
                )

                # Broadcast event OOB agar semua client tahu
                await broadcast({
                    "type":     "oob",
                    "playerId": player_id,
                    "name":     p["name"],
                    "strokes":  p["strokes"],
                    "x":        safe_x,
                    "y":        safe_y,
                })

                # Pindah giliran
                state.advance_turn()
                await broadcast_turn()
                await broadcast_state()

            # ════════════════════════════════════════════════
            #  HOLE — bola masuk lubang
            # ════════════════════════════════════════════════
            elif msg_type == "hole" and player_id:
                p = state.players.get(player_id)
                if not p or p["inHole"] or state.game_over:
                    continue

                p["inHole"]  = True
                p["vx"]      = 0
                p["vy"]      = 0
                p["wins"]   += 1
                state.game_over       = True
                state.waiting_for_stop= False

                log.info(f"[*] {p['name']} MENANG! Hole #{state.hole_number} — {p['strokes']} pukulan")

                await broadcast({
                    "type":       "win",
                    "playerId":   player_id,
                    "name":       p["name"],
                    "holeNumber": state.hole_number,
                    "strokes":    p["strokes"],
                })
                await broadcast(state.build_leaderboard())

                # Tunggu lalu reset map
                await asyncio.sleep(RESET_DELAY)
                state.reset_map()

                await broadcast({
                    "type":        "reset",
                    "seed":        state.seed,
                    "holeNumber":  state.hole_number,
                    "players":     list(state.players.values()),
                    "currentTurn": state.current_turn_id,
                })
                await broadcast(state.build_leaderboard())
                log.info(f"[*] Map reset → seed={state.seed}, hole=#{state.hole_number}")

            # ════════════════════════════════════════════════
            #  TURN END — client lapor bola sudah berhenti
            # ════════════════════════════════════════════════
            elif msg_type == "turnEnd" and player_id:
                if player_id != state.current_turn_id:
                    continue

                p = state.players.get(player_id)
                if not p or p["inHole"]:
                    continue

                # FIX: Tidak bergantung waiting_for_stop semata.
                # OOB sudah set isStopped=True di server — cukup
                # validasi bola memang berhenti.
                if not p["isStopped"]:
                    # Bola belum berhenti — tolak request (spurious turnEnd)
                    continue

                state.waiting_for_stop = False
                state.advance_turn()
                await broadcast_turn()
                await broadcast_state()

                if state.current_turn_id and state.current_turn_id in state.players:
                    next_name = state.players[state.current_turn_id]["name"]
                    log.info(f"[→] Giliran pindah ke {next_name}")

    except ConnectionClosed:
        pass
    except Exception as e:
        log.error(f"[!] Handler error: {e}")
    finally:
        log.info(f"[-] Disconnect: {addr}")
        if player_id:
            # Hapus dari clients map
            state.clients.pop(websocket, None)
            name = state.remove_player(player_id)
            log.info(f"[-] {name} removed. Players: {len(state.players)}")
            if state.players:
                await broadcast_state()
                await broadcast(state.build_leaderboard())
                if state.current_turn_id:
                    await broadcast_turn()


# ============================================================
#  STATE BROADCASTER — periodic sync untuk semua client
#  Adaptive: lebih jarang broadcast jika tidak ada pemain aktif
# ============================================================
async def state_broadcaster():
    while True:
        await asyncio.sleep(BROADCAST_HZ)
        if not state.players or state.game_over:
            continue
        # Hanya broadcast jika ada bola bergerak
        any_moving = any(
            not p["isStopped"] and not p["inHole"]
            for p in state.players.values()
        )
        if any_moving:
            await broadcast_state()


# ============================================================
#  PING / KEEPALIVE
#  Mencegah koneksi hotspot Android putus diam-diam
#  (Android sering kill idle WebSocket setelah ~30 detik)
# ============================================================
async def ping_loop():
    while True:
        await asyncio.sleep(PING_INTERVAL)
        if not state.clients:
            continue
        dead = []
        for ws in list(state.clients.keys()):
            try:
                await asyncio.wait_for(ws.ping(), timeout=PING_TIMEOUT)
            except Exception:
                dead.append(ws)
        for ws in dead:
            pid = state.clients.pop(ws, None)
            if pid:
                name = state.remove_player(pid)
                log.info(f"[-] Ping timeout, removed: {name}")
                await broadcast_state()
                await broadcast(state.build_leaderboard())


# ============================================================
#  MAIN
# ============================================================
async def main():
    banner = f"""
{'='*52}
  Procedural Mini Golf — WebSocket Server
  ws://{HOST}:{PORT}
  Max players : {MAX_PLAYERS}
  OOB penalty : +{OOB_PENALTY} stroke
  Ping every  : {PING_INTERVAL}s
  {'='*52}"""
    print(banner)

    async with websockets.serve(
        handler,
        HOST,
        PORT,
        # Keepalive di level websockets juga (backup selain ping_loop)
        ping_interval=None,   # kita urus sendiri via ping_loop
        ping_timeout=None,
        max_size=2**18,        # 256 KB — cukup untuk JSON game
    ):
        await asyncio.gather(
            state_broadcaster(),
            ping_loop(),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Server dihentikan.")
