// Vaal Street as one desktop app.
//
// This shell is deliberately thin. The desk, its models and the screen check are the Python package,
// run as one child process (`vaalstreet app`, see vaalstreet/desktop.py). The page is the same one a
// browser gets, loaded from the child's own address on 127.0.0.1, so the desk's Host and Origin
// checks apply unchanged and no page code lives here. What the shell adds is what a browser tab
// cannot give: one thing to start, one thing to quit, and a hideout window that stays over the game.
//
// The shell sends nothing anywhere. It opens no address but the local desk; a link that leaves the
// desk is handed to the system's browser. It registers no keys and reads no screen or clipboard: the
// screen check does that, in the child, under the rules the README gives.

const { app, BrowserWindow, Menu, dialog, shell } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');

const READY = 'VAAL-DESK-READY';
// One fixed port, so the page's own remembered choices (kept per address by the browser engine)
// survive a restart. Not 3000: a desk running in WSL shows up on Windows at localhost:3000.
const FIRST_PORT = 3917;
const PORTS_TRIED = 10;
const LOG_KEPT = 200;

let desk = null;        // { child, url, dataDir }
let mainWindow = null;
let hideout = null;
let quitting = false;
const log = [];
let settings = { stacks: false, stash: false };

const settingsFile = () => path.join(app.getPath('userData'), 'settings.json');

function loadSettings() {
  try { settings = { ...settings, ...JSON.parse(fs.readFileSync(settingsFile(), 'utf8')) }; } catch { /* first start */ }
}

function saveSettings() {
  fs.mkdirSync(path.dirname(settingsFile()), { recursive: true });
  fs.writeFileSync(settingsFile(), JSON.stringify(settings, null, 2));
}

function note(text) {
  for (const line of String(text).split(/\r?\n/)) if (line.trim()) log.push(line);
  log.splice(0, Math.max(0, log.length - LOG_KEPT));
}

// The packaged app carries the Python side as a program of its own; from a checkout, the Python on
// the PATH (or VAAL_PYTHON) runs the package in place.
function deskCommand(port) {
  const args = ['app', '--port', String(port)];
  if (settings.stash) args.push('--stash'); else if (settings.stacks) args.push('--stacks');
  if (process.env.VAAL_NO_SYNC) args.push('--no-sync');
  if (app.isPackaged) {
    const exe = process.platform === 'win32' ? 'vaal-desk.exe' : 'vaal-desk';
    return { command: path.join(process.resourcesPath, 'vaal-desk', exe), args, cwd: process.resourcesPath };
  }
  const python = process.env.VAAL_PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
  return { command: python, args: ['-m', 'vaalstreet', ...args], cwd: path.join(__dirname, '..') };
}

function portFree(port) {
  return new Promise((resolve) => {
    const probe = net.createServer();
    probe.once('error', () => resolve(false));
    probe.once('listening', () => probe.close(() => resolve(true)));
    probe.listen(port, '127.0.0.1');
  });
}

async function freePort() {
  for (let port = FIRST_PORT; port < FIRST_PORT + PORTS_TRIED; port++) if (await portFree(port)) return port;
  throw new Error(`No free port between ${FIRST_PORT} and ${FIRST_PORT + PORTS_TRIED - 1}.`);
}

// Start the Python side and resolve with its address once it says it is ready.
async function startDesk() {
  const port = await freePort();
  const { command, args, cwd } = deskCommand(port);
  note(`Starting ${command} ${args.join(' ')}`);
  return new Promise((resolve, reject) => {
    // PYTHONUNBUFFERED: the ready line must arrive when it is printed, not when a buffer fills.
    const child = spawn(command, args, {
      cwd, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
      env: { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' },
    });
    const started = { child, url: null, dataDir: null };
    let pending = '';
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', (chunk) => {
      note(chunk);
      pending += chunk;
      const lines = pending.split(/\r?\n/);
      pending = lines.pop();
      for (const line of lines) {
        const data = line.match(/\(data in (.+)\)\s*$/);
        if (data) started.dataDir = data[1];
        if (line.startsWith(READY + ' ') && !started.url) {
          started.url = line.slice(READY.length + 1).trim();
          resolve(started);
        }
      }
    });
    child.stderr.on('data', note);
    child.once('error', (err) => reject(new Error(`Could not start the desk (${command}): ${err.message}`)));
    child.once('exit', (code) => {
      if (!started.url) reject(new Error(`The desk stopped before it was ready (exit code ${code}).`));
      else if (!quitting && desk === started) deskDied(code);
    });
  });
}

// Closing its input is how the child is asked to stop (it then closes the ledger and the port
// itself); it is ended the hard way only if it has not gone a few seconds later.
function stopDesk(running) {
  if (!running || running.child.exitCode !== null) return Promise.resolve();
  return new Promise((resolve) => {
    const timer = setTimeout(() => running.child.kill(), 4000);
    running.child.once('exit', () => { clearTimeout(timer); resolve(); });
    try { running.child.stdin.end(); } catch { running.child.kill(); }
  });
}

function deskDied(code) {
  const choice = dialog.showMessageBoxSync({
    type: 'error', title: 'Vaal Street', buttons: ['Start it again', 'Quit'],
    message: `The desk stopped (exit code ${code}).`, detail: log.slice(-15).join('\n'),
  });
  if (choice === 0) restart(); else app.quit();
}

const isDesk = (url) => { try { return desk && new URL(url).origin === desk.url; } catch { return false; } };
const isHideout = (url) => isDesk(url) && new URL(url).searchParams.has('hideout');

const pagePreferences = {
  contextIsolation: true, nodeIntegration: false, sandbox: true,
};

// Every window stays on the desk: a new window is allowed only for a desk address, and anything
// else goes to the system's browser.
function keepInside(window) {
  const contents = window.webContents;
  contents.setWindowOpenHandler(({ url }) => {
    // The page asks for its hideout as a plain window (it knows it is inside the app); the shell
    // answers with one of its own, which it can keep on top and find again.
    if (isHideout(url)) { openHideout(); return { action: 'deny' }; }
    if (isDesk(url)) return { action: 'allow', overrideBrowserWindowOptions: { autoHideMenuBar: true, webPreferences: pagePreferences } };
    if (/^https?:/.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  contents.on('will-navigate', (event, url) => {
    if (isDesk(url)) return;
    event.preventDefault();
    if (/^https?:/.test(url)) shell.openExternal(url);
  });
  contents.on('did-create-window', keepInside);
}

function openMain() {
  mainWindow = new BrowserWindow({
    width: 1440, height: 900, minWidth: 700, minHeight: 500, backgroundColor: '#0d0c0b',
    title: 'Vaal Street', webPreferences: pagePreferences,
  });
  keepInside(mainWindow);
  mainWindow.loadURL(desk.url);
  mainWindow.on('closed', () => { mainWindow = null; if (!quitting) app.quit(); });
}

// The small window for a trading session. 'screen-saver' is the level that stays above a game in
// borderless windowed mode; nothing stays above exclusive fullscreen, as the README says.
function openHideout() {
  if (hideout) { hideout.show(); hideout.focus(); return; }
  hideout = new BrowserWindow({
    width: 400, height: 640, minWidth: 300, minHeight: 200, backgroundColor: '#0d0c0b',
    title: 'Vaal Street hideout', autoHideMenuBar: true, alwaysOnTop: true, skipTaskbar: false,
    webPreferences: pagePreferences,
  });
  hideout.setAlwaysOnTop(true, 'screen-saver');
  keepInside(hideout);
  hideout.loadURL(desk.url + '/?hideout=1');
  hideout.on('closed', () => { hideout = null; });
}

async function restart() {
  const old = desk;
  desk = null;
  await stopDesk(old);
  try {
    desk = await startDesk();
  } catch (err) {
    dialog.showErrorBox('Vaal Street', `${err.message}\n\n${log.slice(-15).join('\n')}`);
    app.quit();
    return;
  }
  if (mainWindow) mainWindow.loadURL(desk.url); else openMain();
  if (hideout) hideout.loadURL(desk.url + '/?hideout=1');
}

function setStackCheck(change) {
  settings = { ...settings, ...change };
  if (settings.stash) settings.stacks = true;
  if (!settings.stacks) settings.stash = false;
  saveSettings();
  buildMenu();
  restart();
}

function showLog() {
  dialog.showMessageBox({ type: 'info', title: 'Vaal Street', message: 'What the desk and the screen check last said',
    detail: log.slice(-40).join('\n') || 'Nothing yet.' });
}

function buildMenu() {
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { label: 'Desk', submenu: [
      { label: 'Hideout window', accelerator: 'CmdOrCtrl+H', click: openHideout },
      { type: 'separator' },
      { label: 'Open the data folder', enabled: Boolean(desk && desk.dataDir), click: () => shell.openPath(desk.dataDir) },
      { label: 'What the desk last said…', click: showLog },
      { label: 'Start the desk again', click: restart },
      { type: 'separator' },
      { role: 'quit' },
    ] },
    { label: 'Screen check', submenu: [
      { label: 'Ctrl+Alt+F9 reads the offer on screen; Ctrl+Alt+F10 also records it', enabled: false },
      { type: 'separator' },
      // Off until the player ticks it, as the README promises for the stack check.
      { label: 'Stack check: Ctrl+C in game reads the clipboard once', type: 'checkbox', checked: settings.stacks,
        click: (item) => setStackCheck({ stacks: item.checked }) },
      { label: 'Also write each copied stack to my stash list', type: 'checkbox', checked: settings.stash,
        click: (item) => setStackCheck({ stash: item.checked }) },
    ] },
    { label: 'View', submenu: [
      { role: 'reload' }, { role: 'toggleDevTools' }, { type: 'separator' },
      { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' }, { type: 'separator' }, { role: 'togglefullscreen' },
    ] },
  ]));
}

// A second start shows the app that is already running, so two desks never write one ledger.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (!mainWindow) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
  });
  app.whenReady().then(async () => {
    loadSettings();
    try {
      desk = await startDesk();
    } catch (err) {
      dialog.showErrorBox('Vaal Street', `${err.message}\n\n${log.slice(-15).join('\n')}`);
      app.quit();
      return;
    }
    buildMenu();
    openMain();
    if (process.env.VAAL_OPEN_HIDEOUT) openHideout();
  });
  app.on('window-all-closed', () => app.quit());
  app.on('before-quit', (event) => {
    if (quitting || !desk) return;
    quitting = true;
    event.preventDefault();
    stopDesk(desk).then(() => app.quit());
  });
}
