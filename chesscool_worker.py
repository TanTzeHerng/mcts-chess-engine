import asyncio
import hashlib
import os
import subprocess
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from playwright.async_api import async_playwright

LC0 = "/opt/lc0/build/release/lc0"
WEIGHTS = "/opt/elite_v2.pb.gz"
EXPECTED_NET_SHA256 = "bd0f9cf2f535eba4a970eb680846635208c683e1c2c763b66bfbd26bfc351e16"
VERIFY_FEN = "r1bqkbnr/pppp1ppp/2n5/4p3/3PP3/5N2/PPP2PPP/RNBQKB1R b KQkq - 0 3"
VERIFY_MOVE = "e5d4"

CONTROL_TOKEN = os.environ.get("CONTROL_TOKEN", "")
USERNAME = os.environ.get("CHESSCOOL_USERNAME", "DanielHale7510")

state = {
    "started_at": time.time(),
    "engine_ready": False,
    "engine_test_move": None,
    "engine_test_ok": False,
    "net_sha256": None,
    "browser_ready": False,
    "authenticated": False,
    "page_url": None,
    "page_title": None,
    "browser_error": None,
    "engine_error": None,
}

class UCIEngine:
    def __init__(self):
        self.p = None
        self.lock = threading.Lock()

    def _write(self, line):
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()

    def _until(self, prefix, timeout=30):
        deadline = time.time() + timeout
        lines = []
        while time.time() < deadline:
            line = self.p.stdout.readline()
            if not line:
                if self.p.poll() is not None:
                    raise RuntimeError(f"lc0 exited with {self.p.returncode}: {lines[-20:]}")
                time.sleep(0.01)
                continue
            line = line.strip()
            lines.append(line)
            if line.startswith(prefix):
                return line, lines
        raise TimeoutError(f"Timeout waiting for {prefix}; tail={lines[-20:]}")

    def start(self):
        with self.lock:
            self.p = subprocess.Popen(
                [LC0, f"--weights={WEIGHTS}", "--backend=blas"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            self._write("uci")
            self._until("uciok", 45)
            self._write("setoption name Threads value 2")
            self._write("setoption name MinibatchSize value 1")
            self._write("isready")
            self._until("readyok", 45)

    def move(self, fen):
        with self.lock:
            self._write("ucinewgame")
            self._write("isready")
            self._until("readyok", 20)
            self._write("position fen " + fen)
            self._write("go nodes 1")
            line, lines = self._until("bestmove ", 30)
            parts = line.split()
            if len(parts) < 2:
                raise RuntimeError(f"Malformed bestmove: {line}")
            return parts[1], lines

    def close(self):
        if self.p and self.p.poll() is None:
            try:
                self._write("quit")
                self.p.wait(timeout=3)
            except Exception:
                self.p.kill()

engine = UCIEngine()
pw = None
browser = None
context = None
page = None

def check_token(token):
    if not CONTROL_TOKEN or token != CONTROL_TOKEN:
        raise HTTPException(status_code=403, detail="bad control token")

def cookie_specs():
    mapping = {
        "randid": os.environ.get("CHESSCOOL_RANDID"),
        "PHPSESSID": os.environ.get("CHESSCOOL_PHPSESSID"),
        "authinfo": os.environ.get("CHESSCOOL_AUTHINFO"),
        "authtoken": os.environ.get("CHESSCOOL_AUTHTOKEN"),
        "csrf": os.environ.get("CHESSCOOL_CSRF"),
    }
    return [
        {"name": k, "value": v, "domain": "chess.cool", "path": "/", "secure": k in {"randid", "csrf"}}
        for k, v in mapping.items() if v
    ]

async def init_browser():
    global pw, browser, context, page
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context = await browser.new_context()
        cookies = cookie_specs()
        if not cookies:
            raise RuntimeError("No Chess.cool cookies configured")
        await context.add_cookies(cookies)
        page = await context.new_page()
        await page.goto("https://chess.cool/play/new", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(4000)
        state["page_url"] = page.url
        state["page_title"] = await page.title()
        user = await page.evaluate("() => (typeof cs_user !== 'undefined' ? cs_user : null)")
        state["authenticated"] = bool(user and str(user.get("login", "")).lower() == USERNAME.lower())
        state["browser_ready"] = True
    except Exception as e:
        state["browser_error"] = repr(e)
        state["browser_ready"] = False

def init_engine():
    try:
        with open(WEIGHTS, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
        state["net_sha256"] = sha
        if sha != EXPECTED_NET_SHA256:
            raise RuntimeError(f"Elite net SHA mismatch: {sha}")
        engine.start()
        move, _ = engine.move(VERIFY_FEN)
        state["engine_test_move"] = move
        state["engine_test_ok"] = move == VERIFY_MOVE
        if not state["engine_test_ok"]:
            raise RuntimeError(f"1-node verification mismatch: got {move}, expected {VERIFY_MOVE}")
        state["engine_ready"] = True
    except Exception as e:
        state["engine_error"] = repr(e)
        state["engine_ready"] = False

@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.gather(asyncio.to_thread(init_engine), init_browser())
    yield
    try:
        engine.close()
    except Exception:
        pass
    if context:
        await context.close()
    if browser:
        await browser.close()
    if pw:
        await pw.stop()

app = FastAPI(lifespan=lifespan)

@app.get("/health")
async def health():
    return {
        "ok": bool(state["engine_ready"] and state["authenticated"]),
        **state,
    }

@app.get("/inspect")
async def inspect(x_control_token: str = Header(default="")):
    check_token(x_control_token)
    if not page:
        raise HTTPException(503, "browser unavailable")
    await page.goto("https://chess.cool/play/new", wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(5000)
    form = page.locator("#create_new_game_form")
    if await form.count() == 0:
        return {
            "url": page.url,
            "title": await page.title(),
            "text": (await page.locator("body").inner_text())[:12000],
            "form_found": False,
        }
    data = await form.evaluate("""form => ({
        text: form.innerText,
        inputs: Array.from(form.querySelectorAll('input')).map(e => ({
            id:e.id, name:e.name, type:e.type, value:e.value, checked:e.checked,
            min:e.min, max:e.max, step:e.step, className:e.className
        })),
        selects: Array.from(form.querySelectorAll('select')).map(e => ({
            id:e.id, name:e.name, value:e.value,
            options:Array.from(e.options).map(o=>({value:o.value,text:o.text,selected:o.selected}))
        })),
        buttons: Array.from(form.querySelectorAll('button')).map(e => ({
            text:e.innerText, value:e.value, className:e.className,
            inputId:e.getAttribute('data-input-id')
        })),
        html: form.outerHTML.slice(0,30000)
    })""")
    user = await page.evaluate("() => (typeof cs_user !== 'undefined' ? cs_user : null)")
    return {"url": page.url, "title": await page.title(), "user": user, "form_found": True, **data}

@app.get("/engine-test")
async def engine_test(x_control_token: str = Header(default="")):
    check_token(x_control_token)
    if not state["engine_ready"]:
        raise HTTPException(503, state["engine_error"] or "engine unavailable")
    move, lines = await asyncio.to_thread(engine.move, VERIFY_FEN)
    return {"fen": VERIFY_FEN, "nodes": 1, "bestmove": move, "expected": VERIFY_MOVE, "ok": move == VERIFY_MOVE, "tail": lines[-15:]}
