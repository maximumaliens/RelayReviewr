"""Unit checks for persona lookup: a project's persona reaches its worktrees, not unrelated folders."""
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from ccrelay.intake import OPS_INSTRUCTIONS, instructions_for  # noqa: E402

PERSONA = "PERSONA-MARKER: you review the example project."


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   cwd=cwd, check=True, capture_output=True)


class PersonaLookup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.repo = root / "Project"
        self.repo.mkdir()
        git("init", "-q", cwd=self.repo)
        git("commit", "-q", "--allow-empty", "-m", "seed", cwd=self.repo)
        persona = root / "persona.md"
        persona.write_text(PERSONA)
        self.cfg = {"personas": {str(self.repo): str(persona)}}
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def test_exact_project_folder_gets_the_persona(self):
        self.assertIn(PERSONA, instructions_for(self.cfg, str(self.repo)))

    def test_worktree_inside_the_project_gets_the_persona(self):
        wt = self.repo / ".claude" / "worktrees" / "abc-43-implement"
        git("worktree", "add", "-q", "-b", "abc-43", str(wt), cwd=self.repo)
        self.assertIn(PERSONA, instructions_for(self.cfg, str(wt)))

    def test_worktree_outside_the_project_folder_gets_the_persona(self):
        wt = self.root / "elsewhere" / "wt"
        git("worktree", "add", "-q", "-b", "outside", str(wt), cwd=self.repo)
        self.assertIn(PERSONA, instructions_for(self.cfg, str(wt)))

    def test_unrelated_repo_gets_no_persona(self):
        other = self.root / "Other"
        other.mkdir()
        git("init", "-q", cwd=other)
        self.assertEqual(instructions_for(self.cfg, str(other)), OPS_INSTRUCTIONS.strip())

    def test_folder_outside_any_repo_gets_no_persona(self):
        plain = self.root / "plain"
        plain.mkdir()
        self.assertEqual(instructions_for(self.cfg, str(plain)), OPS_INSTRUCTIONS.strip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
