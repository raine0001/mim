# MIM Desktop Shell (MVP)

This is a native desktop shell for MIM interaction.

## What it does

- Launches a dedicated desktop app window for MIM (`http://127.0.0.1:18001/mim` by default).
- Grants camera/microphone/media permissions at the app shell level.
- Runs independently from a normal browser tab workflow.

## Requirements

- Node.js 20+
- MIM backend reachable at `http://127.0.0.1:18001`

## Run

```bash
cd desktop/mim-shell
npm install
npm start
```

Optional custom URL:

```bash
cd desktop/mim-shell
npm install
MIM_UI_URL=http://127.0.0.1:18001/mim npm start
```

## Notes

- This MVP shell reuses the existing `/mim` UI route.
- Next step is moving camera/mic capture and speaker output to native modules for full host-controlled media I/O.
