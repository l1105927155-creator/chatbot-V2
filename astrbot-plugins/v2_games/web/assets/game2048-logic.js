/* 2048 rules shared with the server's Python replay. */
(function () {
  'use strict';
  const SIZE = 4;
  const WIN_VALUE = 2048;

  function mulberry32(seed) {
    let a = seed >>> 0;
    return function () {
      a = (a + 0x6D2B79F5) >>> 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a) >>> 0;
      const product = (Math.imul((t ^ (t >>> 7)) >>> 0, 61 | t)) >>> 0;
      t = (((t + product) >>> 0) ^ t) >>> 0;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function emptyGrid() {
    return Array.from({ length: SIZE }, () => Array(SIZE).fill(null));
  }

  function emptyCells(grid) {
    const out = [];
    for (let r = 0; r < SIZE; r += 1) {
      for (let c = 0; c < SIZE; c += 1) {
        if (grid[r][c] === null) out.push([r, c]);
      }
    }
    return out;
  }

  function spawnTile(state) {
    const empty = emptyCells(state.grid);
    if (!empty.length) return;
    const pick = empty[Math.floor(state.rng() * empty.length)];
    state.grid[pick[0]][pick[1]] = state.rng() < 0.9 ? 2 : 4;
  }

  function createState(rng) {
    const state = {
      grid: emptyGrid(), score: 0, won: false, over: false, moves: 0,
      rng: rng || Math.random,
    };
    spawnTile(state);
    spawnTile(state);
    return state;
  }

  function slideLine(line) {
    const kept = line.filter((v) => v !== null);
    const merged = [];
    let gained = 0;
    let i = 0;
    while (i < kept.length) {
      if (i + 1 < kept.length && kept[i] === kept[i + 1]) {
        const value = kept[i] * 2;
        merged.push(value);
        gained += value;
        i += 2;
      } else {
        merged.push(kept[i]);
        i += 1;
      }
    }
    while (merged.length < SIZE) merged.push(0);
    let moved = false;
    for (let p = 0; p < SIZE; p += 1) {
      if ((line[p] || 0) !== (merged[p] || 0)) moved = true;
      line[p] = merged[p] || null;
    }
    return { moved, gained };
  }

  function extract(grid, axis, index, reverse) {
    const line = [];
    for (let i = 0; i < SIZE; i += 1) {
      line.push(axis === 'row' ? grid[index][i] : grid[i][index]);
    }
    return reverse ? line.reverse() : line;
  }

  function write(grid, axis, index, reverse, line) {
    const data = reverse ? line.slice().reverse() : line;
    for (let i = 0; i < SIZE; i += 1) {
      if (axis === 'row') grid[index][i] = data[i];
      else grid[i][index] = data[i];
    }
  }

  function couldMove(grid) {
    for (let r = 0; r < SIZE; r += 1) {
      for (let c = 0; c < SIZE; c += 1) {
        const value = grid[r][c];
        if (value === null) return true;
        if (r + 1 < SIZE && grid[r + 1][c] === value) return true;
        if (c + 1 < SIZE && grid[r][c + 1] === value) return true;
      }
    }
    return false;
  }

  function canMove(state, dir) {
    const axis = dir === 0 || dir === 2 ? 'col' : 'row';
    const reverse = dir === 1 || dir === 2;
    for (let i = 0; i < SIZE; i += 1) {
      const line = extract(state.grid, axis, i, reverse);
      if (slideLine(line).moved) return true;
    }
    return false;
  }

  function move(state, dir) {
    if (state.over) return false;
    const axis = dir === 0 || dir === 2 ? 'col' : 'row';
    const reverse = dir === 1 || dir === 2;
    let moved = false;
    let gained = 0;
    for (let i = 0; i < SIZE; i += 1) {
      const line = extract(state.grid, axis, i, reverse);
      const result = slideLine(line);
      if (result.moved) {
        moved = true;
        gained += result.gained;
        write(state.grid, axis, i, reverse, line);
      }
    }
    if (!moved) return false;
    state.score += gained;
    state.moves += 1;
    for (let r = 0; r < SIZE; r += 1) {
      for (let c = 0; c < SIZE; c += 1) {
        if (state.grid[r][c] !== null && state.grid[r][c] >= WIN_VALUE) state.won = true;
      }
    }
    spawnTile(state);
    if (!couldMove(state.grid)) state.over = true;
    return true;
  }

  function maxTile(state) {
    let best = 0;
    for (let r = 0; r < SIZE; r += 1) {
      for (let c = 0; c < SIZE; c += 1) {
        if (state.grid[r][c] !== null && state.grid[r][c] > best) best = state.grid[r][c];
      }
    }
    return best;
  }

  function legalDirections(state) {
    if (state.over) return [];
    const names = ['up', 'right', 'down', 'left'];
    return names.filter((_, index) => canMove(state, index));
  }

  window.V2Game2048 = {
    SIZE, WIN_VALUE, mulberry32, createState, move, canMove, maxTile,
    legalDirections, emptyGrid,
  };
})();
