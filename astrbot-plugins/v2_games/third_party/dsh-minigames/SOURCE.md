# Third-party source

- Repository: https://github.com/lhh010/dsh-minigames
- Commit: `5e306441a7dcc5b758695e94d5fc8f8c0dc9a11b`
- License: BSD 3-Clause (see `LICENSE` in this directory)
- Upstream version at snapshot: 0.9.0 (2026-10-28)

Adapted files under `web/assets/`:

- `theme.js` — from `src/client/games/theme.ts`
- `canvas-fit.js` — from `src/client/games/canvas-fit.ts`
- `game2048-logic.js` — from `src/client/games/game2048/logic.ts`
- `game2048-render.js` — from `src/client/games/game2048/render.ts`

The page controller, Python engine and server-side replay verification are
V2-authored. The upstream 2048 module uses the same board rules but was not
vendored wholesale; only the drawing helpers and rules were adapted.
