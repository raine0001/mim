# MIM Desktop MVP Architecture

## Goal

Run MIM interaction as a dedicated desktop application process, not a browser tab.

## MVP shape

- **Backend brain**: existing FastAPI service (`:8001`)
- **Desktop shell**: Electron app (`desktop/mim-shell`)
- **UI surface**: existing `/mim` endpoint loaded in the desktop window

## Why this helps

- Camera/microphone permissions are managed by the app shell runtime.
- MIM interaction has a stable always-on window and startup path.
- No dependency on normal browser tabs/workflow.

## Files added

- `desktop/mim-shell/package.json`
- `desktop/mim-shell/main.js`
- `desktop/mim-shell/README.md`
- `scripts/run_mim_desktop_shell.sh`
- `deploy/systemd-user/mim-desktop-shell.service`

## Run manually

```bash
cd /home/testpilot/mim/desktop/mim-shell
npm install
npm start
```

## Run via helper script

```bash
bash /home/testpilot/mim/scripts/run_mim_desktop_shell.sh
```

## Enable as user service

```bash
mkdir -p ~/.config/systemd/user
cp /home/testpilot/mim/deploy/systemd-user/mim-desktop-shell.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mim-desktop-shell.service
```

## Next phase (recommended)

- Move camera capture to native adapter process (OpenCV/GStreamer).
- Move microphone capture + STT to native adapter process.
- Move TTS playback to native audio output process.
- Keep FastAPI as orchestration and memory core.
