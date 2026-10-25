"""Safe, offline checks for the AirPods user installer scripts."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


MODULE = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


class InstallScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="airpods install test ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "xdg config"
        self.bindir = self.root / "fake bin"
        self.bindir.mkdir()
        self.log = self.root / "systemctl.log"
        systemctl = self.bindir / "systemctl"
        systemctl.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$SYSTEMCTL_LOG\"\n"
            "case \"$*\" in *'show-environment') exit 0;; esac\nexit 0\n"
        )
        systemctl.chmod(0o755)
        self.exe = self.root / 'path with spaces/airpodsd "quoted" 100%.bin'
        self.exe.parent.mkdir(parents=True)
        self.exe.write_text("#!/bin/sh\nexit 0\n")
        self.exe.chmod(0o755)
        self.pip_log = self.root / "pip.log"
        fake_python = self.bindir / "python-with-pip-stub"
        fake_python.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$PIP_LOG\"\nexit 0\n"
        )
        fake_python.chmod(0o755)
        self.fake_python = fake_python
        self.fake_scripts = self.root / "isolated python" / "bin"
        self.fake_scripts.mkdir(parents=True)
        isolated_python = self.bindir / "isolated-python"
        isolated_python.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = -c ]; then\n"
            "  case \"$2\" in\n"
            "    *sysconfig.get_path*) printf '3.12\\t1\\t%s\\n' \"$FAKE_SCRIPTS\";;\n"
            "    *'import dbus, gi'*) exit 1;;\n"
            "    *'import airpods'*) exit 0;;\n"
            "  esac\n"
            "elif [ \"$1\" = -m ] && [ \"$2\" = pip ]; then\n"
            "  printf '%s\\n' \"$*\" >> \"$PIP_LOG\"\n"
            "  if [ \"$3\" = install ]; then printf '#!/bin/sh\\nexit 0\\n' > \"$FAKE_SCRIPTS/airpodsd\"; chmod +x \"$FAKE_SCRIPTS/airpodsd\"; fi\n"
            "fi\n"
        )
        isolated_python.chmod(0o755)
        self.isolated_python = isolated_python

    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "XDG_CONFIG_HOME": str(self.config),
                "AIRPODSD_BIN": str(self.exe),
                "AIRPODSD_PYTHON": PYTHON,
                "SYSTEMCTL_LOG": str(self.log),
                "PIP_LOG": str(self.pip_log),
                "FAKE_SCRIPTS": str(self.fake_scripts),
                "PATH": f"{self.bindir}:/usr/bin:/bin",
            }
        )
        return env

    def run_install(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(MODULE / "install.sh"), "--no-install", *args],
            env=self.env(), text=True, capture_output=True, check=False,
        )

    def test_default_installs_unit_without_systemctl_enable_or_start(self) -> None:
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text() if self.log.exists() else ""
        self.assertEqual(calls, "")
        self.assertTrue((self.config / "systemd/user/airpodsd.service").is_file())

    def test_enable_is_explicit_and_starts_service(self) -> None:
        result = self.run_install("--enable")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text().splitlines()
        self.assertIn("--user enable --now airpodsd.service", calls)
        self.assertIn("--user daemon-reload", calls)

    def test_different_existing_unit_is_preserved_without_replace(self) -> None:
        unit = self.config / "systemd/user/airpodsd.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("[Service]\nExecStart=/some/other/daemon\n")
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--replace-unit", result.stderr)
        self.assertEqual(unit.read_text(), "[Service]\nExecStart=/some/other/daemon\n")

    def test_replace_unit_makes_backup_before_replacing(self) -> None:
        unit = self.config / "systemd/user/airpodsd.service"
        unit.parent.mkdir(parents=True)
        original = "[Service]\nExecStart=/some/other/daemon\n"
        unit.write_text(original)
        result = self.run_install("--replace-unit")
        self.assertEqual(result.returncode, 0, result.stderr)
        backups = list(unit.parent.glob("airpodsd.service.*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), original)
        self.assertIn('ExecStart="', unit.read_text())

    def test_execstart_quotes_spaces_quotes_and_percent(self) -> None:
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        unit = (self.config / "systemd/user/airpodsd.service").read_text()
        self.assertIn('ExecStart="' + str(self.exe).replace('"', '\\"').replace('%', '%%') + '"', unit)

    def test_help_needs_no_runtime_tools(self) -> None:
        env = self.env()
        env.pop("AIRPODSD_PYTHON")
        env["PATH"] = "/usr/bin:/bin"
        result = subprocess.run(
            [str(MODULE / "install.sh"), "--help"], env=env,
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--enable", result.stdout)
        self.assertFalse(self.log.exists())

    def test_uninstall_refuses_unrecorded_unit_before_removing_package(self) -> None:
        unit = self.config / "systemd/user/airpodsd.service"
        unit.parent.mkdir(parents=True)
        original = "[Service]\nExecStart=/some/other/daemon\n"
        unit.write_text(original)
        env = self.env()
        env["AIRPODSD_PYTHON"] = str(self.fake_python)
        result = subprocess.run(
            [str(MODULE / "uninstall.sh")], env=env,
            text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to uninstall", result.stderr)
        self.assertEqual(unit.read_text(), original)
        self.assertFalse(self.log.exists())
        self.assertFalse(self.pip_log.exists())

    def test_uninstall_requires_registered_or_explicit_python(self) -> None:
        env = self.env()
        env.pop("AIRPODSD_PYTHON")
        result = subprocess.run(
            [str(MODULE / "uninstall.sh")], env=env,
            text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to guess", result.stderr)
        self.assertFalse(self.log.exists())
        self.assertFalse(self.pip_log.exists())

    def test_isolated_python_uses_its_scripts_path_without_user_pip(self) -> None:
        env = self.env()
        env["AIRPODSD_PYTHON"] = str(self.isolated_python)
        env.pop("AIRPODSD_BIN")
        result = subprocess.run(
            [str(MODULE / "install.sh")], env=env,
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        pip_calls = self.pip_log.read_text().splitlines()
        self.assertIn("-m pip --version", pip_calls)
        self.assertIn("-m pip install --upgrade .", pip_calls)
        self.assertNotIn("--user", " ".join(pip_calls))
        unit = (self.config / "systemd/user/airpodsd.service").read_text()
        self.assertIn(f'ExecStart="{self.fake_scripts}/airpodsd"', unit)
        self.assertEqual((self.config / "airpodsd/python").read_text().strip(), str(self.isolated_python))

    def test_system_python_uses_user_pip_and_user_scripts_path(self) -> None:
        env = self.env()
        env["AIRPODSD_PYTHON"] = str(self.isolated_python)
        env.pop("AIRPODSD_BIN")
        script = self.isolated_python.read_text().replace("3.12\\t1\\t", "3.12\\t0\\t")
        system_python = self.bindir / "system-python"
        system_python.write_text(script)
        system_python.chmod(0o755)
        env["AIRPODSD_PYTHON"] = str(system_python)
        result = subprocess.run(
            [str(MODULE / "install.sh")], env=env,
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        pip_calls = self.pip_log.read_text().splitlines()
        self.assertIn("-m pip install --user --upgrade .", pip_calls)
        unit = (self.config / "systemd/user/airpodsd.service").read_text()
        self.assertIn(f'ExecStart="{self.fake_scripts}/airpodsd"', unit)


if __name__ == "__main__":
    unittest.main()
