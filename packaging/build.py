"""Builds dist/Converter/ with PyInstaller, then dist/Converter-Setup-<version>.exe with Inno Setup.

    .venv\\Scripts\\python packaging\\build.py            # both
    .venv\\Scripts\\python packaging\\build.py --no-installer
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / 'packaging'


def version() -> str:
    text = (ROOT / 'converter' / '__init__.py').read_text('utf-8')
    return re.search(r"__version__ = '([^']+)'", text).group(1)


def write_version_info(v: str):
    nums = tuple(int(x) for x in (v.split('.') + ['0', '0', '0'])[:4])
    (PKG / 'version_info.txt').write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'sttsspjy'),
      StringStruct('FileDescription', 'Converter'),
      StringStruct('FileVersion', '{v}'),
      StringStruct('InternalName', 'Converter'),
      StringStruct('LegalCopyright', 'MIT License'),
      StringStruct('OriginalFilename', 'Converter.exe'),
      StringStruct('ProductName', 'Converter'),
      StringStruct('ProductVersion', '{v}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""", 'utf-8')


def find_iscc() -> str | None:
    for c in (os.environ.get('ISCC'), shutil.which('iscc'),
              r'C:\Program Files (x86)\Inno Setup 6\ISCC.exe', r'C:\Program Files\Inno Setup 6\ISCC.exe',
              str(Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'Inno Setup 6' / 'ISCC.exe')):
        if c and Path(c).is_file():
            return c
    return None


def main():
    v = version()
    print(f'Building Converter {v}')
    write_version_info(v)
    shutil.rmtree(ROOT / 'build', ignore_errors=True)
    shutil.rmtree(ROOT / 'dist' / 'Converter', ignore_errors=True)
    subprocess.run([sys.executable, '-m', 'PyInstaller', str(PKG / 'converter.spec'), '--noconfirm',
                    '--distpath', str(ROOT / 'dist'), '--workpath', str(ROOT / 'build')], check=True, cwd=ROOT)
    if '--no-installer' in sys.argv:
        return
    iscc = find_iscc()
    if not iscc:
        sys.exit('Inno Setup 6 (ISCC.exe) not found. Install it or set ISCC=path\\to\\ISCC.exe')
    subprocess.run([iscc, f'/DAppVersion={v}', str(PKG / 'installer.iss')], check=True, cwd=PKG)
    print(f'Done: dist/Converter-Setup-{v}.exe')


if __name__ == '__main__':
    main()
