/* 2048 canvas renderer adapted from lhh010/dsh-minigames (BSD-3-Clause). */
(function () {
  'use strict';
  const CELL = 64;
  const GAP = 8;
  const HUD_H = 36;
  const BOARD_W = 4 * CELL + 5 * GAP;
  const BOARD_H = 4 * CELL + 5 * GAP;
  const LOGICAL_W = BOARD_W;
  const LOGICAL_H = HUD_H + BOARD_H;

  const particles = window.V2Theme.createParticles();
  let previousScore = -1;
  let previousNow = 0;

  function tileColor(value) {
    return window.V2Theme.accent(Math.log2(value) - 1);
  }

  function tilePos(index) {
    return GAP + index * (CELL + GAP);
  }

  function render2048(ctx, state) {
    const theme = window.V2Theme;
    const now = performance.now();
    const dt = Math.min(50, Math.max(0, now - previousNow));
    previousNow = now;

    theme.drawBackground(ctx, LOGICAL_W, LOGICAL_H);
    theme.hudPair(ctx, 10, 24, '', '2048', 'left');
    theme.hudPair(ctx, LOGICAL_W - 10, 24, '得分', String(state.score), 'right');

    ctx.fillStyle = theme.THEME.panel;
    theme.glow(ctx, theme.THEME.panelEdge, 8);
    theme.roundRectPath(ctx, 0, HUD_H, BOARD_W, BOARD_H, 10);
    ctx.fill();
    theme.noGlow(ctx);
    ctx.strokeStyle = theme.THEME.panelEdge;
    ctx.lineWidth = 1;
    theme.roundRectPath(ctx, 0.5, HUD_H + 0.5, BOARD_W - 1, BOARD_H - 1, 10);
    ctx.stroke();

    for (let r = 0; r < 4; r += 1) {
      for (let c = 0; c < 4; c += 1) {
        const x = tilePos(c);
        const y = HUD_H + tilePos(r);
        const value = state.grid[r][c];
        if (value === null) {
          ctx.fillStyle = theme.THEME.bgBottom;
          theme.roundRectPath(ctx, x, y, CELL, CELL, 8);
          ctx.fill();
          continue;
        }
        const color = tileColor(value);
        ctx.fillStyle = color;
        theme.glow(ctx, color, Math.min(18, 4 + Math.log2(value) * 1.2));
        theme.roundRectPath(ctx, x, y, CELL, CELL, 8);
        ctx.fill();
        theme.noGlow(ctx);
        ctx.fillStyle = value <= 4 ? theme.THEME.text : '#ffffff';
        ctx.font = value >= 1024 ? 'bold 18px system-ui, sans-serif'
          : 'bold 24px system-ui, sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(String(value), x + CELL / 2, y + CELL / 2);
      }
    }
    ctx.textBaseline = 'alphabetic';

    if (state.score !== previousScore) {
      if (previousScore >= 0 && state.score > previousScore) {
        particles.burst(BOARD_W / 2, HUD_H + BOARD_H / 2, theme.THEME.good, 12, 160);
      }
      previousScore = state.score;
    }
    particles.tick(ctx, dt);

    if (state.won && !state.over) {
      theme.drawOverlay(ctx, LOGICAL_W, LOGICAL_H, '达成 2048！',
                        '继续挑战更高分 · 按 R 重新开始', theme.THEME.gold);
    } else if (state.over) {
      theme.drawOverlay(ctx, LOGICAL_W, LOGICAL_H, '游戏结束',
                        '得分 ' + state.score + ' · 按 R 重新开始', theme.THEME.bad);
    }
  }

  function resetRenderer() {
    previousScore = -1;
    previousNow = 0;
  }

  window.V2Game2048Render = {
    CELL, GAP, HUD_H, BOARD_W, BOARD_H, LOGICAL_W, LOGICAL_H,
    render2048, resetRenderer,
  };
})();
