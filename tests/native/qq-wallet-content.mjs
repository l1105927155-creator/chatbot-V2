import assert from 'node:assert/strict';
import {walletToSegment, plugin_init, plugin_cleanup} from '../../napcat-plugins/v2_content/index.mjs';

const native = {
  receiver: {title: '恭喜发财', subTitle: '赶紧点击拆开吧', content: 'QQ红包', notice: '[QQ红包]恭喜发财',
    nativeAndroid: 'payment-operation', jumpUrl: 'https://wallet.invalid/secret'},
  authkey: 'PRIVATE_AUTH', billNo: 'PRIVATE_BILL', grabbedAmount: '100', grabState: 1,
};
const encoded = walletToSegment(native);
assert.equal(encoded.type, 'json');
const card = JSON.parse(encoded.data.data);
assert.equal(card.app, 'v2.qq.redbag');
assert.equal(card.meta.receiver.title, '恭喜发财');
assert.equal(card.prompt, '[QQ红包]恭喜发财');
assert(!/PRIVATE|wallet.invalid|grabbedAmount|grabState|payment-operation/.test(JSON.stringify(encoded)));
assert.equal(JSON.parse(walletToSegment({receiver:{content:'QQ转账'}}).data.data).app, 'v2.qq.wallet');
assert.equal(JSON.parse(walletToSegment({receiver:{title:'给你的红包',content:'QQ转账',notice:'红包请收款'}}).data.data).app, 'v2.qq.wallet');
assert.equal(JSON.parse(walletToSegment({receiver:{title:'红包祝福'}}).data.data).app, 'v2.qq.wallet');
assert.equal(JSON.parse(walletToSegment({sender:{title:'任意祝福',content:'QQ红包'}}).data.data).app, 'v2.qq.redbag');
assert.equal(walletToSegment(null), null);
assert.equal(walletToSegment([]), null);
assert.equal(JSON.parse(walletToSegment({}).data.data).app, 'v2.qq.wallet');
assert(JSON.stringify(walletToSegment({receiver:{title:'字'.repeat(100000)}})).length < 3000);
assert(!JSON.stringify(walletToSegment({receiver:{title:'bad\u0001\ud800'}})).includes('\\ud800'));
assert.equal(JSON.parse(walletToSegment({receiver:{title:'🎉红包'}}).data.data).meta.receiver.title, '🎉红包');

const table = {};
const ctx = {oneBot:{apis:{MsgApi:{rawToOb11Converters:table}}}, logger:{info(){}}};
await plugin_init(ctx);
const converter = table.walletElement;
assert.equal(typeof converter, 'function');
assert.deepEqual(await converter(native), encoded);
await plugin_init(ctx);
assert.equal(table.walletElement, converter);
await plugin_cleanup(ctx);
assert.equal(table.walletElement, undefined);
await plugin_init(ctx);
const replacement = () => 'another provider';
table.walletElement = replacement;
await plugin_cleanup(ctx);
assert.equal(table.walletElement, replacement);
await plugin_init(ctx);
assert.equal(table.walletElement, replacement);
console.log('Wallet display conversion, privacy, boundedness and native plugin lifecycle passed');
