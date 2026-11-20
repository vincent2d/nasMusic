import ast
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


class DeployFpkLifecycleTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[2] / "tools" / "deploy_fpk.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        self.deploy = next(ast.literal_eval(node.value) for node in tree.body
                           if isinstance(node, ast.Assign)
                           and any(isinstance(name, ast.Name) and name.id == "DEPLOY" for name in node.targets))

    def test_start_and_stop_go_through_app_center_and_propagate_failure(self):
        helpers = "\n".join(re.search(rf"^{name}\(\)\{{.*?^\}}", self.deploy, re.M | re.S).group()
                            for name in ("start_app", "stop_app"))
        with tempfile.TemporaryDirectory() as folder:
            trace = Path(folder) / "trace"
            cli = Path(folder) / "appcenter-cli"
            cli.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$TRACE"\nexit "${CLI_RESULT:-0}"\n')
            cli.chmod(0o755)
            env = {**os.environ, "PATH": folder + os.pathsep + os.environ["PATH"], "TRACE": str(trace)}
            result = subprocess.run(["bash", "-c", helpers + "\nstop_app && start_app"], env=env)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(trace.read_text().splitlines(), ["stop fnaudio", "start fnaudio"])
            for helper in ("start_app", "stop_app"):
                result = subprocess.run(["bash", "-c", helpers + "\n" + helper], env={**env, "CLI_RESULT": "7"})
                self.assertEqual(result.returncode, 7)

    def test_deploy_and_rollback_never_start_a_detached_instance(self):
        self.assertNotRegex(self.deploy, r'bash "\$CONTROL/cmd/main" (start|stop)')
        self.assertIn('if ! start_app; then', self.deploy)
        self.assertIn('stop_app || fail "App Center could not stop fnAudio"', self.deploy)
        self.assertIn('stop_app >/dev/null 2>&1 || true', self.deploy)
        self.assertIn('[ -f "$CONTROL/cmd/main" ] && start_app', self.deploy)
        self.assertLess(self.deploy.index('fail "install fnAudio through App Center'), self.deploy.index("trap rollback EXIT"))
