const { app, BrowserWindow, session, shell } = require('electron');
const path = require('path');

const DEFAULT_UI_URL = process.env.MIM_UI_URL || 'http://127.0.0.1:18001/mim';
const DISABLE_SANDBOX = process.env.MIM_ELECTRON_DISABLE_SANDBOX === '1';
const USER_DATA_DIR = (process.env.MIM_ELECTRON_USER_DATA_DIR || '').trim();

console.log(`[mim-shell] ui_url=${DEFAULT_UI_URL}`);
if (USER_DATA_DIR) {
    const resolvedUserDataDir = path.resolve(USER_DATA_DIR);
    app.setPath('userData', resolvedUserDataDir);
    console.log(`[mim-shell] user_data_dir=${resolvedUserDataDir}`);
}

const LEGACY_UI_ORIGIN = 'http://127.0.0.1:8000';
const ACTIVE_UI_ORIGIN = 'http://127.0.0.1:18001';

if (DISABLE_SANDBOX) {
    app.commandLine.appendSwitch('no-sandbox');
    app.commandLine.appendSwitch('disable-setuid-sandbox');
}
app.commandLine.appendSwitch('disable-gpu');
app.commandLine.appendSwitch('disable-gpu-compositing');
app.commandLine.appendSwitch('disable-software-rasterizer');
app.commandLine.appendSwitch('enable-speech-dispatcher');
app.commandLine.appendSwitch('enable-logging');

function grantMediaPermissions() {
    const mediaPermissions = new Set([
        'media',
        'microphone',
        'camera',
        'display-capture',
    ]);

    session.defaultSession.setPermissionCheckHandler((_wc, permission, requestingOrigin) => {
        if (mediaPermissions.has(permission)) {
            console.log(`[mim-shell] permission-check allow permission=${permission} origin=${String(requestingOrigin || '')}`);
        }
        if (mediaPermissions.has(permission)) {
            return true;
        }
        return true;
    });

    session.defaultSession.setPermissionRequestHandler((_wc, permission, callback) => {
        if (mediaPermissions.has(permission)) {
            console.log(`[mim-shell] permission-request allow permission=${permission}`);
        }
        if (mediaPermissions.has(permission)) {
            callback(true);
            return;
        }
        callback(true);
    });

    if (session.defaultSession.setDevicePermissionHandler) {
        session.defaultSession.setDevicePermissionHandler(() => true);
    }

    session.defaultSession.webRequest.onBeforeRequest((details, callback) => {
        const url = String(details.url || '');
        if (url.startsWith(`${LEGACY_UI_ORIGIN}/`)) {
            const redirectedUrl = `${ACTIVE_UI_ORIGIN}${url.slice(LEGACY_UI_ORIGIN.length)}`;
            console.log(`[mim-shell] redirect ${url} -> ${redirectedUrl}`);
            callback({ redirectURL: redirectedUrl });
            return;
        }
        callback({});
    });
}

function createWindow() {
    const win = new BrowserWindow({
        width: 1320,
        height: 860,
        minWidth: 980,
        minHeight: 640,
        autoHideMenuBar: true,
        title: 'MIM Desktop',
        webPreferences: {
            contextIsolation: true,
            sandbox: !DISABLE_SANDBOX,
            spellcheck: false,
            autoplayPolicy: 'no-user-gesture-required',
            webSecurity: true,
        },
    });

    win.webContents.setWindowOpenHandler(({ url }) => {
        shell.openExternal(url);
        return { action: 'deny' };
    });

    // Mirror renderer console messages into terminal logs so mic failures are visible
    // even when DevTools is not open.
    win.webContents.on('console-message', (_event, level, message, line, sourceId) => {
        const src = String(sourceId || 'renderer');
        console.log(`[mim-shell][renderer:${level}] ${src}:${line} ${String(message || '')}`);
    });

    win.loadURL(DEFAULT_UI_URL).catch(() => {
        win.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(`
      <html>
        <body style="font-family:Arial;background:#081d2a;color:#d7efff;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;">
          <div style="max-width:640px;padding:24px;border:1px solid #1a4f68;border-radius:10px;background:#0d2536;">
            <h2 style="margin:0 0 12px 0;">MIM Desktop</h2>
            <p style="margin:0 0 8px 0;">Could not reach ${DEFAULT_UI_URL}.</p>
            <p style="margin:0;opacity:0.85;">Start the MIM server first, then relaunch this app.</p>
          </div>
        </body>
      </html>
    `)}`);
    });

    win.webContents.on('did-finish-load', () => {
        try {
            win.webContents.setAudioMuted(false);
        } catch (_) {
        }

        win.webContents
            .executeJavaScript(
                "try { if (window.speechSynthesis && window.speechSynthesis.resume) { window.speechSynthesis.resume(); } } catch (_) {}",
                true,
            )
            .catch(() => { });
    });
}

app.whenReady().then(() => {
    grantMediaPermissions();
    createWindow();

    app.on('activate', () => {
        if (BrowserWindow.getAllWindows().length === 0) {
            createWindow();
        }
    });
});

app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') {
        app.quit();
    }
});
