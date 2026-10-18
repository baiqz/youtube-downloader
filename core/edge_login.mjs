// 登录向导：起一个独立的 Edge 窗口，等用户登录 YouTube，用 CDP 把明文 cookie 导出成
// yt-dlp 能吃 的 Netscape cookies.txt。
//
// 为什么不用 --cookies-from-browser：
//   1) Chromium 运行时把 Cookies 库锁死，复制直接 PermissionError；
//   2) 新版 Edge/Chrome 的 cookie 是 App-Bound 加密（v20），就算复制出来也解不开。
//   CDP 的 Network.getAllCookies 拿到的是浏览器自己解密后的明文，两条都绕开。
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

// ---------- 参数 ----------
const argv = process.argv.slice(2);
const arg = (name, def = '') => {
  const i = argv.indexOf('--' + name);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : def;
};
const has = (name) => argv.includes('--' + name);

const BROWSER = arg('browser');
const PROFILE = arg('profile');
const OUT = arg('out');
const STATE = arg('state');
const PROXY = arg('proxy');
const PORT = parseInt(arg('port', '9411'), 10);
const TIMEOUT = parseInt(arg('timeout', '300'), 10) * 1000;
const HEADLESS = has('headless');
const START_URL = arg('url', 'https://www.youtube.com/');

// 判定"已登录"的 cookie：Google/Youtube 的登录态三件套任意一个即可
const AUTH_NAMES = new Set(['SID', 'SAPISID', '__Secure-3PSID', '__Secure-1PSID', 'HSID', 'SSID', 'LOGIN_INFO']);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function writeState(state, message, extra = {}) {
  try {
    const body = JSON.stringify({ state, message, updated: Date.now() / 1000, ...extra }, null, 2);
    fs.writeFileSync(STATE + '.tmp', body, 'utf8');
    fs.renameSync(STATE + '.tmp', STATE);
  } catch { /* 状态文件写失败不致命 */ }
}

class CDP {
  constructor(ws) {
    this.ws = ws; this.id = 0; this.pending = new Map(); this.closed = false;
    ws.addEventListener('message', (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id && this.pending.has(m.id)) {
        const { res, rej } = this.pending.get(m.id);
        this.pending.delete(m.id);
        m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result);
      }
    });
    ws.addEventListener('close', () => { this.closed = true; });
  }
  send(method, params = {}, sessionId) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
    return new Promise((res, rej) => {
      this.pending.set(id, { res, rej });
      setTimeout(() => { if (this.pending.has(id)) { this.pending.delete(id); rej(new Error('CDP 超时 ' + method)); } }, 20000);
    });
  }
}

function toNetscape(cookies) {
  const keep = cookies.filter((c) =>
    /(^|\.)(youtube\.com|google\.com|googlevideo\.com|youtube-nocookie\.com|googleapis\.com|gstatic\.com)$/.test(c.domain || ''));
  const lines = ['# Netscape HTTP Cookie File', '# https://curl.se/docs/http-cookies.html', '# exported by ytdl'];
  for (const c of keep) {
    const dom = (c.httpOnly ? '#HttpOnly_' : '') + c.domain;
    const inc = c.domain.startsWith('.') ? 'TRUE' : 'FALSE';
    const exp = c.expires && c.expires > 0 ? Math.floor(c.expires) : 0;
    const val = String(c.value ?? '').replace(/[\t\r\n]/g, '');
    lines.push([dom, inc, c.path || '/', c.secure ? 'TRUE' : 'FALSE', exp, c.name, val].join('\t'));
  }
  return { text: lines.join('\n') + '\n', count: keep.length };
}

function authed(cookies) {
  return cookies.some((c) => AUTH_NAMES.has(c.name) && c.value && /(^|\.)(google|youtube)\.com$/.test(c.domain || ''));
}

async function main() {
  fs.mkdirSync(path.dirname(OUT), { recursive: true });
  fs.mkdirSync(path.dirname(STATE), { recursive: true });

  const args = [
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${PROFILE}`,
    '--no-first-run', '--no-default-browser-check', '--disable-features=msEdgeFirstRunExperience',
    '--window-size=1120,820', '--lang=zh-CN',
  ];
  if (PROXY) args.push(`--proxy-server=${PROXY}`);
  // 注意：不要 --incognito（会拿不到持久 cookie）
  if (HEADLESS) args.push('--headless=new', '--disable-gpu', '--mute-audio');
  args.push(START_URL);

  const child = spawn(BROWSER, args, { stdio: 'ignore', detached: false });

  let version = null;
  for (let i = 0; i < 60; i++) {
    if (child.exitCode !== null) throw new Error('浏览器启动失败（已退出）');
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/version`);
      if (r.ok) { version = await r.json(); break; }
    } catch { }
    await sleep(300);
  }
  if (!version) throw new Error('浏览器调试端口没起来，可能被安全软件拦了');

  const ws = new WebSocket(version.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    ws.addEventListener('open', res);
    ws.addEventListener('error', () => rej(new Error('连不上浏览器调试通道')));
    setTimeout(() => rej(new Error('连接浏览器调试通道超时')), 15000);
  });
  const cdp = new CDP(ws);

  const { targetId } = await cdp.send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await cdp.send('Target.attachToTarget', { targetId, flatten: true });
  await cdp.send('Page.enable', {}, sessionId);
  await cdp.send('Network.enable', {}, sessionId);
  await cdp.send('Page.navigate', { url: START_URL }, sessionId).catch(() => { });

  writeState('waiting', '请在弹出的浏览器窗口里登录 YouTube 账号');

  const deadline = Date.now() + TIMEOUT;
  let exported = false;

  while (Date.now() < deadline) {
    await sleep(1500);
    let cookies = null;
    try {
      const r = await cdp.send('Network.getAllCookies', {}, sessionId);
      cookies = r.cookies || [];
    } catch {
      if (cdp.closed) break;
      continue;
    }
    if (authed(cookies)) {
      await sleep(1200); // 等剩余 cookie 落全（LOGIN_INFO 之类）
      try {
        const r2 = await cdp.send('Network.getAllCookies', {}, sessionId);
        cookies = r2.cookies || cookies;
      } catch { }
      const { text, count } = toNetscape(cookies);
      fs.writeFileSync(OUT, text, 'utf8');
      exported = true;
      writeState('logged_in', `已连接，导出 ${count} 条 cookie`, { cookies: count, headless: HEADLESS });
      break;
    }
    if (cdp.closed) break; // 用户把窗口关了
  }

  if (!exported) {
    // 窗口被关或超时：如果已经有 cookie 也存一份，聊胜于无
    writeState(cdp.closed ? 'closed' : 'timeout',
      cdp.closed ? '登录窗口已关闭，还没检测到登录' : '等待登录超时');
  }

  try { await cdp.send('Browser.close'); } catch { }
  try { ws.close(); } catch { }
  try { child.kill(); } catch { }

  process.exit(exported ? 0 : 2);
}

main().catch((e) => {
  writeState('error', String(e && e.message ? e.message : e));
  process.exit(3);
});
