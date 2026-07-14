# レコードApp を Windows 用 exe（ターミナル非表示）にビルドするスクリプト。
#
# 前提: アプリの依存が入った Python 環境で実行すること。
#   python -m pip install pyaudiowpatch faster-whisper customtkinter openai pyinstaller
#
# 実行:
#   powershell -ExecutionPolicy Bypass -File build.ps1
#
# 生成物: dist\レコードApp.exe

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

Write-Host "PyInstaller でビルドします..." -ForegroundColor Cyan
python -m PyInstaller --noconfirm --clean "レコードApp.spec"

$exe = Join-Path $here "dist\レコードApp.exe"
if (Test-Path $exe) {
    $size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host "完了: $exe ($size MB)" -ForegroundColor Green
} else {
    Write-Host "ビルドに失敗しました。上のログを確認してください。" -ForegroundColor Red
    exit 1
}
