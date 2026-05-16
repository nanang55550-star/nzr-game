#!/usr/bin/env python3
"""
Procedural Mini Golf - WebSocket Server
Jalankan di Termux/HP Hotspot:  python server.py
"""

import asyncio
import websockets
import json
import random
import string

# Konfigurasi
HOST = "0.0.0.0"   
PORT = 8765

# ============================================================
#  GAME STATE SERVER
# ============================================================
class GameState:
    def __init__(self):
        self.clients = {}          
        self.players = {}          
        self.seed = random.randint(1, 999999)
        self.hole_number = 1
        self.current_turn_id = None
        self.game_over = False
        self.waiting_for_stop = False
        self.leaderboard = {}      
        self.colors = [
            {"c": "#ffffff", "s": "#e0e0e0"},
            {"c": "#ffca28", "s": "#ff6f00"},
            {"c": "#42a5f5", "s": "#1565c0"},
            {"c": "#ef5350", "s": "#c62828"},
            {"c": "#ab47bc", "s": "#6a1b9a"},
            {"c": "#26a69a", "s": "#00695c"},
            {"c": "#ffa726", "s": "#ef6c00"},
            {"c": "#ec407a", "s": "#ad1457"},
            {"c": "#8d6e63", "s": "#4e342e"},
            {"c": "#bdbdbd", "s": "#616161"}
        ]
        self.next_color_idx = 0

    def gen_id(self):
        return ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))

    def get_start_x(self, index):
        return 80 + (index * 18)

    def create_player(self, name):
        pid = self.gen_id()
        color = self.colors[self.next_color_idx % len(self.colors)]
        self.next_color_idx += 1
        idx = len(self.players)
        player = {
            "id": pid,
            "name": name,
            "color": color["c"],
            "strokeColor": color["s"],
            "x": self.get_start_x(idx),
            "y": 0,           
            "vx": 0,
            "vy": 0,
            "strokes": 0,
            "totalStrokes": 0,
            "wins": 0,
            "inHole": False,
            "isStopped": True,
            "index": idx
        }
        self.players[pid] = player
        self.leaderboard[pid] = 0
        if self.current_turn_id is None:
            self.current_turn_id = pid
        return pid, player

    def reset_map(self):
        self.seed = random.randint(1, 999999)
        self.hole_number += 1
        self.game_over = False
        self.waiting_for_stop = False
        for i, (pid, p) in enumerate(self.players.items()):
            p["x"] = self.get_start_x(i)
            p["y"] = 0
            p["vx"] = 0
            p["vy"] = 0
            p["strokes"] = 0
            p["inHole"] = False
            p["isStopped"] = True
        if self.players:
            self.current_turn_id = list(self.players.keys())[0]

    def advance_turn(self):
        if not self.players:
            return
        ids = list(self.players.keys())
        if self.current_turn_id not in ids:
            self.current_turn_id = ids[0]
            return
        cur_idx = ids.index(self.current_turn_id)
        attempts = 0
        while attempts < len(ids):
            cur_idx = (cur_idx + 1) % len(ids)
            nid = ids[cur_idx]
            if not self.players[nid]["inHole"]:
                self.current_turn_id = nid
                return
            attempts += 1
        self.current_turn_id = ids[0]

    def build_state(self):
        return {
            "type": "state",
            "players": list(self.players.values()),
            "currentTurn": self.current_turn_id,
            "holeNumber": self.hole_number,
            "seed": self.seed,
            "gameOver": self.game_over
        }

    def build_leaderboard(self):
        data = []
        for pid, p in self.players.items():
            data.append({
                "id": pid,
                "name": p["name"],
                "color": p["color"],
                "wins": p["wins"],
                "totalStrokes": p["totalStrokes"]
            })
        data.sort(key=lambda x: (-x["wins"], x["totalStrokes"]))
        return {"type": "leaderboard", "data": data}


state = GameState()

# ============================================================
#  WEBSOCKET HANDLER
# ============================================================
async def broadcast(msg_dict, exclude=None):
    msg = json.dumps(msg_dict)
    dead = []
    for ws, pid in state.clients.items():
        if exclude and ws == exclude:
            continue
        try:
            await ws.send(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in state.clients:
            await remove_player(state.clients[ws])

async def broadcast_state():
    await broadcast(state.build_state())

async def remove_player(pid):
    if pid in state.players:
        del state.players[pid]
    to_remove = [ws for ws, id_ in state.clients.items() if id_ == pid]
    for ws in to_remove:
        if ws in state.clients:
            del state.clients[ws]
    if state.current_turn_id == pid and state.players:
        state.current_turn_id = list(state.players.keys())[0]
        await broadcast({"type": "turn", "playerId": state.current_turn_id})
    await broadcast_state()

# FIX: Menghapus parameter 'path' agar kompatibel dengan websockets v14+
async def handler(websocket):
    print(f"[+] Client connected: {websocket.remote_address}")
    player_id = None

    try:
        async for message in websocket:
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type")

            if msg_type == "join":
                name = data.get("name", "Pemain").strip()[:12]
                if not name:
                    name = "Pemain"
                pid, player = state.create_player(name)
                state.clients[websocket] = pid
                player_id = pid

                await websocket.send(json.dumps({
                    "type": "init",
                    "yourId": pid,
                    "seed": state.seed,
                    "holeNumber": state.hole_number,
                    "players": list(state.players.values()),
                    "currentTurn": state.current_turn_id,
                    "gameOver": state.game_over
                }))

                await broadcast({"type": "playerJoined", "player": player}, exclude=websocket)
                await broadcast_state()
                await broadcast(state.build_leaderboard())
                print(f"[+] {name} ({pid}) joined. Total players: {len(state.players)}")

            elif msg_type == "shoot" and player_id:
                vx = data.get("vx", 0)
                vy = data.get("vy", 0)
                if player_id != state.current_turn_id:
                    continue
                p = state.players.get(player_id)
                if not p or p["inHole"] or not p["isStopped"]:
                    continue
                p["vx"] = vx
                p["vy"] = vy
                p["strokes"] += 1
                p["totalStrokes"] += 1
                p["isStopped"] = False
                state.waiting_for_stop = True
                await broadcast({
                    "type": "shoot",
                    "playerId": player_id,
                    "vx": vx,
                    "vy": vy,
                    "strokes": p["strokes"]
                })
                print(f"[>] {p['name']} shoots vx={vx:.2f} vy={vy:.2f}")

            elif msg_type == "sync" and player_id:
                p = state.players.get(player_id)
                if p:
                    p["x"] = data.get("x", p["x"])
                    p["y"] = data.get("y", p["y"])
                    p["vx"] = data.get("vx", p["vx"])
                    p["vy"] = data.get("vy", p["vy"])
                    p["isStopped"] = data.get("isStopped", p["isStopped"])

            elif msg_type == "hole" and player_id:
                p = state.players.get(player_id)
                if not p or p["inHole"]:
                    continue
                p["inHole"] = True
                p["vx"] = 0
                p["vy"] = 0
                p["wins"] += 1
                state.game_over = True
                print(f"[*] {p['name']} MENANG! Hole #{state.hole_number}")

                await broadcast({
                    "type": "win",
                    "playerId": player_id,
                    "name": p["name"],
                    "holeNumber": state.hole_number
                })
                await broadcast(state.build_leaderboard())

                await asyncio.sleep(3)
                state.reset_map()
                await broadcast({
                    "type": "reset",
                    "seed": state.seed,
                    "holeNumber": state.hole_number,
                    "players": list(state.players.values()),
                    "currentTurn": state.current_turn_id
                })
                await broadcast(state.build_leaderboard())
                print(f"[*] Map reset. New seed: {state.seed}. Hole #{state.hole_number}")

            elif msg_type == "turnEnd" and player_id:
                if player_id != state.current_turn_id:
                    continue
                p = state.players.get(player_id)
                if not p or not p["isStopped"] or p["inHole"]:
                    continue
                if not state.waiting_for_stop:
                    continue
                state.waiting_for_stop = False
                state.advance_turn()
                await broadcast({
                    "type": "turn",
                    "playerId": state.current_turn_id
                })
                await broadcast_state()
                print(f"[→] Giliran pindah ke {state.players[state.current_turn_id]['name']}")

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        print(f"[-] Client disconnected")
        if player_id:
            await remove_player(player_id)


# ============================================================
#  STATE BROADCASTER LOOP (Diperlambat jadi 1 detik demi stabilitas hotspot)
# ============================================================
async def state_broadcaster():
    while True:
        await asyncio.sleep(1.0)  # Diubah ke 1.0 detik agar tidak membebani jaringan lokal
        if state.players:
            await broadcast_state()


# ============================================================
#  MAIN
# ============================================================
async def main():
    print(f"=" * 50)
    print("  Procedural Mini Golf - WebSocket Server")
    print(f"  Listening on ws://{HOST}:{PORT}")
    print(f"  Share IP Hotspot ke client!")
    print(f"=" * 50)
    # FIX: Menghapus parameter 'path' di websockets.serve
    async with websockets.serve(handler, HOST, PORT):
        await state_broadcaster()

if __name__ == "__main__":
    asyncio.run(main())
