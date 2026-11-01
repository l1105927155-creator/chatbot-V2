/* Fit a fixed logical canvas into its host at device-pixel resolution. */
(function () {
  'use strict';
  function fitCanvas(host, canvas, logicalW, logicalH, maxWidth) {
    const ctx = canvas.getContext('2d');
    if (!ctx) return null;
    const cap = maxWidth === undefined ? 960 : maxWidth;
    function apply() {
      const dpr = window.devicePixelRatio || 1;
      const availW = host.clientWidth;
      const availH = host.clientHeight;
      let cssW = Math.min(availW > 0 ? availW : logicalW, cap);
      let cssH = cssW * (logicalH / logicalW);
      if (availH > 0 && cssH > availH) {
        cssH = availH;
        cssW = cssH * (logicalW / logicalH);
      }
      canvas.style.width = cssW + 'px';
      canvas.style.height = cssH + 'px';
      canvas.width = Math.max(1, Math.round(cssW * dpr));
      canvas.height = Math.max(1, Math.round(cssH * dpr));
      ctx.setTransform((cssW / logicalW) * dpr, 0, 0, (cssH / logicalH) * dpr, 0, 0);
    }
    apply();
    const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(apply) : null;
    if (observer) observer.observe(host);
    return {
      ctx,
      dispose() { if (observer) observer.disconnect(); },
    };
  }
  window.V2CanvasFit = { fitCanvas };
})();
