'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { render } = require('../dsh/skills/doodle/render.cjs');
let called = 0;
const fakeRough = {
  generator(options) {
    assert.equal(options.options.disableMultiStroke, true);
    assert.equal(options.options.disableMultiStrokeFill, true);
    return {
      path(d, opts) {
        called++;
        assert.equal(opts.disableMultiStroke, true);
        assert.equal(opts.disableMultiStrokeFill, true);
        assert.equal(opts.fill, undefined);
        assert.ok(opts.seed > 0);
        return { d };
      },
      toPaths(drawable) { return [{d: drawable.d}]; }
    };
  }
};
const sample = JSON.parse(fs.readFileSync(path.join(__dirname, '../dsh/skills/doodle/example.json'), 'utf8'));
const svg = render(sample, fakeRough);
assert.equal(called, sample.paths.length);
assert.match(svg, /^<svg /);
assert.match(svg, /<path d="M 252 134/);
assert.match(svg, /<text x="304"/);
assert.ok(!svg.includes('<script'));
assert.equal(svg, render(sample, fakeRough));
assert.match(render({...sample,labels:[{text:'<bad&"\'>',x:10,y:20}]},fakeRough), /&lt;bad&amp;&quot;&apos;&gt;/);
assert.throws(() => render({...sample, paths:[{d:'M 0 0" onload="alert(1)'}]},fakeRough), /not an allowed SVG path/);
assert.throws(() => render({...sample,paths:[]},fakeRough), /paths must contain/);
assert.throws(() => render({...sample,width:Infinity},fakeRough), /width must be/);
assert.throws(() => render({...sample,ink:'url(https://a.test)'},fakeRough), /six-digit hex color/);
assert.throws(() => render({...sample,labels:[{text:'bad',x:0,y:9999}]},fakeRough), /labels\[0\].y/);
console.log('doodle renderer contract tests passed');
