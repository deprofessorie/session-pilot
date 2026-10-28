#!/usr/bin/env python3
"""Install session-pilot into one or more AI clients.

  python install.py --client claude                 # copy skill (global)
  python install.py --client claude --with-rule     # + always-on bootstrap rule
  python install.py --client cursor --project .     # project-scoped clients
  python install.py --client all --dry-run          # every client found on this machine
  python install.py --client claude --uninstall
"""
import argparse
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
SKILL = REPO / "skills" / "session-pilot"
BOOT = REPO / "adapters" / "bootstrap.md"
MDC = REPO / "adapters" / "cursor" / "session-pilot.mdc"
START, END = "<!-- session-pilot:start -->", "<!-- session-pilot:end -->"
H = Path.home()

# client: (global skills dir, global rule file, project skills dir, project rule file, detect dir)
CLIENTS = {
    "claude":      (H / ".claude/skills", H / ".claude/CLAUDE.md", ".claude/skills", "CLAUDE.md", H / ".claude"),
    "antigravity": (H / ".gemini/config/skills", H / ".gemini/config/AGENTS.md", ".agents/skills", "AGENTS.md", H / ".gemini/antigravity"),
    "gemini":      (H / ".gemini/skills", H / ".gemini/GEMINI.md", ".gemini/skills", "GEMINI.md", H / ".gemini/settings.json"),
    "codex":       (H / ".codex/skills", H / ".codex/AGENTS.md", ".codex/skills", "AGENTS.md", H / ".codex"),
    "opencode":    (H / ".config/opencode/skills", H / ".config/opencode/AGENTS.md", ".opencode/skills", "AGENTS.md", H / ".config/opencode"),
    "cursor":      (None, None, ".cursor/skills", ".cursor/rules/session-pilot.mdc", H / ".cursor"),
    "copilot":     (None, None, ".github/skills", ".github/copilot-instructions.md", None),
    "project":     (None, None, ".agents/skills", "AGENTS.md", None),
}


def say(dry, msg):
    print(("[dry-run] " if dry else "") + msg)


def add_rule(rule, dry):
    if rule.suffix == ".mdc":
        say(dry, f"rule  -> {rule}")
        if not dry:
            rule.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(MDC, rule)
        return
    text = rule.read_text(encoding="utf-8") if rule.exists() else ""
    if START in text:
        say(dry, f"rule  already present in {rule}")
        return
    say(dry, f"rule  -> append to {rule}")
    if not dry:
        rule.parent.mkdir(parents=True, exist_ok=True)
        sep = "\n\n" if text and not text.endswith("\n\n") else ""
        rule.write_text(text + sep + BOOT.read_text(encoding="utf-8"), encoding="utf-8")


def remove_rule(rule, dry):
    if not rule.exists():
        return
    if rule.suffix == ".mdc":
        say(dry, f"remove {rule}")
        if not dry:
            rule.unlink()
        return
    text = rule.read_text(encoding="utf-8")
    if START in text and END in text:
        a, b = text.index(START), text.index(END) + len(END)
        say(dry, f"strip rule from {rule}")
        if not dry:
            rest = (text[:a].rstrip() + "\n" + text[b:].lstrip()).strip()
            if rest:
                rule.write_text(rest + "\n", encoding="utf-8")
            else:
                rule.unlink()  # file only contained our rule


def install_antigravity_plugin(dry=False):
    import json
    pdir = H / ".gemini/config/plugins/session-pilot"
    say(dry, f"antigravity plugin -> {pdir}")
    if dry:
        return
    rdir = pdir / "rules"
    rdir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": "session-pilot",
        "displayName": "Session Pilot",
        "version": "1.0.0",
        "description": "Always-on per-chat memory, skill routing, token optimization, and tone matching.",
        "license": "MIT"
    }
    (pdir / "plugin.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    boot_text = BOOT.read_text(encoding="utf-8")
    (rdir / "AGENTS.md").write_text(boot_text, encoding="utf-8")
    (rdir / "GEMINI.md").write_text(boot_text, encoding="utf-8")
    sdir = pdir / "skills/session-pilot"
    if sdir.exists():
        shutil.rmtree(sdir)
    shutil.copytree(SKILL, sdir, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    cfg = H / ".gemini/config/config.json"
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            data.setdefault("plugins", {})["session-pilot"] = {"enabled": True}
            cfg.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass


def remove_antigravity_plugin(dry=False):
    import json
    pdir = H / ".gemini/config/plugins/session-pilot"
    say(dry, f"remove antigravity plugin {pdir}")
    if dry:
        return
    if pdir.exists():
        shutil.rmtree(pdir)
    cfg = H / ".gemini/config/config.json"
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            if "plugins" in data and "session-pilot" in data["plugins"]:
                del data["plugins"]["session-pilot"]
                cfg.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass


def enable_autostart(dry=False):
    script_path = H / ".gemini" / "config" / "skills" / "session-pilot" / "scripts" / "sp.py"
    if not script_path.exists():
        script_path = SKILL / "scripts" / "sp.py"
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return
        startup = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        vbs_file = startup / "session-pilot-watcher.vbs"
        say(dry, f"autostart -> {vbs_file}")
        if not dry:
            pyw = Path(sys.executable).parent / "pythonw.exe"
            exe = str(pyw) if pyw.exists() else "pythonw"
            vbs_content = (
                'Set WshShell = CreateObject("WScript.Shell")\n'
                'q = Chr(34)\n'
                f'cmd = q & "{exe}" & q & " " & q & "{script_path}" & q & " watch"\n'
                'WshShell.Run cmd, 0, False\n'
            )
            vbs_file.write_text(vbs_content, encoding="utf-8")
    elif sys.platform == "darwin":
        plist_dir = H / "Library" / "LaunchAgents"
        say(dry, f"autostart -> {plist_dir / 'com.sessionpilot.watcher.plist'}")
        if not dry:
            plist_dir.mkdir(parents=True, exist_ok=True)
            plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.sessionpilot.watcher</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>{script_path}</string>
        <string>watch</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>"""
            (plist_dir / "com.sessionpilot.watcher.plist").write_text(plist_content, encoding="utf-8")
    elif sys.platform.startswith("linux"):
        unit_dir = H / ".config" / "systemd" / "user"
        say(dry, f"autostart -> {unit_dir / 'session-pilot.service'}")
        if not dry:
            unit_dir.mkdir(parents=True, exist_ok=True)
            unit_content = f"""[Unit]
Description=Session Pilot Background Indexer
After=default.target

[Service]
ExecStart={sys.executable} {script_path} watch
Restart=always

[Install]
WantedBy=default.target
"""
            (unit_dir / "session-pilot.service").write_text(unit_content, encoding="utf-8")


def disable_autostart(dry=False):
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            vbs = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "session-pilot-watcher.vbs"
            if vbs.exists():
                say(dry, f"remove autostart {vbs}")
                if not dry:
                    vbs.unlink()
    elif sys.platform == "darwin":
        plist = H / "Library" / "LaunchAgents" / "com.sessionpilot.watcher.plist"
        if plist.exists():
            say(dry, f"remove autostart {plist}")
            if not dry:
                plist.unlink()
    elif sys.platform.startswith("linux"):
        unit = H / ".config" / "systemd" / "user" / "session-pilot.service"
        if unit.exists():
            say(dry, f"remove autostart {unit}")
            if not dry:
                unit.unlink()


def install(name, args):
    gskills, grule, pskills, prule, _ = CLIENTS[name]
    if args.project:
        base = Path(args.project).resolve()
        skills_dir, rule = base / pskills, base / prule
    elif gskills is None:
        print(f"{name}: project-scoped only; pass --project <dir>")
        return False
    else:
        skills_dir, rule = gskills, grule
    dest = skills_dir / "session-pilot"
    if args.uninstall:
        if dest.exists():
            say(args.dry_run, f"{name}: remove {dest}")
            if not args.dry_run:
                shutil.rmtree(dest)
        if rule:
            remove_rule(rule, args.dry_run)
            if name == "antigravity" and not args.project:
                g_gemini = rule.parent / "GEMINI.md"
                remove_rule(g_gemini, args.dry_run)
                remove_antigravity_plugin(args.dry_run)
        return True
    say(args.dry_run, f"{name}: skill -> {dest}")
    if not args.dry_run:
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(SKILL, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if args.with_rule:
        if rule:
            add_rule(rule, args.dry_run)
            if name == "antigravity" and not args.project:
                g_gemini = rule.parent / "GEMINI.md"
                add_rule(g_gemini, args.dry_run)
                install_antigravity_plugin(args.dry_run)
        else:
            print(f"{name}: no known global rule file. Add adapters/bootstrap.md to your "
                  f"workspace AGENTS.md / GEMINI.md, or rerun with --project <dir>.")
    return True


def main():
    p = argparse.ArgumentParser(description="Install session-pilot")
    p.add_argument("--client", required=True, help=", ".join(CLIENTS) + ", or all")
    p.add_argument("--project", help="install into this project instead of globally")
    p.add_argument("--with-rule", action="store_true", help="also add the always-on bootstrap rule")
    p.add_argument("--autostart", action="store_true", help="enable background indexer on system startup")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    if sys.version_info < (3, 8):
        sys.exit("session-pilot needs Python 3.8+")
    if a.client == "all":
        names = [n for n, c in CLIENTS.items()
                 if c[4] is not None and c[4].exists() and (c[0] is not None or a.project)]
        if not names:
            sys.exit("no supported clients detected; use --client <name>")
    elif a.client in CLIENTS:
        names = [a.client]
    else:
        sys.exit(f"unknown client '{a.client}'. Choose: {', '.join(CLIENTS)}, all")
    ok = all([install(n, a) for n in names])
    if a.uninstall:
        disable_autostart(a.dry_run)
    elif a.autostart:
        enable_autostart(a.dry_run)
    if ok and not a.uninstall and not a.dry_run:
        helper = SKILL / "scripts" / "sp.py"
        print(f"done. Build the index now (optional): python \"{helper}\" index")
        if a.autostart:
            print("Background indexer service enabled on startup.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
