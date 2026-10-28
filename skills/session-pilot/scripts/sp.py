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
  sp.py gc       [--days N]                     purge memory of deleted chats/projects
  sp.py forget-project [PATH]                   delete all memory for a project
  sp.py import-project [PATH] [--adopt]         read instructions, wire rules & reconcile
  sp.py reconcile [PATH] [--adopt]              compare project skills vs system skills
  sp.py style    [set "text"]                   global default style
"""
import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

VERSION = "1.2.0"
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
    ("skills", "skill"), (".skills", "skill"),
]

INSTRUCTION_FILES = [
    "AGENTS.md", "CLAUDE.md", "GEMINI.md", ".cursorrules",
    ".windsurfrules", ".github/copilot-instructions.md", "README.md"
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


def primary_global_skills_dir():
    """Return primary destination for newly adopted global skills."""
    candidates = [
        Path.home() / ".gemini" / "config" / "skills",
        Path.home() / ".claude" / "skills",
        Path.home() / ".cursor" / "skills",
        Path.home() / ".codex" / "skills",
        Path.home() / ".agents" / "skills",
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return candidates[0]


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
        name_hits = [t for t in q if t in names]
        if name_hits:
            s *= (1.0 + 0.8 * len(name_hits))
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


def strength(best, q=None):
    if not best:
        return "none"
    s, cov, it = best[0]
    names = set(tokens(it["n"].replace("-", " ").replace("_", " ")))
    has_name_hit = any(t in names for t in (q or []))

    if (has_name_hit and s >= 6.5) or (cov >= 0.35 and s >= 8.0) or (s >= 11.0):
        return "strong"
    if (has_name_hit and s >= 3.0) or (cov >= 0.20 and s >= 3.5) or (s >= 5.0):
        return "weak"
    return "none"


# ------------------------------------------------------------------- memory
def detect_active_conversation():
    """Detect active conversation ID from environment or client transcript folders."""
    for ev in ("SESSION_PILOT_ID", "CONVERSATION_ID", "SESSION_ID", "CHAT_ID"):
        v = os.environ.get(ev)
        if v:
            return v, None

    # Check Antigravity / Gemini brain directory
    brain_root = Path.home() / ".gemini" / "antigravity" / "brain"
    if brain_root.is_dir():
        candidates = []
        now = time.time()
        for d in brain_root.iterdir():
            if d.is_dir() and len(d.name) == 36 and d.name.count("-") == 4:
                tfile = d / ".system_generated" / "logs" / "transcript.jsonl"
                mtime = tfile.stat().st_mtime if tfile.is_file() else d.stat().st_mtime
                if now - mtime < 900:  # Active in the last 15m
                    candidates.append((mtime, d.name, str(d)))
        if candidates:
            candidates.sort(key=lambda x: -x[0])
            return candidates[0][1], candidates[0][2]

    # Check Claude Code sessions
    claude_sessions = Path.home() / ".claude" / "sessions"
    if claude_sessions.is_dir():
        recent = sorted(claude_sessions.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        if recent and (time.time() - recent[0].stat().st_mtime < 900):
            return recent[0].stem, str(recent[0])

    return None, None


def session_id(arg):
    if arg:
        return re.sub(r"[^A-Za-z0-9._-]", "_", arg)[:120]
    if not os.environ.get("SESSION_PILOT_NO_AUTO_CONV"):
        cid, _ = detect_active_conversation()
        if cid:
            return re.sub(r"[^A-Za-z0-9._-]", "_", cid)[:120]
    return "auto-" + hashlib.sha1(os.getcwd().lower().encode()).hexdigest()[:12]


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


def gc_sessions(days=None):
    """Purge sessions whose project directories or conversation transcripts no longer exist."""
    pruned = []
    now = time.time()
    cutoff = (now - days * 86400) if days else None
    brain_root = Path.home() / ".gemini" / "antigravity" / "brain"

    for f in list(SESS_DIR.glob("*.json")):
        s = load_json(f, None)
        if not s or not isinstance(s, dict):
            try:
                f.unlink()
                pruned.append((f.stem, "corrupt"))
            except OSError:
                pass
            continue

        if cutoff and s.get("updated", s.get("created", 0)) < cutoff:
            try:
                f.unlink()
                pruned.append((f.stem, "expired"))
            except OSError:
                pass
            continue

        cwd = s.get("cwd")
        if cwd and not Path(cwd).exists():
            try:
                f.unlink()
                pruned.append((f.stem, f"project deleted ({cwd})"))
            except OSError:
                pass
            continue

        bdir = s.get("brain_dir")
        if bdir and not Path(bdir).exists():
            try:
                f.unlink()
                pruned.append((f.stem, f"chat deleted ({bdir})"))
            except OSError:
                pass
            continue

        sid = s.get("id", f.stem)
        if brain_root.is_dir() and len(sid) == 36 and sid.count("-") == 4:
            if not (brain_root / sid).exists():
                try:
                    f.unlink()
                    pruned.append((f.stem, "chat deleted"))
                except OSError:
                    pass
                continue

    return pruned


# -------------------------------------------------- project & skills analysis
def scan_project_instructions(project_dir):
    """Find and summarize instructions from standard project rule files."""
    pdir = Path(project_dir).resolve()
    found = []
    for rel in INSTRUCTION_FILES:
        path = pdir / rel
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                lines = [line.strip() for line in text.splitlines()
                         if line.strip() and not line.startswith("<!--") and not line.startswith("-->")]
                summary = " ".join(lines[:6])
                found.append({"file": rel, "path": str(path), "summary": short(summary, 220), "size": len(text)})
            except OSError:
                pass
    crules = pdir / ".cursor" / "rules"
    if crules.is_dir():
        for mdc in sorted(crules.glob("*.mdc")):
            try:
                text = mdc.read_text(encoding="utf-8", errors="replace")
                fm = frontmatter(mdc)
                desc = fm.get("description") or short(" ".join(text.splitlines()[:4]), 120)
                found.append({"file": f".cursor/rules/{mdc.name}", "path": str(mdc), "summary": desc, "size": len(text)})
            except OSError:
                pass
    return found


def skill_capability_score(skill_entry):
    """Compute capability score based on scripts, references, tests, and instruction depth."""
    p = Path(skill_entry.get("p", ""))
    s_dir = p.parent if p.name == "SKILL.md" else p
    score = 10
    if not s_dir.is_dir():
        return score
    scripts = s_dir / "scripts"
    if scripts.is_dir():
        script_files = list(scripts.glob("*.py")) + list(scripts.glob("*.sh")) + list(scripts.glob("*.js"))
        score += 20 + len(script_files) * 5
    for sub in ("references", "examples", "resources"):
        if (s_dir / sub).is_dir():
            score += 10
    tests = s_dir / "tests"
    if tests.is_dir():
        score += 15
    try:
        size = p.stat().st_size
        score += min(30, int(size / 300))
    except OSError:
        pass
    return score


def reconcile_project_skills(project_dir, auto_adopt=False):
    """Compare project-local skills with globally installed skills.
    Decide whether to implement more capable global skills or adopt project skills.
    """
    pdir = Path(project_dir).resolve()
    project_skills = []
    seen = set()
    for rel_root, kind in PROJECT_ROOTS:
        root = pdir / rel_root
        if root.is_dir():
            for e in scan_root(root, kind):
                if e["n"] not in seen:
                    seen.add(e["n"])
                    project_skills.append(e)

    if not project_skills:
        return []

    system_items = [it for it in build_index(cwd=str(pdir)) if it.get("s") in ("global", "extra")]
    results = []
    target_global_dir = primary_global_skills_dir()

    for ps in project_skills:
        p_score = skill_capability_score(ps)
        q, best = rank(system_items, f"{ps['n']} {ps['d']}", k=3)
        match_level = strength(best)

        decision = "adopt_project"
        matching_global = None
        reason = "no matching installed skill"

        if best and match_level in ("strong", "weak"):
            top_global = best[0][2]
            g_score = skill_capability_score(top_global)
            matching_global = top_global
            if g_score > p_score + 5:
                decision = "use_global"
                reason = f"global skill {top_global['n']} is more capable (score {g_score} vs {p_score})"
            elif p_score >= g_score:
                decision = "adopt_project"
                reason = f"project skill has specialized project instructions (score {p_score} vs {g_score})"
            else:
                decision = "use_global"
                reason = f"using installed {top_global['n']}"
        else:
            decision = "adopt_project"
            reason = "no installed skill fits domain; adopting project skill"

        adopted_path = None
        if decision == "adopt_project" and auto_adopt:
            src = Path(ps["p"]).parent if Path(ps["p"]).name == "SKILL.md" else Path(ps["p"])
            dest = target_global_dir / ps["n"]
            try:
                target_global_dir.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(src, dest)
                else:
                    dest.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest / src.name)
                adopted_path = str(dest)
                clog = load_json(CHANGELOG_FILE, [])
                clog.insert(0, {"t": time.time(), "added": [ps["n"]], "removed": [],
                                "note": f"Adopted from project {pdir.name}"})
                atomic_write(CHANGELOG_FILE, clog[:50])
            except Exception as e:
                reason += f" (adopt error: {e})"

        results.append({
            "name": ps["n"],
            "project_path": ps["p"],
            "decision": decision,
            "reason": reason,
            "global_match": matching_global["n"] if matching_global else None,
            "global_path": matching_global["p"] if matching_global else None,
            "adopted_path": adopted_path
        })

    if any(r.get("adopted_path") for r in results):
        build_index(rebuild=True)

    return results


# ------------------------------------------------------------------ commands
def cmd_recall(a):
    sid = session_id(a.session)
    s = load_session(sid)
    if s:
        # Check if project or chat was deleted
        cwd = s.get("cwd")
        if cwd and not Path(cwd).exists():
            f = SESS_DIR / f"{sid}.json"
            if f.exists():
                try:
                    f.unlink()
                except OSError:
                    pass
            s = None
        bdir = s.get("brain_dir") if s else None
        if bdir and not Path(bdir).exists():
            f = SESS_DIR / f"{sid}.json"
            if f.exists():
                try:
                    f.unlink()
                except OSError:
                    pass
            s = None

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
    cwd_str = str(Path(getattr(a, "cwd", None) or os.getcwd()).resolve())
    s = load_session(sid) or {"id": sid, "created": now, "checkpoints": [], "skills": [],
                              "history": [], "cwd": cwd_str}
    s["cwd"] = cwd_str
    conv_id = getattr(a, "conv_id", None) or os.environ.get("CONVERSATION_ID") or os.environ.get("SESSION_PILOT_CONV_ID")
    if conv_id:
        s["conv_id"] = conv_id
    brain_root = Path.home() / ".gemini" / "antigravity" / "brain"
    if brain_root.is_dir():
        for candidate in (conv_id, sid):
            if candidate and (brain_root / candidate).is_dir():
                s["brain_dir"] = str(brain_root / candidate)
                break

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
    st = strength(best, q)
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


def cmd_forget_project(a):
    target = str(Path(a.path or os.getcwd()).resolve()).lower()
    pruned = []
    for f in list(SESS_DIR.glob("*.json")):
        s = load_json(f, {})
        cwd = s.get("cwd")
        if cwd and str(Path(cwd).resolve()).lower() == target:
            try:
                f.unlink()
                pruned.append(f.stem)
            except OSError:
                pass
    if pruned:
        out(f"forgot {len(pruned)} session(s) for project {target}")
    else:
        out(f"no memory found for project {target}")


def cmd_prune(a):
    cutoff, n = time.time() - a.days * 86400, 0
    for f in SESS_DIR.glob("*.json"):
        if load_json(f, {}).get("updated", 0) < cutoff:
            f.unlink()
            n += 1
    out(f"pruned {n}")


def cmd_gc(a):
    pruned = gc_sessions(days=a.days)
    if not pruned:
        out("clean: no orphaned or deleted session memory")
    else:
        out(f"pruned {len(pruned)} session(s):")
        for sid, reason in pruned[:10]:
            out(f"  - {sid}: {reason}")
        if len(pruned) > 10:
            out(f"  ... and {len(pruned)-10} more")


def cmd_reconcile(a):
    pdir = Path(a.path or os.getcwd()).resolve()
    reconciled = reconcile_project_skills(pdir, auto_adopt=a.adopt)
    if not reconciled:
        out(f"No project-local skills found in {pdir}")
        return
    out(f"Reconciled {len(reconciled)} project skill(s) in {pdir.name}:")
    for r in reconciled:
        if r["decision"] == "use_global":
            out(f"- {r['name']}: [USE GLOBAL] {r['global_match']}")
            out(f"  {r['reason']}")
            out(f"  Path: {r['global_path']}")
        else:
            adopted = f" (installed to {r['adopted_path']})" if r.get("adopted_path") else ""
            out(f"- {r['name']}: [ADOPT PROJECT]{adopted}")
            out(f"  {r['reason']}")
            out(f"  Path: {r['project_path']}")


def cmd_import_project(a):
    pdir = Path(a.path or os.getcwd()).resolve()
    if not pdir.is_dir():
        out(f"error: project directory does not exist: {pdir}")
        return

    out(f"PROJECT: {pdir.name} ({pdir})")

    # 1. Project instructions
    instructions = scan_project_instructions(pdir)
    if instructions:
        out("Instructions found:")
        for inst in instructions:
            out(f"  - {inst['file']} ({inst['size']} bytes): {inst['summary']}")
    else:
        out("Instructions: No dedicated instruction files found (proceeding with standard pilot)")

    # 2. Wire Session Pilot into project if needed
    agents_md = pdir / "AGENTS.md"
    bootstrap_snippet = (
        "\n<!-- session-pilot:start -->\n"
        "## Session Pilot (always on)\n\n"
        "At the start of every chat, and on each new prompt, follow the `session-pilot` skill:\n"
        "recall this chat's memory, answer simple questions directly in lean mode, route\n"
        "tasks to the best installed skill (`sp.py find`), mirror the user's tone, and\n"
        "checkpoint progress (`sp.py save`) after meaningful steps.\n"
        "<!-- session-pilot:end -->\n"
    )
    if not agents_md.exists():
        if a.wire:
            try:
                agents_md.write_text("# Project Guidelines\n" + bootstrap_snippet, encoding="utf-8")
                out("Session Pilot: wired into new AGENTS.md")
            except OSError:
                pass
    else:
        try:
            content = agents_md.read_text(encoding="utf-8", errors="replace")
            if "session-pilot:start" not in content and a.wire:
                agents_md.write_text(content.rstrip() + "\n" + bootstrap_snippet, encoding="utf-8")
                out("Session Pilot: added always-on rule to AGENTS.md")
        except OSError:
            pass

    # 3. Reconcile project skills vs global skills
    reconciled = reconcile_project_skills(pdir, auto_adopt=a.adopt)
    if reconciled:
        out("Project Skills Reconciliation:")
        for r in reconciled:
            if r["decision"] == "use_global":
                out(f"  * {r['name']}: [USE GLOBAL] -> {r['global_match']}")
                out(f"    Reason: {r['reason']}")
            else:
                adopt_note = f" -> adopted to {r['adopted_path']}" if r.get("adopted_path") else ""
                out(f"  * {r['name']}: [ADOPT PROJECT]{adopt_note}")
                out(f"    Reason: {r['reason']}")
    else:
        out("Skills: No project-local skills found (using system skills)")

    # 4. Save session checkpoint marking project imported
    sid = session_id(a.session)
    now = time.time()
    s = load_session(sid) or {"id": sid, "created": now, "checkpoints": [], "skills": [],
                              "history": [], "cwd": str(pdir)}
    s["cwd"] = str(pdir)
    s["title"] = f"Project {pdir.name}"
    s["checkpoints"].append({
        "t": now,
        "done": f"Imported project {pdir.name} and initialized instructions",
        "next": "Implement project instructions and commence task"
    })
    s["updated"] = now
    atomic_write(SESS_DIR / f"{sid}.json", s)
    out(f"Project imported and ready. Session memory initialized ({sid}).")


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
    last_gc = time.time()
    try:
        while True:
            now = time.time()
            atomic_write(HEARTBEAT_FILE, {"t": now, "pid": pid})
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
                # Periodic GC for deleted projects / chats
                if now - last_gc > 60:
                    pruned = gc_sessions()
                    if pruned:
                        log_daemon(f"Garbage collected {len(pruned)} deleted session(s)")
                    last_gc = now
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
    s.add_argument("--conv-id")
    s.add_argument("--cwd")
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

    fp = sub.add_parser("forget-project")
    fp.add_argument("path", nargs="?", default=None)
    fp.set_defaults(fn=cmd_forget_project)

    pr = sub.add_parser("prune")
    pr.add_argument("--days", type=int, default=90)
    pr.set_defaults(fn=cmd_prune)

    gc_p = sub.add_parser("gc")
    gc_p.add_argument("--days", type=int, default=None)
    gc_p.set_defaults(fn=cmd_gc)

    st = sub.add_parser("style")
    st.add_argument("text", nargs="*")
    st.set_defaults(fn=cmd_style)

    pf = sub.add_parser("prefer")
    pf.add_argument("optimizer")
    pf.set_defaults(fn=cmd_prefer)

    rc = sub.add_parser("reconcile")
    rc.add_argument("path", nargs="?", default=None)
    rc.add_argument("--adopt", action="store_true")
    rc.set_defaults(fn=cmd_reconcile)

    ip = sub.add_parser("import-project")
    ip.add_argument("path", nargs="?", default=None)
    ip.add_argument("--session")
    ip.add_argument("--wire", action="store_true", default=True)
    ip.add_argument("--no-wire", dest="wire", action="store_false")
    ip.add_argument("--adopt", action="store_true")
    ip.set_defaults(fn=cmd_import_project)

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
