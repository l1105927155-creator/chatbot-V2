/* 2048 page controller: server-issued seed, local play, replay submission. */
(function () {
  'use strict';
  const tokenMatch = window.location.pathname.match(/^\/g\/([^/]+)\/2048$/);
  if (!tokenMatch) {
    window.location.replace('/');
    return;
  }
  const token = tokenMatch[1];
  const apiBase = '/api/g/' + encodeURIComponent(token);
  const game = window.V2Game2048;
  const renderer = window.V2Game2048Render;
  const $ = (id) => document.getElementById(id);

  let state = null;
  let runId = null;
  let moves = [];
  let startedAt = 0;
  let saved = false;
  let submitting = false;
  let ctx = null;
  let fit = null;
  let raf = 0;

  function status(text, kind) {
    const element = $('status');
    element.textContent = text;
    element.className = kind || '';
  }

  function updateStats() {
    $('score-value').textContent = state ? state.score : 0;
    $('max-value').textContent = state ? game.maxTile(state) : 0;
    $('moves-value').textContent = state ? state.moves : 0;
    $('save').disabled = !state || saved || submitting || !runId || state.moves === 0;
    $('restart').disabled = submitting && !state;
  }

  async function api(path, options) {
    const response = await fetch(path, Object.assign({
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
    }, options || {}));
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.message || '请求未完成');
      error.code = data.error || 'request_failed';
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function draw() {
    if (ctx && state) renderer.render2048(ctx, state);
    raf = window.requestAnimationFrame(draw);
  }

  function setupCanvas() {
    const host = $('canvas-host');
    const canvas = $('canvas');
    fit = window.V2CanvasFit.fitCanvas(host, canvas,
      renderer.LOGICAL_W, renderer.LOGICAL_H, 640);
    if (!fit) throw new Error('浏览器不支持 Canvas。');
    ctx = fit.ctx;
    window.cancelAnimationFrame(raf);
    draw();
  }

  async function startRun() {
    saved = false;
    submitting = false;
    status('正在开始新对局…');
    const run = await api(apiBase + '/runs', { method: 'POST', body: '{}' });
    runId = run.run_id;
    moves = [];
    startedAt = window.performance.now();
    state = game.createState(game.mulberry32(run.seed));
    renderer.resetRenderer();
    updateStats();
    status('进行中 · 方向键 / WASD / 滑动');
  }

  async function abandonCurrent() {
    if (!runId || saved) return;
    try {
      await api(apiBase + '/runs/' + encodeURIComponent(runId) + '/abandon',
        { method: 'POST', body: '{}' });
    } catch (error) {
      // A missing active run is harmless when starting a new game.
    }
  }

  async function saveRun(manual) {
    if (!state || saved || submitting || !runId) return;
    submitting = true;
    updateStats();
    status('正在保存，服务端回放校验中…');
    try {
      const payload = {
        moves: moves,
        client_score: state.score,
        max_tile: game.maxTile(state),
        duration_ms: Math.max(0, Math.round(window.performance.now() - startedAt)),
      };
      const result = await api(
        apiBase + '/runs/' + encodeURIComponent(runId) + '/submit',
        { method: 'POST', body: JSON.stringify(payload) });
      saved = true;
      submitting = false;
      updateStats();
      const rank = result.result && result.result.rank;
      const best = result.result && result.result.best_score;
      status('已保存 · 本群排名 ' + (rank || '未上榜') + ' · 个人最高 ' + (best || state.score),
        'ok');
    } catch (error) {
      submitting = false;
      updateStats();
      if (error.code === 'score_mismatch' || error.code === 'invalid_replay') {
        status('校验失败，未保存；请重新开始。', 'error');
      } else if (error.code === 'run_closed') {
        status('对局已结束，未保存。', 'error');
      } else {
        status('保存失败：' + error.message + '；可再次点击“保存分数”重试。', 'error');
        if (manual) {
          // Keep the run active for a network retry.
        }
      }
      if (error.code === 'run_closed' && !saved) saved = true;
    }
  }

  function moveByDirection(direction) {
    if (!state || submitting || saved || state.over) return;
    if (game.move(state, direction)) {
      moves.push(direction);
      updateStats();
      if (state.over) {
        status('游戏结束 · 正在保存…');
        saveRun(false);
      }
    }
  }

  async function restart() {
    if (submitting) return;
    await abandonCurrent();
    try {
      await startRun();
    } catch (error) {
      status('开始新对局失败：' + error.message, 'error');
    }
  }

  function bindKeys() {
    const mapping = {
      ArrowUp: 0, KeyW: 0,
      ArrowRight: 1, KeyD: 1,
      ArrowDown: 2, KeyS: 2,
      ArrowLeft: 3, KeyA: 3,
    };
    window.addEventListener('keydown', (event) => {
      if (event.code === 'KeyS' && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        saveRun(true);
        return;
      }
      if (mapping[event.code] !== undefined) {
        event.preventDefault();
        moveByDirection(mapping[event.code]);
        return;
      }
      if (event.code === 'KeyR') {
        event.preventDefault();
        restart();
      }
    });
    const host = $('canvas-host');
    let startX = 0;
    let startY = 0;
    let tracking = false;
    host.addEventListener('touchstart', (event) => {
      if (event.touches.length !== 1) return;
      tracking = true;
      startX = event.touches[0].clientX;
      startY = event.touches[0].clientY;
    }, { passive: true });
    host.addEventListener('touchmove', (event) => {
      if (tracking) event.preventDefault();
    }, { passive: false });
    host.addEventListener('touchend', (event) => {
      if (!tracking || !event.changedTouches.length) return;
      tracking = false;
      const dx = event.changedTouches[0].clientX - startX;
      const dy = event.changedTouches[0].clientY - startY;
      if (Math.max(Math.abs(dx), Math.abs(dy)) < 24) return;
      if (Math.abs(dx) > Math.abs(dy)) moveByDirection(dx > 0 ? 1 : 3);
      else moveByDirection(dy > 0 ? 2 : 0);
    }, { passive: true });
  }

  async function boot() {
    $('back').href = '/g/' + encodeURIComponent(token);
    $('save').addEventListener('click', () => saveRun(true));
    $('restart').addEventListener('click', restart);
    bindKeys();
    try {
      await api(apiBase + '/meta');
      const me = await api(apiBase + '/me');
      $('identity').textContent = me.display_name || '已绑定';
      $('best-value').textContent = me.best_score || 0;
    } catch (error) {
      if (error.status === 401) {
        window.location.replace('/g/' + encodeURIComponent(token));
        return;
      }
      status('身份读取失败：' + error.message, 'error');
      return;
    }
    setupCanvas();
    try {
      await startRun();
    } catch (error) {
      status('开始新对局失败：' + error.message, 'error');
    }
  }

  boot();
})();
