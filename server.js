const http = require('http');
const fs = require('fs');
const path = require('path');
const { execSync, exec } = require('child_process');
const DIR = path.join(process.env.HOME, 'jeffrey/workspace/projects/jeffrey-os-dashboard');
const PORT = 8080;
const OC_PATH = 'PATH=/Users/magi/homebrew/bin:' + process.env.PATH;

const MIME = {
  '.html': 'text/html', '.css': 'text/css', '.js': 'application/javascript',
  '.json': 'application/json', '.png': 'image/png', '.svg': 'image/svg+xml'
};

// Regenerate state every 30s (async — never blocks HTTP requests)
let stateGenerating = false;
function regenState() {
  if (stateGenerating) return;
  stateGenerating = true;
  const child = exec('python3 update-state.py', { cwd: DIR, timeout: 25000, env: process.env });
  child.on('close', (code) => {
    if (code !== 0 && code !== null) {
      console.error(`[${new Date().toISOString()}] State generation exited with code ${code}`);
    }
    stateGenerating = false;
  });
  child.on('error', (e) => {
    console.error(`[${new Date().toISOString()}] State generation error:`, e.message);
    stateGenerating = false;
  });
  setTimeout(() => {
    if (stateGenerating) {
      child.kill();
      console.error(`[${new Date().toISOString()}] State generation timed out, killed`);
      stateGenerating = false;
    }
  }, 25000);
}
regenState();
setInterval(regenState, 30000);

const startTime = Date.now();

function corsHeaders(contentType) {
  return {
    'Content-Type': contentType || 'application/json',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Cache-Control': 'no-cache, no-store'
  };
}

function jsonRes(res, code, data) {
  res.writeHead(code, corsHeaders());
  res.end(JSON.stringify(data));
}

function runOC(cmd) {
  return execSync(cmd, { env: { ...process.env, PATH: '/Users/magi/homebrew/bin:' + process.env.PATH }, timeout: 30000, encoding: 'utf8' });
}

const server = http.createServer((req, res) => {
  let urlPath = req.url.split('?')[0];

  // Handle CORS preflight
  if (req.method === 'OPTIONS') {
    res.writeHead(204, corsHeaders());
    res.end();
    return;
  }

  // === API ROUTES ===

  // GET /api/health
  if (urlPath === '/api/health' && req.method === 'GET') {
    const stateFile = path.join(DIR, 'state.json');
    let stateAge = -1;
    try {
      const stat = fs.statSync(stateFile);
      stateAge = Math.round((Date.now() - stat.mtimeMs) / 1000);
    } catch (e) { /* no state file yet */ }
    return jsonRes(res, 200, {
      ok: true,
      status: stateAge >= 0 && stateAge < 60 ? 'healthy' : 'degraded',
      stateAgeSeconds: stateAge,
      uptime: Math.round((Date.now() - startTime) / 1000),
      time: new Date().toISOString(),
    });
  }

  // GET /api/state
  if ((urlPath === '/api/state' || urlPath === '/state.json') && req.method === 'GET') {
    const stateFile = path.join(DIR, 'state.json');
    try {
      const data = fs.readFileSync(stateFile, 'utf8');
      res.writeHead(200, corsHeaders());
      res.end(data);
    } catch (e) {
      jsonRes(res, 500, { error: 'state.json not found' });
    }
    return;
  }

  // GET /api/crons — live from openclaw
  if (urlPath === '/api/crons' && req.method === 'GET') {
    try {
      const output = runOC('openclaw cron list --json');
      res.writeHead(200, corsHeaders());
      res.end(output);
    } catch (e) {
      // Fallback to state.json crons
      try {
        const state = JSON.parse(fs.readFileSync(path.join(DIR, 'state.json'), 'utf8'));
        return jsonRes(res, 200, state.crons || []);
      } catch (e2) {
        return jsonRes(res, 500, { error: 'Failed to get crons', detail: e.message });
      }
    }
    return;
  }

  // POST /api/cron/:id/run
  const runMatch = urlPath.match(/^\/api\/cron\/([^/]+)\/run$/);
  if (runMatch && req.method === 'POST') {
    const cronId = decodeURIComponent(runMatch[1]);
    try {
      const output = runOC(`openclaw cron run ${cronId}`);
      return jsonRes(res, 200, { ok: true, cron: cronId, output: output.trim() });
    } catch (e) {
      return jsonRes(res, 500, { ok: false, cron: cronId, error: e.message });
    }
  }

  // POST /api/cron/:id/toggle
  const toggleMatch = urlPath.match(/^\/api\/cron\/([^/]+)\/toggle$/);
  if (toggleMatch && req.method === 'POST') {
    const cronId = decodeURIComponent(toggleMatch[1]);
    try {
      // Check current state from state.json
      const state = JSON.parse(fs.readFileSync(path.join(DIR, 'state.json'), 'utf8'));
      const cron = (state.crons || []).find(c => c.name === cronId);
      const action = cron && cron.enabled ? 'disable' : 'enable';
      const output = runOC(`openclaw cron ${action} ${cronId}`);
      return jsonRes(res, 200, { ok: true, cron: cronId, action, output: output.trim() });
    } catch (e) {
      return jsonRes(res, 500, { ok: false, cron: cronId, error: e.message });
    }
  }

  // GET /api/cron-log — last 60 lines of openclaw log
  if (urlPath === '/api/cron-log' && req.method === 'GET') {
    try {
      const today = new Date().toISOString().slice(0,10);
      const logFile = `/tmp/openclaw/openclaw-${today}.log`;
      const raw = fs.readFileSync(logFile, 'utf8').split('\n').filter(Boolean).slice(-60);
      const lines = raw.map(l => {
        try {
          const j = JSON.parse(l);
          // OpenClaw logs are JSON objects with msg/message fields, or sometimes arrays
          let msg;
          if (typeof j.msg === 'string') msg = j.msg;
          else if (typeof j.message === 'string') msg = j.message;
          else if (typeof j[2] === 'string') msg = j[2];
          else if (typeof j[1] === 'string') msg = j[1];
          else msg = JSON.stringify(j.msg || j.message || j[1] || j).slice(0, 120);
          const time = j.time || j.timestamp || j._meta?.date;
          const level = j.level || j._meta?.logLevelName || j.logLevel || 'INFO';
          return { time, level: String(level).toUpperCase(), msg: String(msg).slice(0, 120) };
        } catch { return { time: new Date().toISOString(), level: 'INFO', msg: l.slice(0,120) }; }
      });
      res.writeHead(200, corsHeaders());
      res.end(JSON.stringify({ lines }));
    } catch(e) {
      res.writeHead(200, corsHeaders());
      res.end(JSON.stringify({ lines: [] }));
    }
    return;
  }

  // GET /api/contribution-queue
  if (urlPath === '/api/contribution-queue' && req.method === 'GET') {
    const queueFile = path.join(process.env.HOME, 'jeffrey/workspace/projects/contribution-queue.md');
    try {
      const data = fs.readFileSync(queueFile, 'utf8');
      res.writeHead(200, corsHeaders('text/plain'));
      res.end(data);
    } catch (e) {
      jsonRes(res, 500, { error: 'contribution-queue.md not found' });
    }
    return;
  }

  // === STATIC FILES ===
  if (urlPath === '/' || urlPath === '/dashboard.html') urlPath = '/index.html';

  // Prevent path traversal
  const filePath = path.join(DIR, path.normalize(urlPath));
  if (!filePath.startsWith(DIR)) {
    res.writeHead(403);
    res.end('Forbidden');
    return;
  }

  const ext = path.extname(filePath);

  fs.readFile(filePath, (err, data) => {
    if (err) {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      res.end('Not found');
      return;
    }
    res.writeHead(200, {
      'Content-Type': MIME[ext] || 'text/plain',
      'Access-Control-Allow-Origin': '*',
      'Cache-Control': 'no-cache, no-store'
    });
    res.end(data);
  });
});

server.listen(PORT, '0.0.0.0', () => {
  console.log(`Jeffrey OS Dashboard v6.1 running on http://0.0.0.0:${PORT}`);
  console.log(`API: /api/state, /api/crons, /api/cron/:id/run, /api/cron/:id/toggle, /api/health`);
  console.log(`State refresh: 15s`);
});
