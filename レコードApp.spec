# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec: レコードApp を Windows 用の単一 exe（ターミナル非表示）にビルドする。
#
# 使い方（アプリの依存が入った Python 環境で）:
#   python -m pip install pyaudiowpatch faster-whisper customtkinter openai pyinstaller
#   python -m PyInstaller --noconfirm --clean レコードApp.spec
#
# 生成物: dist/レコードApp.exe
#
# メモ:
# - console=False によりターミナル（黒い窓）は表示されない。
# - customtkinter はテーマ用の JSON/アセットを同梱する必要があるため collect_all する。
# - faster_whisper / ctranslate2 / av はバイナリ・データを collect_all で取り込む。

from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []

for pkg in ("customtkinter", "faster_whisper", "ctranslate2", "av"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        # 未インストールのパッケージがあってもビルド自体は継続（実行時に必要）
        pass


a = Analysis(
    ["録音.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="レコードApp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # ← ターミナルを表示しない
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="icon.ico",    # ← アプリアイコン
)
