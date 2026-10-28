#!/usr/bin/env python3
"""session-pilot helper: skill scout + per-session memory + background indexer.

Stdlib only, Python 3.8+. Output is deliberately terse to save tokens.

  sp.py recall   [--session ID]                 print this session's memory
  sp.py save     [--session ID] [--goal ..] [--done ..] [--next ..]
                 [--skill NAME ...] [--style ..] [--title ..] [--status ..]
  sp.py find     "task text" [-k 5]             rank skills/agents/plugins
  sp.py optimizers                              list installed token-saver skills
  sp.py index    [--rebuild]                    refresh the capability index
  sp.py roots                                   show scanned directories
  sp.py watch    [--interval 10]                foreground filesystem watcher
  sp.py daemon   [start|stop|status]            background indexer service
  sp.py changelog [-k 5]                        recent skill additions/removals
  sp.py sessions | forget ID | prune --days N   manage memory
  sp.py style    [set "text"]                   global default style
"""
import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

VERSION = "1.1.0"
HOME = Path(os.environ.get("SESSION_PILOT_HOME") or (Path.home() / ".session-pilot"))
INDEX_FILE = HOME / "index.json"
SESS_DIR = HOME / "sessions"
PROFILE = HOME / "profile.json"
CHANGELOG_FILE = HOME / "changelog.json"
DAEMON_PID_FILE = HOME / "daemon.pid"
HEARTBEAT_FILE = HOME / "daemon.heartbeat"
INDEX_MAX_AGE = 24 * 3600
MAX_CHECKPOINTS = 8
HISTORY_CHARS = 1200

# (path, kind). Missing paths are skipped silently.
GLOBAL_ROOTS = [
    ("~/.gemini/config/skills", "skill"),
    ("~/.gemini/antigravity/builtin/skills", "skill"),
    ("~/.gemini/skills", "skill"),
    ("~/.gemini/config/plugins", "plugin"),
    ("~/.claude/skills", "skill"),
    ("~/.claude/agents", "agent"),
    ("~/.claude/plugins", "plugin"),
    ("~/.codex/skills", "skill"),
    ("~/.codex/agents", "agent"),
    ("~/.agents/skills", "skill"),
    ("~/.cursor/skills", "skill"),
    ("~/.copilot/skills", "skill"),
    ("~/.config/opencode/skills", "skill"),
    ("~/.config/opencode/agent", "agent"),
    ("~/.qwen/agents", "agent"),
]
PROJECT_ROOTS = [
    (".agents/skills", "skill"), (".agent/skills", "skill"),
    (".claude/skills", "skill"), (".claude/agents", "agent"),
    (".github/skills", "skill"), (".codex/skills", "skill"),
    (".cursor/skills", "skill"), (".gemini/skills", "skill"),
]

STOP = set("""a an the and or but if then so to of in on at by for with from into onto
about as is are was were be been being do does did done have has had i me my we our you
your it its this that these those there here please can could would should will shall
may might must just also very really some any all each every more most other such than
too not no yes ok okay hey hi hello thanks thank want need like make get got use using
help how what when where which who why whom whose new one two lets let im ive id dont""".split())

OPTIMIZER_TERMS = ["token", "compress", "compact", "context budget", "prompt optim",
                   "caveman", "rtk", "summariz"]


# ----------------------------------------------------------------- utilities
def out(s=""):
    if sys.stdout:
        sys.stdout.write(s + "\n")


def stem(w):
    for suf in ("ing", "ers", "er", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def tokens(text):
    return [stem(w) for w in re.findall(r"[a-z0-9]+", text.lower())
            if w not in STOP and len(w) > 1]


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


# ------------------------------------------------------------- daemon/process
def is_pid_alive(pid):
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.windll.kernel32
            handle = k32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            code = wintypes.DWORD()
            k32.GetExitCodeProcess(handle, ctypes.byref(code))
            k32.CloseHandle(handle)
            return code.value == 259  # STILL_ACTIVE
        except Exception:
            return False
    else:
        try:
            os.kill(int(pid), 0)
            return True
        except (OSError, ProcessLookupError, ValueError):
            return False


def kill_pid(pid):
    if sys.platform == "win32":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            handle = k32.OpenProcess(1, False, int(pid))
            if handle:
                k32.TerminateProcess(handle, 0)
                k32.CloseHandle(handle)
        except Exception:
            pass
    else:
        try:
            os.kill(int(pid), 15)  # SIGTERM
        except (OSError, ProcessLookupError, ValueError):
            pass


def daemon_status():
    hb_data = load_json(HEARTBEAT_FILE, {})
    pid_data = load_json(DAEMON_PID_FILE, {})
    pid = pid_data.get("pid")
    last_hb = hb_data.get("t", 0)
    age = time.time() - last_hb if last_hb else 999999
    if pid and is_pid_alive(pid) and age < 45:
        return True, pid, age
    return False, pid, age


# --------------------------------------------------------------- frontmatter
def frontmatter(path):
    """Return {'name','description'} from a markdown file's YAML frontmatter."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            head = fh.read(6000)
    except OSError:
        return {}
    if not head.startswith("---"):
        return {}
    end = head.find("\n---", 3)
    lines = head[3:end if end != -1 else len(head)].splitlines()
    res, i = {}, 0
    while i < len(lines):
        m = re.match(r"^(name|description):\s*(.*)$", lines[i])
        if not m:
            i += 1
            continue
        key, val = m.group(1), m.group(2).strip()
        i += 1
        if val in (">", ">-", ">+", "|", "|-", "|+", ""):
            parts = []
            while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                parts.append(lines[i].strip())
                i += 1
            val = " ".join(p for p in parts if p)
        else:
            while i < len(lines) and re.match(r"^[ \t]+\S", lines[i]):
                val += " " + lines[i].strip()
                i += 1
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        res[key] = val
    return res


# --------------------------------------------------------------------- roots
def all_roots(cwd=None):
    roots = []
    if not os.environ.get("SESSION_PILOT_NO_DEFAULT_ROOTS"):
        roots += [(Path(os.path.expanduser(p)), k, "global") for p, k in GLOBAL_ROOTS]
        base = Path(cwd or os.getcwd()).resolve()
        for d in [base] + list(base.parents)[:6]:
            for p, k in PROJECT_ROOTS:
                roots.append((d / p, k, "project"))
            if (d / ".git").exists():
                break
    for extra in filter(None, os.environ.get("SESSION_PILOT_ROOTS", "").split(os.pathsep)):
        kind = "skill"
        if "=" in extra and extra.split("=", 1)[0] in ("skill", "agent", "plugin"):
            kind, extra = extra.split("=", 1)
        roots.append((Path(os.path.expanduser(extra)), kind, "extra"))
    seen, uniq = set(), []
    for p, k, s in roots:
        key = (str(p).lower(), k)
        if key not in seen and p.is_dir():
            seen.add(key)
            uniq.append((p, k, s))
    return uniq


def scan_root(root, kind):
    entries = []
    if kind == "agent":
        for f in sorted(root.glob("*.md")):
            fm = frontmatter(f)
            if fm.get("name") or fm.get("description"):
                entries.append({"n": fm.get("name") or f.stem, "d": fm.get("description", ""),
                                "k": "agent", "p": str(f)})
        return entries
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if kind == "plugin":
            pj = next((c for c in (child / "plugin.json", child / ".claude-plugin" / "plugin.json")
                       if c.is_file()), None)
            if pj:
                meta = load_json(pj, {})
                entries.append({"n": meta.get("name", child.name), "d": meta.get("description", ""),
                                "k": "plugin", "p": str(pj)})
            sub = child / "skills"
            if sub.is_dir():
                entries += scan_root(sub, "skill")
            continue
        sk = child / "SKILL.md"
        if sk.is_file():
            fm = frontmatter(sk)
            entries.append({"n": fm.get("name") or child.name, "d": fm.get("description", ""),
                            "k": "skill", "p": str(sk)})
        else:  # one nested level, e.g. document-skills/pdf/SKILL.md
            for g in child.iterdir():
                if g.is_dir() and (g / "SKILL.md").is_file():
                    fm = frontmatter(g / "SKILL.md")
                    entries.append({"n": fm.get("name") or g.name, "d": fm.get("description", ""),
                                    "k": "skill", "p": str(g / "SKILL.md")})
    return entries


def check_for_changes():
    """Scan roots, detect added/removed skills, update index and changelog."""
    cache = load_json(INDEX_FILE, {})
    if cache.get("v") != VERSION:
        cache = {}
    old_roots = cache.get("roots", {})
    now = time.time()
    fresh = {}
    changed = False

    old_names = set()
    for rdata in old_roots.values():
        for e in rdata.get("e", []):
            old_names.add(e["n"])

    new_names = set()
    for root, kind, scope in all_roots():
        key = f"{kind}|{root}"
        try:
            mtime = root.stat().st_mtime
        except OSError:
            continue
        prev = old_roots.get(key)
        if prev and prev.get("m") == mtime and now - prev.get("t", 0) < INDEX_MAX_AGE:
            ents = prev["e"]
        else:
            ents = scan_root(root, kind)
            changed = True
            prev = {"m": mtime, "t": now, "e": ents}
        fresh[key] = prev
        for e in ents:
            new_names.add(e["n"])

    if changed or set(fresh) != set(old_roots):
        atomic_write(INDEX_FILE, {"v": VERSION, "roots": fresh})
        added = sorted(list(new_names - old_names))
        removed = sorted(list(old_names - new_names))
        if added or removed:
            clog = load_json(CHANGELOG_FILE, [])
            clog.insert(0, {"t": now, "added": added, "removed": removed})
            clog = clog[:50]
            atomic_write(CHANGELOG_FILE, clog)
            return added, removed
    return [], []


def build_index(rebuild=False, cwd=None):
    # If background daemon is actively updating and index exists, use pre-warmed cache
    running, _, _ = daemon_status()
    if not rebuild and cwd is None and running and INDEX_FILE.exists():
        cache = load_json(INDEX_FILE, {})
        if cache.get("v") == VERSION and cache.get("roots"):
            items = []
            for root_data in cache["roots"].values():
                for e in root_data.get("e", []):
                    items.append(dict(e, s="global"))
            if items:
                return items

    cache = {} if rebuild else load_json(INDEX_FILE, {})
    if cache.get("v") != VERSION:
        cache = {}
    old = cache.get("roots", {})
    now = time.time()
    fresh, items, changed = {}, [], False
    for root, kind, scope in all_roots(cwd):
        key = f"{kind}|{root}"
        try:
            mtime = root.stat().st_mtime
        except OSError:
            continue
        prev = old.get(key)
        if prev and prev.get("m") == mtime and now - prev.get("t", 0) < INDEX_MAX_AGE:
            ents = prev["e"]
        else:
            ents, changed = scan_root(root, kind), True
            prev = {"m": mtime, "t": now, "e": ents}
        fresh[key] = prev
        for e in ents:
            items.append(dict(e, s=scope))
    if changed or set(fresh) != set(old):
        atomic_write(INDEX_FILE, {"v": VERSION, "roots": fresh})
    return items


# -------------------------------------------------------------------- search
SCOPE_RANK = {"project": 0, "extra": 1, "global": 2}


def rank(items, query, k=5):
    q = list(dict.fromkeys(tokens(query)))
    if not q or not items:
        return q, []
    docs = []
    for it in items:
        name_t = tokens(it["n"].replace("-", " ").replace("_", " "))
        body = name_t * 3 + tokens(it["d"])
        docs.append((it, body, set(name_t)))
    n = len(docs)
    avg = sum(len(b) for _, b, _ in docs) / n or 1.0
    df = {t: sum(1 for _, b, _ in docs if t in b) for t in q}
    qlow = query.lower()
    scored = []
    for it, body, names in docs:
        tf = {}
        for t in body:
            if t in df:
                tf[t] = tf.get(t, 0) + 1
        if not tf:
            continue
        s = 0.0
        for t, f in tf.items():
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * len(body) / avg))
        if it["n"].lower() in qlow or it["n"].lower().replace("-", " ") in qlow:
            s *= 1.5
        cov = len(tf) / len(q)
        scored.append((s * (0.5 + cov), cov, it))
    scored.sort(key=lambda x: (-x[0], SCOPE_RANK.get(x[2]["s"], 3), x[2]["n"]))
    best, seen = [], set()
    for s, cov, it in scored:
        key = it["n"].lower()
        if key in seen:
            continue
        seen.add(key)
        best.append((s, cov, it))
        if len(best) >= k:
            break
    return q, best


def strength(best):
    if not best:
        return "none"
    s, cov, _ = best[0]
    if cov >= 0.5 and s >= 10:
        return "strong"
    if cov >= 0.3 and s >= 5:
        return "weak"
    return "none"


# ------------------------------------------------------------------- memory
def session_id(arg):
    sid = arg or os.environ.get("SESSION_PILOT_ID")
    if not sid:
        sid = "auto-" + hashlib.sha1(os.getcwd().lower().encode()).hexdigest()[:12]
    return re.sub(r"[^A-Za-z0-9._-]", "_", sid)[:120]


def load_session(sid):
    return load_json(SESS_DIR / f"{sid}.json", None)


def short(text, limit=240):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def ago(ts):
    d = int(time.time() - ts)
    for unit, sec in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= sec:
            return f"{d // sec}{unit} ago"
    return "just now"


# ------------------------------------------------------------------ commands
def cmd_recall(a):
    sid = session_id(a.session)
    s = load_session(sid)
    style = (s or {}).get("style") or load_json(PROFILE, {}).get("style")
    if not s:
        out(f"NEW SESSION {sid}")
        if style:
            out(f"Style: {style}")
        return
    out(f"SESSION {sid} | {s.get('status', 'active')} | updated {ago(s['updated'])}")
    for label, key in (("Title", "title"), ("Goal", "goal"), ("Style", None)):
        val = style if key is None else s.get(key)
        if val:
            out(f"{label}: {val}")
    if s.get("skills"):
        out("Skills: " + ", ".join(s["skills"]))
    if s.get("history"):
        out("Earlier: " + " | ".join(s["history"]))
    for cp in s.get("checkpoints", [])[-a.last:]:
        line = f"- [{ago(cp['t'])}]"
        if cp.get("done"):
            line += f" done: {cp['done']}"
        if cp.get("next"):
            line += f" | next: {cp['next']}"
        out(line)

    # Check for newly installed skills since this session was last active
    last_up = s.get("updated", s.get("created", 0))
    clog = load_json(CHANGELOG_FILE, [])
    recent_new = []
    for entry in clog:
        if entry.get("t", 0) > last_up:
            recent_new.extend(entry.get("added", []))
    if recent_new:
        uniq = list(dict.fromkeys(recent_new))
        sample = ", ".join(uniq[:4])
        if len(uniq) > 4:
            sample += f" (+{len(uniq)-4} more)"
        out(f"New skills installed since last checkpoint: {sample}")


def cmd_save(a):
    sid = session_id(a.session)
    now = time.time()
    s = load_session(sid) or {"id": sid, "created": now, "checkpoints": [], "skills": [],
                              "history": [], "cwd": os.getcwd()}
    for key in ("title", "goal", "style", "status"):
        val = getattr(a, key)
        if val:
            s[key] = short(val, 300)
    for sk in a.skill or []:
        if sk not in s["skills"]:
            s["skills"].append(sk)
    s["skills"] = s["skills"][-12:]
    if a.done or a.next:
        s["checkpoints"].append({"t": now, "done": short(a.done or ""), "next": short(a.next or "")})
    while len(s["checkpoints"]) > MAX_CHECKPOINTS:
        cp = s["checkpoints"].pop(0)
        if cp.get("done"):
            s["history"].append(short(cp["done"], 120))
    while sum(len(h) for h in s["history"]) > HISTORY_CHARS:
        s["history"].pop(0)
    s["updated"] = now
    atomic_write(SESS_DIR / f"{sid}.json", s)
    if a.style and a.global_style:
        prof = load_json(PROFILE, {})
        prof["style"] = short(a.style, 300)
        atomic_write(PROFILE, prof)
    out(f"saved {sid}")


def cmd_find(a):
    items = build_index(cwd=a.cwd)
    q, best = rank(items, a.query, a.k)
    if a.kind:
        best = [b for b in best if b[2]["k"] == a.kind]
    st = strength(best)
    out(f"match: {st} | indexed {len(items)} | terms: {' '.join(q) or '-'}")
    for i, (s, cov, it) in enumerate(best, 1):
        out(f"{i}. {it['n']} [{it['k']},{it['s']}] score={s:.1f} cov={cov:.0%}")
        out(f"   {short(it['d'], a.width)}")
        out(f"   {it['p']}")


def cmd_optimizers(a):
    """Skills that cut the agent's own context/tokens (not server-side tools)."""
    pref = load_json(PROFILE, {}).get("optimizer")
    items = build_index()
    hits = []
    for it in items:
        name, desc = it["n"].lower(), it["d"].lower()
        if pref and name == pref.lower():
            hits.append((99, it))
            continue
        if re.match(r"\s*(configure|manage|set up|install)\b", desc):
            continue
        text = name + " " + desc
        score = sum(text.count(t) for t in OPTIMIZER_TERMS)
        if score and re.search(r"compact|compress|context|token (budget|usage|saving|cost)"
                               r"|(sav|reduc|cut)\w* tokens", text):
            hits.append((score, it))
    hits.sort(key=lambda x: (-x[0], x[1]["n"]))
    if not hits:
        out("none")
    for score, it in hits[: a.k]:
        out(f"- {it['n']}: {short(it['d'], 110)}\n  {it['p']}")


def cmd_index(a):
    items = build_index(rebuild=a.rebuild)
    by = {}
    for it in items:
        by[it["k"]] = by.get(it["k"], 0) + 1
    out(f"indexed {len(items)} ({', '.join(f'{v} {k}s' for k, v in sorted(by.items()))}) -> {INDEX_FILE}")


def cmd_roots(a):
    for p, k, s in all_roots():
        out(f"{k:6} {s:7} {p}")


def cmd_sessions(a):
    files = sorted(SESS_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        out("no sessions")
    for f in files[: a.k]:
        s = load_json(f, {})
        out(f"{s.get('id', f.stem)} | {s.get('status', 'active')} | {ago(s.get('updated', 0))} | "
            f"{short(s.get('title') or s.get('goal') or '-', 70)}")


def cmd_forget(a):
    f = SESS_DIR / f"{session_id(a.session)}.json"
    if f.exists():
        f.unlink()
        out(f"forgot {f.stem}")
    else:
        out("no such session")


def cmd_prune(a):
    cutoff, n = time.time() - a.days * 86400, 0
    for f in SESS_DIR.glob("*.json"):
        if load_json(f, {}).get("updated", 0) < cutoff:
            f.unlink()
            n += 1
    out(f"pruned {n}")


def cmd_style(a):
    prof = load_json(PROFILE, {})
    if a.text:
        prof["style"] = short(" ".join(a.text), 300)
        atomic_write(PROFILE, prof)
    out(prof.get("style", "none"))


def cmd_prefer(a):
    """Pin a preferred token-optimizer skill (or clear with 'none')."""
    prof = load_json(PROFILE, {})
    if a.optimizer.lower() == "none":
        prof.pop("optimizer", None)
    else:
        prof["optimizer"] = a.optimizer
    atomic_write(PROFILE, prof)
    out(f"optimizer: {prof.get('optimizer', 'none')}")


def cmd_changelog(a):
    clog = load_json(CHANGELOG_FILE, [])
    if not clog:
        out("no recent changes recorded")
        return
    for c in clog[: a.k]:
        out(f"[{ago(c['t'])}]")
        if c.get("added"):
            out(f"  + added:   {', '.join(c['added'][:6])}" + (f" (+{len(c['added'])-6} more)" if len(c['added']) > 6 else ""))
        if c.get("removed"):
            out(f"  - removed: {', '.join(c['removed'][:6])}" + (f" (+{len(c['removed'])-6} more)" if len(c['removed']) > 6 else ""))


def log_daemon(msg):
    log_file = HOME / "daemon.log"
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def cmd_watch(a):
    interval = a.interval or 10
    pid = os.getpid()
    atomic_write(DAEMON_PID_FILE, {"pid": pid, "start": time.time()})
    log_daemon(f"Session Pilot watcher started (PID {pid}, interval {interval}s)")
    out(f"Session Pilot watcher running (PID {pid}, interval {interval}s)")
    try:
        while True:
            atomic_write(HEARTBEAT_FILE, {"t": time.time(), "pid": pid})
            try:
                added, removed = check_for_changes()
                if added or removed:
                    msg = f"Index updated:"
                    if added:
                        msg += f" +{len(added)} added ({', '.join(added[:3])})"
                    if removed:
                        msg += f" -{len(removed)} removed"
                    log_daemon(msg)
                    out(f"[{time.strftime('%H:%M:%S')}] {msg}")
            except Exception as e:
                log_daemon(f"Watcher scan error: {e}")
                out(f"Watcher scan error: {e}")
            time.sleep(interval)
    except KeyboardInterrupt:
        log_daemon("Watcher stopped by KeyboardInterrupt.")
        out("Watcher stopped.")
    except Exception as e:
        log_daemon(f"Watcher fatal error: {e}")
    finally:
        log_daemon(f"Watcher exiting (PID {pid})")
        for f in (DAEMON_PID_FILE, HEARTBEAT_FILE):
            if f.exists():
                try:
                    f.unlink()
                except OSError:
                    pass


def cmd_daemon(a):
    action = a.action
    running, pid, age = daemon_status()

    if action == "status":
        if running:
            out(f"daemon: RUNNING (PID {pid}, heartbeat {age:.0f}s ago)")
        else:
            out("daemon: STOPPED")
    elif action == "stop":
        if not running:
            out("daemon: not running")
            return
        kill_pid(pid)
        for f in (DAEMON_PID_FILE, HEARTBEAT_FILE):
            if f.exists():
                try:
                    f.unlink()
                except OSError:
                    pass
        out(f"daemon: stopped (PID {pid})")
    elif action == "start":
        if running:
            out(f"daemon: already running (PID {pid})")
            return
        script = Path(__file__).resolve()
        if sys.platform == "win32":
            pyw = Path(sys.executable).parent / "pythonw.exe"
            exe = str(pyw) if pyw.exists() else sys.executable
            DETACHED_PROCESS = 0x00000008
            CREATE_NO_WINDOW = 0x08000000
            subprocess.Popen([exe, str(script), "watch", "--interval", str(a.interval or 10)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
                             close_fds=True)
        else:
            subprocess.Popen([sys.executable, str(script), "watch", "--interval", str(a.interval or 10)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                             start_new_session=True)
        time.sleep(1.0)
        r, p, _ = daemon_status()
        if r:
            out(f"daemon: started in background (PID {p})")
        else:
            out("daemon: launched (will update heartbeat shortly)")


def main(argv=None):
    p = argparse.ArgumentParser(prog="sp.py", description="session-pilot helper")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("recall")
    r.add_argument("--session")
    r.add_argument("--last", type=int, default=3)
    r.set_defaults(fn=cmd_recall)

    s = sub.add_parser("save")
    s.add_argument("--session")
    for opt in ("title", "goal", "done", "next", "style", "status"):
        s.add_argument("--" + opt)
    s.add_argument("--skill", action="append")
    s.add_argument("--global-style", action="store_true")
    s.set_defaults(fn=cmd_save)

    f = sub.add_parser("find")
    f.add_argument("query")
    f.add_argument("-k", type=int, default=5)
    f.add_argument("--kind", choices=["skill", "agent", "plugin"])
    f.add_argument("--width", type=int, default=140)
    f.add_argument("--cwd")
    f.set_defaults(fn=cmd_find)

    o = sub.add_parser("optimizers")
    o.add_argument("-k", type=int, default=3)
    o.set_defaults(fn=cmd_optimizers)

    i = sub.add_parser("index")
    i.add_argument("--rebuild", action="store_true")
    i.set_defaults(fn=cmd_index)

    sub.add_parser("roots").set_defaults(fn=cmd_roots)

    w = sub.add_parser("watch")
    w.add_argument("--interval", type=int, default=10)
    w.set_defaults(fn=cmd_watch)

    dm = sub.add_parser("daemon")
    dm.add_argument("action", choices=["start", "stop", "status"])
    dm.add_argument("--interval", type=int, default=10)
    dm.set_defaults(fn=cmd_daemon)

    cl = sub.add_parser("changelog")
    cl.add_argument("-k", type=int, default=5)
    cl.set_defaults(fn=cmd_changelog)

    ls = sub.add_parser("sessions")
    ls.add_argument("-k", type=int, default=20)
    ls.set_defaults(fn=cmd_sessions)

    fg = sub.add_parser("forget")
    fg.add_argument("session")
    fg.set_defaults(fn=cmd_forget)

    pr = sub.add_parser("prune")
    pr.add_argument("--days", type=int, default=90)
    pr.set_defaults(fn=cmd_prune)

    st = sub.add_parser("style")
    st.add_argument("text", nargs="*")
    st.set_defaults(fn=cmd_style)

    pf = sub.add_parser("prefer")
    pf.add_argument("optimizer")
    pf.set_defaults(fn=cmd_prefer)

    a = p.parse_args(argv)
    if a.cmd == "style" and a.text and a.text[0] == "set":
        a.text = a.text[1:]
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
    a.fn(a)


if __name__ == "__main__":
    main()
