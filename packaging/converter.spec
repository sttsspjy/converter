# PyInstaller spec: builds dist/Converter/ (one-folder, windowed).
#   .venv\Scripts\python -m PyInstaller packaging\converter.spec --noconfirm
from pathlib import Path

ROOT = Path(SPECPATH).parent

a = Analysis(
    [str(ROOT / 'app.py')],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / 'ui'), 'ui'), (str(ROOT / 'assets'), 'assets')],
    hiddenimports=['clr', 'webview.platforms.winforms', 'webview.platforms.edgechromium'],
    excludes=['tkinter', 'unittest', 'pydoc', 'test'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='Converter',
    icon=str(ROOT / 'assets' / 'icon.ico'),
    console=False,
    version=str(ROOT / 'packaging' / 'version_info.txt'),
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name='Converter', upx=False)
