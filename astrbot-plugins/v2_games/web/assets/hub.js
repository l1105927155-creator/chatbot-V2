/* Group hub and landing page controller. */
(function () {
  'use strict';
  const match = window.location.pathname.match(/^\/g\/([^/]+)$/);
  const token = match ? match[1] : null;
  const apiBase = token ? '/api/g/' + encodeURIComponent(token) : null;
  const $ = (id) => document.getElementById(id);

  function notice(text, kind) {
    const element = $(token ? 'notice' : 'landing-notice');
    element.textContent = text || '';
    element.className = kind || 'muted';
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

  function renderGames(games, targetId) {
    const host = $(targetId);
    host.replaceChildren();
    if (!games.length) {
      host.textContent = '暂无游戏';
      return;
    }
    games.forEach((game) => {
      const card = document.createElement('article');
      card.className = 'game-card';
      const title = document.createElement('h3');
      title.textContent = game.title;
      const desc = document.createElement('p');
      desc.textContent = game.description;
      card.appendChild(title);
      card.appendChild(desc);
      if (token && game.id === '2048') {
        const link = document.createElement('a');
        link.className = 'button';
        link.href = '/g/' + encodeURIComponent(token) + '/2048';
        link.textContent = '进入游戏';
        card.appendChild(link);
      } else {
        const hint = document.createElement('span');
        hint.className = 'muted';
        hint.textContent = token ? '即将开放' : '请从群内入口进入';
        card.appendChild(hint);
      }
      host.appendChild(card);
    });
  }

  function formatTime(value) {
    if (!value) return '—';
    const date = new Date(Number(value) * 1000);
    if (Number.isNaN(date.getTime())) return '—';
    return date.toLocaleString('zh-CN', { hour12: false });
  }

  function renderBoard(board) {
    const body = $('leaderboard-body');
    body.replaceChildren();
    const rows = board.rows || [];
    if (!rows.length) {
      const row = document.createElement('tr');
      const cell = document.createElement('td');
      cell.colSpan = 5;
      cell.className = 'muted';
      cell.textContent = '还没有成绩，先玩一局吧。';
      row.appendChild(cell);
      body.appendChild(row);
      return;
    }
    rows.forEach((item) => {
      const row = document.createElement('tr');
      if (item.is_me) row.className = 'me';
      const cells = [
        String(item.rank),
        item.display_name,
        String(item.score),
        String(item.max_tile),
        formatTime(item.ended_at),
      ];
      cells.forEach((value, index) => {
        const cell = document.createElement('td');
        cell.textContent = value;
        if (index >= 2 && index <= 3) cell.className = 'number';
        row.appendChild(cell);
      });
      body.appendChild(row);
    });
  }

  async function loadBoard() {
    if (!token) return;
    const period = $('period').value;
    const kind = $('kind').value;
    notice('正在读取排行榜…');
    try {
      const board = await api(apiBase + '/leaderboard?period=' +
        encodeURIComponent(period) + '&kind=' + encodeURIComponent(kind) + '&limit=20');
      renderBoard(board);
      notice('本群排行榜 · 总榜 / 周榜按服务端成绩');
    } catch (error) {
      notice('排行榜读取失败：' + error.message, 'error');
    }
  }

  function showIdentity(identity) {
    if (identity) {
      $('identity-form').hidden = true;
      $('identity-current').hidden = false;
      $('identity-label').textContent = identity.qq_masked || '已绑定';
      $('best-label').textContent = '已填写 QQ，成绩按本群记录';
    } else {
      $('identity-form').hidden = false;
      $('identity-current').hidden = true;
    }
  }

  async function loadMeta() {
    if (!token) {
      const data = await api('/api/games');
      renderGames(data.games || [], 'landing-games');
      return;
    }
    const meta = await api(apiBase + '/meta');
    renderGames(meta.games || [], 'games-list');
    showIdentity(meta.identity);
  }

  async function submitIdentity(event) {
    event.preventDefault();
    const qq = $('qq').value.trim();
    if (!/^[1-9][0-9]{4,11}$/.test(qq)) {
      notice('请输入 5-12 位 QQ 号。', 'error');
      return;
    }
    notice('正在保存身份…');
    try {
      const result = await api(apiBase + '/identity', {
        method: 'POST',
        body: JSON.stringify({ qq }),
      });
      showIdentity({ qq_masked: result.me.display_name });
      notice('身份已保存；公开榜只显示掩码 QQ。');
      await loadBoard();
    } catch (error) {
      notice('身份保存失败：' + error.message, 'error');
    }
  }

  function showChangeIdentity() {
    $('identity-current').hidden = true;
    $('identity-form').hidden = false;
    $('qq').focus();
  }

  async function boot() {
    const groupMode = Boolean(token);
    $('landing').hidden = groupMode;
    $('hub').hidden = !groupMode;
    if (!groupMode) {
      try {
        const data = await api('/api/games');
        renderGames(data.games || [], 'landing-games');
        notice('这是本机小游戏入口。请在 QQ 群发送 /games 获取本群游戏链接。');
      } catch (error) {
        notice('游戏目录读取失败：' + error.message, 'error');
      }
      return;
    }
    $('identity-form').addEventListener('submit', submitIdentity);
    $('change-identity').addEventListener('click', showChangeIdentity);
    $('period').addEventListener('change', loadBoard);
    $('kind').addEventListener('change', loadBoard);
    $('refresh').addEventListener('click', loadBoard);
    try {
      await loadMeta();
      await loadBoard();
    } catch (error) {
      if (error.status === 404 || error.code === 'group_not_found') {
        notice('群链接无效或已失效，请回到 QQ 群重新发送 /games。', 'error');
        $('hub').hidden = true;
      } else {
        notice('页面读取失败：' + error.message, 'error');
      }
    }
  }

  boot();
})();
