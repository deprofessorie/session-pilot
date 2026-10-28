"""Tests for sp.py. Run: python -m unittest discover -s tests"""
import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "session-pilot" / "scripts"))


def skill(root, name, desc):
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n# {name}\n",
                                encoding="utf-8")


class SPTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.skills = self.tmp / "skills"
        self.agents = self.tmp / "agents"
        self.agents.mkdir(parents=True)
        skill(self.skills, "fastapi-patterns", "FastAPI backend structure, auth, Pydantic schemas")
        skill(self.skills, "react-testing", "React component testing with Testing Library and axe")
        skill(self.skills, "strategic-compact", "Suggests context compaction to save tokens")
        (self.skills / "group" / "pdf").mkdir(parents=True)
        (self.skills / "group" / "pdf" / "SKILL.md").write_text(
            "---\nname: document-pdf\ndescription: >-\n  Extract text and tables\n  from PDF files\n---\n",
            encoding="utf-8")
        (self.agents / "reviewer.md").write_text(
            "---\nname: code-reviewer\ndescription: \"Reviews code for bugs\"\n---\n", encoding="utf-8")
        os.environ.update({
            "SESSION_PILOT_HOME": str(self.tmp / "home"),
            "SESSION_PILOT_NO_DEFAULT_ROOTS": "1",
            "SESSION_PILOT_ROOTS": os.pathsep.join([str(self.skills), "agent=" + str(self.agents)]),
            "SESSION_PILOT_NO_AUTO_CONV": "1",
        })
        os.environ.pop("SESSION_PILOT_ID", None)
        import importlib
        import sp
        self.sp = importlib.reload(sp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k in ("SESSION_PILOT_HOME", "SESSION_PILOT_NO_DEFAULT_ROOTS", "SESSION_PILOT_ROOTS"):
            os.environ.pop(k, None)

    def run_sp(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.sp.main(list(args))
        return buf.getvalue()

    def test_frontmatter_folded(self):
        fm = self.sp.frontmatter(self.skills / "group" / "pdf" / "SKILL.md")
        self.assertEqual(fm["name"], "document-pdf")
        self.assertEqual(fm["description"], "Extract text and tables from PDF files")

    def test_index_counts_kinds(self):
        out = self.run_sp("index", "--rebuild")
        self.assertIn("indexed 5", out)
        self.assertIn("1 agents", out)

    def test_find_ranks_best_first(self):
        out = self.run_sp("find", "build a FastAPI backend with auth")
        self.assertIn("1. fastapi-patterns", out)
        out = self.run_sp("find", "extract tables from a pdf")
        self.assertIn("1. document-pdf", out)
        out = self.run_sp("find", "review my code for bugs", "--kind", "agent")
        self.assertIn("1. code-reviewer [agent", out)

    def test_find_no_match(self):
        out = self.run_sp("find", "zebra quantum banana")
        self.assertTrue(out.startswith("match: none"))

    def test_optimizers(self):
        self.assertIn("strategic-compact", self.run_sp("optimizers"))
        self.run_sp("prefer", "react-testing")
        self.assertTrue(self.run_sp("optimizers").startswith("- react-testing"))

    def test_memory_roundtrip_and_cap(self):
        self.assertIn("NEW SESSION s1", self.run_sp("recall", "--session", "s1"))
        self.run_sp("save", "--session", "s1", "--goal", "ship it", "--style", "terse",
                    "--skill", "fastapi-patterns")
        for i in range(12):
            self.run_sp("save", "--session", "s1", "--done", f"step {i}", "--next", f"step {i + 1}")
        out = self.run_sp("recall", "--session", "s1")
        self.assertIn("Goal: ship it", out)
        self.assertIn("Style: terse", out)
        self.assertIn("next: step 12", out)
        self.assertIn("Earlier: step 0", out)
        s = self.sp.load_session("s1")
        self.assertEqual(len(s["checkpoints"]), self.sp.MAX_CHECKPOINTS)

    def test_session_id_sanitized_and_auto(self):
        self.assertEqual(self.sp.session_id("a/b:c"), "a_b_c")
        self.assertTrue(self.sp.session_id(None).startswith("auto-"))

    def test_global_style_fallback(self):
        self.run_sp("style", "set", "friendly, emoji ok")
        self.assertIn("Style: friendly, emoji ok", self.run_sp("recall", "--session", "fresh"))

    def test_forget(self):
        self.run_sp("save", "--session", "gone", "--goal", "x")
        self.assertIn("forgot gone", self.run_sp("forget", "gone"))
        self.assertIn("no sessions", self.run_sp("sessions"))

    def test_check_for_changes_and_changelog(self):
        self.run_sp("index")
        skill(self.skills, "new-skill-x", "A newly added skill description")
        added, removed = self.sp.check_for_changes()
        self.assertEqual(added, ["new-skill-x"])
        self.assertEqual(removed, [])
        out = self.run_sp("changelog")
        self.assertIn("+ added:   new-skill-x", out)

    def test_recall_reports_new_tools(self):
        self.run_sp("index")
        self.run_sp("save", "--session", "s-old", "--goal", "work")
        time.sleep(0.05)
        skill(self.skills, "hot-tool", "Brand new tool added during conversation")
        self.sp.check_for_changes()
        out = self.run_sp("recall", "--session", "s-old")
        self.assertIn("New skills installed since last checkpoint: hot-tool", out)

    def test_gc_purges_deleted_project(self):
        pdir = self.tmp / "proj-alpha"
        pdir.mkdir(parents=True)
        self.run_sp("save", "--session", "sess-proj", "--cwd", str(pdir), "--goal", "build something")
        self.assertIn("sess-proj", self.run_sp("sessions"))
        # Delete project directory
        shutil.rmtree(pdir)
        gc_out = self.run_sp("gc")
        self.assertIn("pruned 1 session(s)", gc_out)
        self.assertIn("sess-proj", gc_out)
        self.assertIn("no sessions", self.run_sp("sessions"))

    def test_recall_purges_deleted_project(self):
        pdir = self.tmp / "proj-beta"
        pdir.mkdir(parents=True)
        self.run_sp("save", "--session", "sess-beta", "--cwd", str(pdir), "--goal", "build beta")
        # Delete project directory
        shutil.rmtree(pdir)
        # Recall should detect deletion and return NEW SESSION
        out = self.run_sp("recall", "--session", "sess-beta")
        self.assertIn("NEW SESSION sess-beta", out)

    def test_import_project_and_instructions(self):
        pdir = self.tmp / "proj-gamma"
        pdir.mkdir(parents=True)
        (pdir / "AGENTS.md").write_text("# Project Gamma Rules\nAlways use strict typing.\n", encoding="utf-8")
        out = self.run_sp("import-project", str(pdir), "--session", "sess-gamma")
        self.assertIn("PROJECT: proj-gamma", out)
        self.assertIn("AGENTS.md", out)
        self.assertIn("Always use strict typing", out)
        self.assertIn("Session Pilot: added always-on rule to AGENTS.md", out)
        # Check AGENTS.md now contains bootstrap
        content = (pdir / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("session-pilot:start", content)

    def test_reconcile_and_adopt_project_skill(self):
        # Give global fastapi-patterns a helper script so it has higher capability
        (self.skills / "fastapi-patterns" / "scripts").mkdir(parents=True, exist_ok=True)
        (self.skills / "fastapi-patterns" / "scripts" / "helper.py").write_text("# helper", encoding="utf-8")

        pdir = self.tmp / "proj-delta"
        local_skills = pdir / ".gemini" / "skills"
        local_skills.mkdir(parents=True)
        # 1. Project has a custom specialized skill not present in global
        skill(local_skills, "custom-billing-engine", "Custom billing calculation for project delta")
        # 2. Project has a basic copy of an existing global skill
        skill(local_skills, "fastapi-patterns", "Basic fastapi starter")

        out = self.run_sp("reconcile", str(pdir))
        # fastapi-patterns should prefer global
        self.assertIn("fastapi-patterns: [USE GLOBAL]", out)
        # custom skill should adopt project
        self.assertIn("custom-billing-engine: [ADOPT PROJECT]", out)

        # Test adopting into global skills
        adopt_out = self.run_sp("reconcile", str(pdir), "--adopt")
        self.assertIn("[ADOPT PROJECT] (installed to", adopt_out)


if __name__ == "__main__":
    unittest.main()
