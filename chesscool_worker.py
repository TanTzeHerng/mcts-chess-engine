import hashlib
import os
import subprocess
import threading
import time

from fastapi import FastAPI, HTTPException, Query

LC0 = "/opt/lc0/build/release/lc0"
WEIGHTS = "/opt/elite_v2.pb.gz"
CONTROL_TOKEN = os.environ.get("CONTROL_TOKEN", "")

VERIFY_FEN = "r1bqkbnr/pppp1ppp/2n5/4p3/3PP3/5N2/PPP2PPP/RNBQKB1R b KQkq - 0 3"
VERIFY_MOVE = "e5d4"

state = {
    "started_at": time.time(),
    "engine_ready": False,
    "engine_error": None,
    "verify_move": None,
    "verify_ok": False,
    "net_sha256": None,
    "net_size": None,
}

class EliteEngine:
    def __init__(self):
        self.p = None
        self.lock = threading.Lock()

    def _write(self, line):
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()

    def _wait_for(self, prefix, timeout=45):
        deadline = time.time() + timeout
        lines = []
        while time.time() < deadline:
            line = self.p.stdout.readline()
            if not line:
                if self.p.poll() is not None:
                    raise RuntimeError(f"lc0 exited rc={self.p.returncode}; tail={lines[-20:]}")
                time.sleep(0.01)
                continue
            line = line.strip()
            lines.append(line)
            if line.startswith(prefix):
                return line, lines
        raise TimeoutError(f"timeout waiting for {prefix}; tail={lines[-20:]}")

    def start(self):
        self.p = subprocess.Popen(
            [LC0, f"--weights={WEIGHTS}", "--backend=blas"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._write("uci")
        self._wait_for("uciok")
        self._write("setoption name Threads value 2")
        self._write("setoption name MinibatchSize value 1")
        self._write("isready")
        self._wait_for("readyok")

    def bestmove(self, fen):
        with self.lock:
            self._write("ucinewgame")
            self._write("isready")
            self._wait_for("readyok", 20)
            self._write("position fen " + fen)
            self._write("go nodes 1")
            line, lines = self._wait_for("bestmove ", 30)
            parts = line.split()
            if len(parts) < 2:
                raise RuntimeError("malformed bestmove: " + line)
            return parts[1], lines

engine = EliteEngine()

def initialize():
    try:
        state["net_size"] = os.path.getsize(WEIGHTS)
        h = hashlib.sha256()
        with open(WEIGHTS, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        state["net_sha256"] = h.hexdigest()
        engine.start()
        move, _ = engine.bestmove(VERIFY_FEN)
        state["verify_move"] = move
        state["verify_ok"] = move == VERIFY_MOVE
        if not state["verify_ok"]:
            raise RuntimeError(f"Elite one-node self-test failed: got {move}, expected {VERIFY_MOVE}")
        state["engine_ready"] = True
    except Exception as exc:
        state["engine_error"] = repr(exc)
        state["engine_ready"] = False

initialize()
app = FastAPI()

def authorize(token):
    if not CONTROL_TOKEN or token != CONTROL_TOKEN:
        raise HTTPException(status_code=403, detail="forbidden")

@app.get("/health")
def health():
    return {"ok": state["engine_ready"], **state}

@app.get("/verify")
def verify(token: str = Query(default="")):
    authorize(token)
    if not state["engine_ready"]:
        raise HTTPException(status_code=503, detail=state["engine_error"] or "engine unavailable")
    move, lines = engine.bestmove(VERIFY_FEN)
    return {
        "fen": VERIFY_FEN,
        "nodes": 1,
        "bestmove": move,
        "expected": VERIFY_MOVE,
        "ok": move == VERIFY_MOVE,
        "tail": lines[-12:],
    }

@app.get("/move")
def move(fen: str, token: str = Query(default="")):
    authorize(token)
    if not state["engine_ready"]:
        raise HTTPException(status_code=503, detail=state["engine_error"] or "engine unavailable")
    bestmove, lines = engine.bestmove(fen)
    return {
        "fen": fen,
        "nodes": 1,
        "bestmove": bestmove,
        "tail": lines[-12:],
    }
