# Windows exe のビルド手順

`レコードApp` を、ターミナル（黒い窓）を表示しない単一の `.exe` にビルドします。
アイコンは `app_icon.ico` を使用します。

## 前提

アプリの依存パッケージ + PyInstaller が入った Python 環境が必要です。

```powershell
python -m pip install pyaudiowpatch faster-whisper customtkinter openai pyinstaller
```

`ffmpeg` / `ffprobe` は exe には同梱されません。実行する PC の PATH に必要です
（アプリ本体と同じ前提）。

## ビルド

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
```

または直接:

```powershell
python -m PyInstaller --noconfirm --clean レコードApp.spec
```

生成物: `dist\レコードApp.exe`

- `console=False`（`レコードApp.spec`）によりターミナルは表示されません。
- `customtkinter` / `faster_whisper` / `ctranslate2` / `av` のデータ・バイナリは
  spec 内の `collect_all` で同梱されます。

## 配布について（重要）

`faster-whisper`（`ctranslate2` / `av` を含む）を同梱するため、生成される exe は
数百 MB になります。**GitHub のリポジトリには 1 ファイル 100MB の上限があるため、
exe を直接コミットすることはできません。**

exe の配布は次のいずれかを使ってください:

- **GitHub Releases**（推奨。アセットは最大 2GB）
  ```powershell
  gh release create v1.06 "dist\レコードApp.exe" --title "レコードApp v1.06" --notes "Windows 実行ファイル"
  ```
- **GitHub Actions** でタグ push 時に自動ビルドして Release に添付する

`dist/` と `build/` は `.gitignore` 済みです。
