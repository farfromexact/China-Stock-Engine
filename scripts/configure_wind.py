"""Configure the official Wind SDK in this project's virtual environment only.

Uses only the standard library; never imports or downloads WindPy.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import site
import sys
import sysconfig

ROOT = Path(__file__).resolve().parents[1]


def resolve_sdk(explicit, existing_pth, system, home):
    if explicit:
        candidate = Path(explicit).expanduser()
        if not (candidate / 'WindPy.py').is_file():
            raise ValueError(f'WindPy.py not found in configured directory: {candidate}')
        return candidate.resolve()
    if existing_pth.is_file():
        for line in existing_pth.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if not line or line.startswith(('#', 'import ' , 'import\t')):
                continue
            candidate = Path(line)
            if not candidate.is_absolute():
                candidate = existing_pth.parent / candidate
            if (candidate / 'WindPy.py').is_file():
                return candidate.resolve()
    defaults = {
        'Darwin': Path('/Applications/Wind API.app/Contents/python'),
        'Windows': home / 'Wind' / 'Wind.NET.Client' / 'WindNET' / 'x64',
    }
    candidate = defaults.get(system)
    if candidate and (candidate / 'WindPy.py').is_file():
        return candidate.resolve()
    raise ValueError('Official Wind SDK not found. Install/repair it, then pass '
                     '--wind-python-dir or set WIND_PYTHON_DIR to the folder containing WindPy.py.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wind-python-dir', help='Directory containing the official WindPy.py')
    parser.add_argument('--check', action='store_true', help='Inspect paths without writing or loading native SDK')
    args = parser.parse_args()
    try:
        if sys.prefix == sys.base_prefix or Path(sys.prefix).resolve() != (ROOT / '.venv').resolve():
            raise ValueError('Run with this repository\'s .venv Python; system Python will not be modified.')
        # Windows may list the environment root before Lib/site-packages.
        # Use the interpreter's install scheme rather than assuming index zero.
        pth = Path(sysconfig.get_path('purelib')) / 'WindPy.pth'
        existing = pth
        if not existing.is_file():
            existing = next((Path(directory) / 'WindPy.pth' for directory in site.getsitepackages()
                             if (Path(directory) / 'WindPy.pth').is_file()), pth)
        sdk = resolve_sdk(args.wind_python_dir or os.environ.get('WIND_PYTHON_DIR'),
                          existing, platform.system(), Path.home())
        print(f'Python: {sys.executable}\nPlatform: {platform.system()} {platform.machine()}\nSDK: {sdk}\nPath file: {pth}')
        if not args.check:
            pth.write_text(str(sdk) + '\n', encoding='utf-8')
            print(f'Configured without loading SDK. Run with {sys.executable}: -m china_stock_engine.wind_collect doctor')
        else:
            print('SDK path exists. Native loading, architecture and login are NOT tested.')
        return 0
    except (ValueError, OSError, UnicodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
