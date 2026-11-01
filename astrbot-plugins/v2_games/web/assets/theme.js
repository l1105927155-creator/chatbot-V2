/* Shared canvas visual kit adapted from lhh010/dsh-minigames (BSD-3-Clause). */
(function () {
  'use strict';
  const THEME = {
    bgTop: '#181822',
    bgBottom: '#10101a',
    panel: '#1e1e2a',
    panelEdge: '#34344a',
    text: '#e2e2ee',
    textMuted: 'rgba(226,226,238,0.55)',
    accents: ['#7aa2f7', '#bb9af7', '#9ece6a', '#e0af68', '#f7768e', '#7dcfff', '#ff9e64', '#73daca'],
    good: '#9ece6a',
    bad: '#f7768e',
    gold: '#e0af68',
  };

  function accent(index) {
    const size = THEME.accents.length;
    return THEME.accents[((index % size) + size) % size];
  }

  function drawBackground(ctx, w, h) {
    const gradient = ctx.createLinearGradient(0, 0, 0, h);
    gradient.addColorStop(0, THEME.bgTop);
    gradient.addColorStop(1, THEME.bgBottom);
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, w, h);
  }

  function glow(ctx, color, blur) {
    ctx.shadowColor = color;
    ctx.shadowBlur = blur === undefined ? 10 : blur;
  }

  function noGlow(ctx) {
    ctx.shadowBlur = 0;
    ctx.shadowColor = 'transparent';
  }

  function roundRectPath(ctx, x, y, w, h, r) {
    const radius = Math.min(r, w / 2, h / 2);
    if (typeof ctx.roundRect === 'function') {
      ctx.beginPath();
      ctx.roundRect(x, y, w, h, radius);
      return;
    }
    ctx.beginPath();
    ctx.moveTo(x + radius, y);
    ctx.arcTo(x + w, y, x + w, y + h, radius);
    ctx.arcTo(x + w, y + h, x, y + h, radius);
    ctx.arcTo(x, y + h, x, y, radius);
    ctx.arcTo(x, y, x + w, y, radius);
    ctx.closePath();
  }

  function hudPair(ctx, x, y, label, value, align, valueColor) {
    ctx.font = '600 13px system-ui, sans-serif';
    if (align === 'right') {
      ctx.textAlign = 'right';
      const valueWidth = ctx.measureText(value).width;
      ctx.fillStyle = valueColor || THEME.text;
      ctx.fillText(value, x, y);
      ctx.fillStyle = THEME.textMuted;
      ctx.fillText(label, x - valueWidth - 6, y);
      return;
    }
    ctx.textAlign = align || 'left';
    ctx.fillStyle = THEME.textMuted;
    ctx.fillText(label, x, y);
    ctx.fillStyle = valueColor || THEME.text;
    ctx.fillText(value, x + ctx.measureText(label + ' ').width, y);
  }

  function spaced(text) {
    return Array.from(text).join(' ');
  }

  function drawOverlay(ctx, w, h, title, subtitle, titleColor) {
    ctx.fillStyle = 'rgba(13,13,20,0.74)';
    ctx.fillRect(0, 0, w, h);
    ctx.textAlign = 'center';
    ctx.font = 'bold 26px system-ui, sans-serif';
    ctx.fillStyle = titleColor || THEME.text;
    ctx.fillText(spaced(title), w / 2, h / 2 - 8);
    ctx.font = '600 13px system-ui, sans-serif';
    ctx.fillStyle = THEME.textMuted;
    ctx.fillText(subtitle, w / 2, h / 2 + 20);
  }

  function createParticles() {
    const pool = [];
    function spawn(x, y, color, speed) {
      if (pool.length >= 160) pool.shift();
      const angle = Math.random() * Math.PI * 2;
      const velocity = speed * (0.35 + Math.random() * 0.65);
      pool.push({
        x, y,
        vx: Math.cos(angle) * velocity,
        vy: Math.sin(angle) * velocity - speed * 0.25,
        life: 0,
        maxLife: 380 + Math.random() * 320,
        size: 2 + Math.random() * 3,
        color,
        circle: Math.random() < 0.5,
      });
    }
    return {
      burst(x, y, color, count, speed) {
        const total = count === undefined ? 10 : count;
        const velocity = speed === undefined ? 130 : speed;
        for (let i = 0; i < total; i += 1) spawn(x, y, color, velocity);
      },
      tick(ctx, dtMs) {
        const dt = dtMs / 1000;
        for (let i = pool.length - 1; i >= 0; i -= 1) {
          const particle = pool[i];
          particle.life += dtMs;
          if (particle.life >= particle.maxLife) {
            pool.splice(i, 1);
            continue;
          }
          particle.x += particle.vx * dt;
          particle.y += particle.vy * dt;
          particle.vy += 260 * dt;
          const alpha = 1 - particle.life / particle.maxLife;
          ctx.globalAlpha = alpha;
          ctx.fillStyle = particle.color;
          glow(ctx, particle.color, 6);
          if (particle.circle) {
            ctx.beginPath();
            ctx.arc(particle.x, particle.y, particle.size * alpha, 0, Math.PI * 2);
            ctx.fill();
          } else {
            ctx.fillRect(particle.x - particle.size / 2,
                         particle.y - particle.size / 2,
                         particle.size, particle.size);
          }
          noGlow(ctx);
        }
        ctx.globalAlpha = 1;
      },
      size() { return pool.length; },
    };
  }

  window.V2Theme = {
    THEME, accent, drawBackground, glow, noGlow, roundRectPath, hudPair,
    spaced, drawOverlay, createParticles,
  };
})();
