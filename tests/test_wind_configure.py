import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("stock_configure_wind", Path(__file__).resolve().parents[1] / "scripts/configure_wind.py")
configure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure)


class WindConfigurationTests(unittest.TestCase):
    def test_windows_scheme_uses_purelib_not_first_site_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sdk = root / "SDK"; sdk.mkdir()
            (sdk / "WindPy.py").write_text("raise RuntimeError('SDK must not be imported')")
            purelib = root / ".venv/Lib/site-packages"; purelib.mkdir(parents=True)
            with patch.object(configure, "ROOT", root), \
                 patch.object(configure.sys, "prefix", str(root / ".venv")), \
                 patch.object(configure.sys, "base_prefix", str(root / "base")), \
                 patch.object(configure.sysconfig, "get_path", return_value=str(purelib)), \
                 patch.object(configure.site, "getsitepackages", return_value=[str(root / ".venv"), str(purelib)]), \
                 patch.object(configure.sys, "argv", ["configure", "--wind-python-dir", str(sdk)]), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(configure.main(), 0)
            self.assertEqual((purelib / "WindPy.pth").read_text().strip(), str(sdk.resolve()))
            self.assertFalse((root / ".venv/WindPy.pth").exists())

    def test_check_mode_does_not_register_or_import_sdk(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); sdk = root / "SDK"; sdk.mkdir()
            (sdk / "WindPy.py").write_text("raise RuntimeError('not imported')")
            purelib = root / ".venv/Lib/site-packages"; purelib.mkdir(parents=True)
            with patch.object(configure, "ROOT", root), \
                 patch.object(configure.sys, "prefix", str(root / ".venv")), \
                 patch.object(configure.sys, "base_prefix", str(root / "base")), \
                 patch.object(configure.sysconfig, "get_path", return_value=str(purelib)), \
                 patch.object(configure.sys, "argv", ["configure", "--wind-python-dir", str(sdk), "--check"]), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(configure.main(), 0)
            self.assertFalse((purelib / "WindPy.pth").exists())
