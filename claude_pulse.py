"""Claude Pulse — widget flutuante de monitorizacao de uso do Claude Code.

Le os transcritos JSONL em ~/.claude/projects/, agrega tokens/custos por
dia/mes/modelo e obtem as percentagens reais dos limites (5h + semanal)
via o endpoint OAuth usado pelo /usage do Claude Code.
"""
import asyncio
import ctypes
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import webview

VERSION = "1.2.0"
REPO = "Masterjbrito/claude-pulse"

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
CREDS_FILE = CLAUDE_DIR / ".credentials.json"

# Precos API por MTok: (input, output, cache_write_5m, cache_read)
PRICING = {
    "opus": (15.0, 75.0, 18.75, 1.50),
    "fable": (15.0, 75.0, 18.75, 1.50),   # estimado (tabela opus)
    "sonnet": (3.0, 15.0, 3.75, 0.30),
    "haiku": (1.0, 5.0, 1.25, 0.10),
}


def price_for(model: str):
    m = model.lower()
    for key, p in PRICING.items():
        if key in m:
            return p
    return PRICING["sonnet"]


def short_model(model: str) -> str:
    m = model.lower()
    for key in PRICING:
        if key in m:
            return key.capitalize()
    return model.replace("claude-", "")[:16]


class UsageStore:
    """Cache incremental: rele apenas ficheiros alterados."""

    def __init__(self):
        self._files = {}       # path -> (mtime, size, records)
        self._lock = threading.Lock()

    @staticmethod
    def _proj_name(path: Path) -> str:
        try:
            folder = path.relative_to(PROJECTS_DIR).parts[0]
        except (ValueError, IndexError):
            return "?"
        # ex.: "c--Users-alice-Documents-my-app" -> "my-app" (generico p/ qualquer user)
        for marker in ("-Documents-", "-Desktop-", "-Projects-", "-repos-", "-src-"):
            i = folder.rfind(marker)
            if i != -1:
                return folder[i + len(marker):] or folder
        parts = folder.split("-")
        return "-".join(parts[4:]) or folder  # remove "c--Users-<nome>-"

    def _parse_file(self, path: Path):
        records = []
        seen = set()
        proj = self._proj_name(path)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"usage"' not in line:
                        continue
                    try:
                        j = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = j.get("message") or {}
                    usage = msg.get("usage")
                    if not usage or j.get("type") != "assistant":
                        continue
                    if (msg.get("model") or "") == "<synthetic>":
                        continue
                    key = (msg.get("id"), j.get("requestId"))
                    if key in seen:
                        continue
                    seen.add(key)
                    ts = j.get("timestamp")
                    if not ts:
                        continue
                    try:
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
                    except ValueError:
                        continue
                    records.append({
                        "proj": proj,
                        "day": dt.strftime("%Y-%m-%d"),
                        "month": dt.strftime("%Y-%m"),
                        "model": msg.get("model") or "?",
                        "in": usage.get("input_tokens", 0),
                        "out": usage.get("output_tokens", 0),
                        "cw": usage.get("cache_creation_input_tokens", 0),
                        "cr": usage.get("cache_read_input_tokens", 0),
                    })
        except OSError:
            pass
        return records

    def records(self):
        with self._lock:
            current = set()
            if PROJECTS_DIR.exists():
                for path in PROJECTS_DIR.rglob("*.jsonl"):
                    current.add(path)
                    try:
                        st = path.stat()
                    except OSError:
                        continue
                    cached = self._files.get(path)
                    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
                        continue
                    self._files[path] = (st.st_mtime, st.st_size, self._parse_file(path))
            for gone in set(self._files) - current:
                del self._files[gone]
            out = []
            for _, _, recs in self._files.values():
                out.extend(recs)
            return out


def cost_of(r):
    pin, pout, pcw, pcr = price_for(r["model"])
    return (r["in"] * pin + r["out"] * pout + r["cw"] * pcw + r["cr"] * pcr) / 1e6


def fetch_limits():
    """Percentagens reais dos limites via endpoint OAuth (nao documentado)."""
    try:
        creds = json.loads(CREDS_FILE.read_text(encoding="utf-8"))
        token = (creds.get("claudeAiOauth") or {}).get("accessToken")
        if not token:
            return None
        resp = requests.get(
            "https://api.anthropic.com/api/oauth/usage",
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        out = []
        for lim in data.get("limits") or []:
            scope = lim.get("scope") or {}
            out.append({
                "kind": lim.get("kind"),
                "model": (scope.get("model") or {}).get("display_name"),
                "pct": round(float(lim.get("percent") or 0), 1),
                "resets_at": lim.get("resets_at"),
            })
        return out or None
    except Exception:
        return None


# Vozes neurais Microsoft (edge-tts) — nativas por idioma
NEURAL_VOICES = {
    "pt": "pt-PT-DuarteNeural",
    "en": "en-GB-RyanNeural",   # mordomo britanico, estilo JARVIS
    "es": "es-ES-AlvaroNeural",
    "fr": "fr-FR-HenriNeural",
    "de": "de-DE-ConradNeural",
}


def _play_mp3(path: str):
    """Toca um mp3 sem janela (winmm no Windows, afplay no macOS)."""
    try:
        if sys.platform == "win32":
            alias = f"cp{int(time.time()*1000)}"
            mci = ctypes.windll.winmm.mciSendStringW
            mci(f'open "{path}" type mpegvideo alias {alias}', None, 0, None)
            mci(f"play {alias} wait", None, 0, None)
            mci(f"close {alias}", None, 0, None)
        elif sys.platform == "darwin":
            import subprocess
            subprocess.run(["afplay", path], check=False)
        else:  # Linux: primeiro leitor mp3 disponivel
            import shutil
            import subprocess
            for player in (["mpg123", "-q"], ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"], ["cvlc", "--play-and-exit", "--intf", "dummy"]):
                if shutil.which(player[0]):
                    subprocess.run(player + [path], check=False)
                    break
    except Exception:
        pass


_speak_lock = threading.Lock()

# Soundbites locais (uso pessoal; pasta fora do git)
SOUNDS_DIR = Path(__file__).parent / "sounds"
ALERT_SOUNDS = {
    80: ["doh.mp3"],
    95: ["fatality.mp3", "gameover.mp3", "hasta.mp3"],
}


def _speak_worker(text: str, lang: str):
    voice = NEURAL_VOICES.get(lang, NEURAL_VOICES["en"])
    path = None
    try:
        import edge_tts
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            path = f.name
        with _speak_lock:
            asyncio.run(edge_tts.Communicate(text, voice, rate="+5%").save(path))
            _play_mp3(path)
    except Exception:
        pass  # sem rede: silencio nesta frase
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


LIMITS_CACHE = Path(__file__).parent / "limits_cache.json"


def forecast_depletion(limits):
    """Estima quando a quota semanal chega a 100% ao ritmo atual.

    Janela semanal comeca em resets_at - 7 dias; ritmo = pct / horas decorridas.
    Devolve ISO datetime se esgotar ANTES da renovacao, senao None.
    """
    try:
        wk = next(l for l in (limits or []) if l.get("kind") == "weekly_all")
        pct = float(wk["pct"])
        resets = datetime.fromisoformat(wk["resets_at"])
        start = resets - timedelta(days=7)
        now = datetime.now(timezone.utc)
        elapsed_h = (now - start).total_seconds() / 3600
        if pct < 2 or elapsed_h < 1:
            return None  # dados a menos para estimar
        eta = start + timedelta(hours=elapsed_h * 100.0 / pct)
        return eta.isoformat() if eta < resets else None
    except (StopIteration, KeyError, ValueError, TypeError):
        return None


class Tray:
    """Icone no system tray com a % semanal desenhada (pystray + PIL)."""

    def __init__(self, on_toggle, on_quit):
        self.icon = None
        self._pct = None
        self._on_toggle = on_toggle
        self._on_quit = on_quit

    def _draw(self, pct):
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGBA", (64, 64), (22, 19, 15, 255))
        d = ImageDraw.Draw(img)
        color = (255, 106, 77) if (pct or 0) >= 80 else (255, 180, 84)
        d.rounded_rectangle([2, 2, 61, 61], radius=12, outline=color, width=4)
        text = "--" if pct is None else str(int(round(pct)))
        try:
            font = ImageFont.truetype("consolab.ttf", 30 if len(text) < 3 else 24)
        except OSError:
            font = ImageFont.load_default()
        box = d.textbbox((0, 0), text, font=font)
        d.text(((64 - box[2] + box[0]) / 2 - box[0], (64 - box[3] + box[1]) / 2 - box[1]),
               text, fill=color, font=font)
        return img

    def start(self):
        try:
            import pystray
            self.icon = pystray.Icon(
                "claude-pulse", self._draw(None), "Claude Pulse",
                menu=pystray.Menu(
                    pystray.MenuItem("Mostrar / Ocultar", lambda: self._on_toggle(), default=True),
                    pystray.MenuItem("Sair", lambda: self._on_quit()),
                ))
            threading.Thread(target=self.icon.run, daemon=True).start()
        except Exception:
            self.icon = None

    def update(self, pct):
        if self.icon and pct is not None and pct != self._pct:
            self._pct = pct
            self.icon.icon = self._draw(pct)
            self.icon.title = f"Claude Pulse — semana {pct:.0f}%"

    def notify(self, title, msg):
        try:
            if self.icon:
                self.icon.notify(msg, title)
        except Exception:
            pass


class Api:
    def __init__(self):
        self.store = UsageStore()
        self._limits = None
        self._limits_ts = 0
        self.window = None
        self.tray = None
        self._update = None
        threading.Thread(target=self._check_update, daemon=True).start()
        try:  # ultimo valor conhecido (sobrevive a restarts e a 429s)
            self._limits = json.loads(LIMITS_CACHE.read_text(encoding="utf-8"))
        except Exception:
            pass

    def _check_update(self):
        """Verifica no GitHub se ha versao mais recente (1x por arranque)."""
        try:
            r = requests.get(f"https://api.github.com/repos/{REPO}/releases/latest", timeout=10)
            tag = (r.json().get("tag_name") or "").lstrip("v")
            if tag and tuple(map(int, tag.split("."))) > tuple(map(int, VERSION.split("."))):
                self._update = {"latest": tag,
                                "url": f"https://github.com/{REPO}/releases/latest"}
        except Exception:
            pass

    def _refresh_limits(self):
        lim = fetch_limits()
        if lim:  # em erro (ex. 429) mantem os ultimos valores conhecidos
            self._limits = lim
            try:
                LIMITS_CACHE.write_text(json.dumps(lim), encoding="utf-8")
            except Exception:
                pass

    def get_stats(self):
        recs = self.store.records()
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        month = now.strftime("%Y-%m")

        days = defaultdict(float)
        weeks = defaultdict(float)
        projs_month = defaultdict(float)
        models_month = defaultdict(lambda: {"cost": 0.0, "in": 0, "out": 0, "cw": 0, "cr": 0})
        today_cost = month_cost = total_cost = 0.0
        today_tok = {"in": 0, "out": 0, "cw": 0, "cr": 0}

        for r in recs:
            c = cost_of(r)
            total_cost += c
            days[r["day"]] += c
            iso = datetime.strptime(r["day"], "%Y-%m-%d").isocalendar()
            weeks[f"{iso[0]}-W{iso[1]:02d}"] += c
            if r["day"] == today:
                today_cost += c
                for k in today_tok:
                    today_tok[k] += r[k]
            if r["month"] == month:
                month_cost += c
                projs_month[r["proj"]] += c
                m = models_month[short_model(r["model"])]
                m["cost"] += c
                for k in ("in", "out", "cw", "cr"):
                    m[k] += r[k]

        last14 = sorted(days.items())[-14:]

        if time.time() - self._limits_ts > 300:  # 5 min: evita rate-limit (429)
            self._limits_ts = time.time()
            threading.Thread(target=self._refresh_limits, daemon=True).start()

        if self.tray and self._limits:
            wk = next((l for l in self._limits if l.get("kind") == "weekly_all"), None)
            if wk:
                self.tray.update(wk["pct"])

        return {
            "today": round(today_cost, 2),
            "month": round(month_cost, 2),
            "total": round(total_cost, 2),
            "today_tokens": today_tok,
            "days": [{"d": d, "c": round(c, 2)} for d, c in last14],
            "models": sorted(
                [dict(name=k, **{kk: round(vv, 2) if kk == "cost" else vv
                                 for kk, vv in v.items()})
                 for k, v in models_month.items()],
                key=lambda x: -x["cost"]),
            "projects": [{"name": k, "cost": round(v, 2)}
                         for k, v in sorted(projs_month.items(), key=lambda x: -x[1])[:10]],
            "weeks": [{"w": w, "c": round(c, 2)} for w, c in sorted(weeks.items())[-8:]],
            "version": VERSION,
            "update": self._update,
            "forecast": forecast_depletion(self._limits),
            "limits": self._limits,
            "now": now.strftime("%H:%M"),
        }

    def play_alert(self, level):
        """Toca um soundbite local para o patamar (80/95). True se tocou."""
        import random
        options = [SOUNDS_DIR / f for f in ALERT_SOUNDS.get(int(level), [])]
        options = [p for p in options if p.exists()]
        if not options:
            return False
        path = str(random.choice(options))
        threading.Thread(target=_play_mp3, args=(path,), daemon=True).start()
        return True

    def export_data(self):
        """Exporta agregados para JSON + CSV (Downloads ou pasta do script)."""
        import csv
        recs = self.store.records()
        by_day_model = defaultdict(lambda: {"in": 0, "out": 0, "cw": 0, "cr": 0, "cost": 0.0})
        for r in recs:
            k = (r["day"], short_model(r["model"]), r["proj"])
            m = by_day_model[k]
            for f in ("in", "out", "cw", "cr"):
                m[f] += r[f]
            m["cost"] += cost_of(r)
        rows = [{"day": d, "model": mo, "project": p, **{k2: (round(v2, 4) if k2 == "cost" else v2) for k2, v2 in v.items()}}
                for (d, mo, p), v in sorted(by_day_model.items())]
        dest = Path.home() / "Downloads"
        if not dest.is_dir():
            dest = Path(__file__).parent
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        jpath = dest / f"claude-pulse-export-{stamp}.json"
        cpath = dest / f"claude-pulse-export-{stamp}.csv"
        jpath.write_text(json.dumps(rows, indent=1), encoding="utf-8")
        with open(cpath, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["day", "model", "project", "in", "out", "cw", "cr", "cost"])
            w.writeheader()
            w.writerows(rows)
        return str(dest)

    def notify(self, title, msg):
        if self.tray:
            self.tray.notify(str(title), str(msg))
        return True

    def speak(self, text, lang):
        # False -> a UI usa o fallback speechSynthesis local
        if importlib.util.find_spec("edge_tts") is None:
            return False
        threading.Thread(target=_speak_worker, args=(str(text), str(lang)), daemon=True).start()
        return True

    def resize(self, w, h):
        if self.window:
            self.window.resize(int(w), int(h))

    def quit(self):
        if self.window:
            self.window.destroy()


def main():
    # impede o WebView2 de suspender timers JS em background/apos sleep
    os.environ.setdefault(
        "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
        "--disable-background-timer-throttling "
        "--disable-backgrounding-occluded-windows --disable-renderer-backgrounding")
    api = Api()
    ui = Path(__file__).parent / "ui" / "index.html"
    try:
        import ctypes
        sw = ctypes.windll.user32.GetSystemMetrics(0)
        sh = ctypes.windll.user32.GetSystemMetrics(1)
        x, y = sw - 400, sh - 360
    except Exception:
        x, y = 100, 100
    win = webview.create_window(
        "Claude Pulse", str(ui),
        width=372, height=288, x=x, y=y,
        frameless=True, easy_drag=True, on_top=True,
        background_color="#16130F",
    )
    api.window = win

    def get_stats():
        return api.get_stats()

    def resize(w, h):
        api.resize(w, h)

    def quit():
        api.quit()

    def speak(text, lang):
        return api.speak(text, lang)

    def play_alert(level):
        return api.play_alert(level)

    def notify(title, msg):
        return api.notify(title, msg)

    def export_data():
        return api.export_data()

    win.expose(get_stats, resize, quit, speak, play_alert, notify, export_data)

    visible = {"v": True}

    def toggle_window():
        try:
            if visible["v"]:
                win.hide()
            else:
                win.show()
            visible["v"] = not visible["v"]
        except Exception:
            pass

    api.tray = Tray(on_toggle=toggle_window, on_quit=lambda: api.quit())

    def _heartbeat():
        # watchdog: acorda a UI mesmo que os timers JS tenham sido suspensos
        while True:
            time.sleep(60)
            try:
                win.evaluate_js("window.__wake && window.__wake()")
            except Exception:
                pass

    def _start(w):
        api.tray.start()
        threading.Thread(target=_heartbeat, daemon=True).start()

    webview.start(_start, win)


if __name__ == "__main__":
    main()
