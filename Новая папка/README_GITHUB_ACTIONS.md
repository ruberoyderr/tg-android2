## From-scratch steps (Windows 11, no WSL)
1) Create a new GitHub repo (private or public).
2) Put in repo root: your `app.py`, `sticker_picker.py` + these files:
   - `main.py`, `mobile_ui.py`, `INTEGRATE_MOBILE_THEME.txt`, `requirements.txt`, `.github/workflows/android-apk.yml`
3) In `app.py` after you make `QApplication` and main window, add:
      from mobile_ui import apply_android_theme, enable_kinetic_scrolling, install_back_button_handler
      apply_android_theme(app, win)
      enable_kinetic_scrolling(win)
      install_back_button_handler(win)
4) Commit & push. Open GitHub → Actions → run “Build Android APK (PySide6)”. Wait for success.
5) Download artifact `android-apk` → inside is your debug `.apk` (ARM64).
6) If your app needs internet, edit generated `buildozer.spec` (from artifact):
      android.permissions = INTERNET,ACCESS_NETWORK_STATE
   Commit it into repo and re-run the action.
7) Install APK: copy to phone and open, or use ADB:
      adb install -r path\to\your.apk
