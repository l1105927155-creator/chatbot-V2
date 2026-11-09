/** Display-only native QQ wallet conversion through the NapCat plugin API. */
const registrations = new WeakMap();
const fields = ['title', 'subTitle', 'content', 'notice'];

function text(value) {
  if (typeof value !== 'string') return '';
  const wellFormed = Array.from(value, char => char.length === 1 && /[\ud800-\udfff]/.test(char)
    ? '\ufffd' : char).join('');
  return Array.from(wellFormed.replace(/[\u0000-\u001f\u007f-\u009f]/g, ' ').trim()).slice(0, 256).join('');
}

export function walletToSegment(wallet) {
  if (!wallet || typeof wallet !== 'object' || Array.isArray(wallet)) return null;
  const visible = {};
  for (const role of ['receiver', 'sender']) {
    const record = wallet[role];
    if (!record || typeof record !== 'object') continue;
    const picked = Object.fromEntries(fields.map(k => [k, text(record[k])]).filter(([,v]) => v));
    if (Object.keys(picked).length) visible[role] = picked;
  }
  const redbag = ['receiver', 'sender'].some(role => visible[role]?.content === 'QQ红包');
  const preferred = visible.receiver || visible.sender || {};
  const card = {
    app: redbag ? 'v2.qq.redbag' : 'v2.qq.wallet',
    view: 'wallet',
    prompt: preferred.notice || preferred.content || preferred.title || 'QQ钱包消息（平台未提供可读展示文字）',
    meta: Object.fromEntries(Object.entries(visible).map(([role, value]) => [role, {
      title: value.title || value.content || '', desc: value.subTitle || '',
      content: value.content || '', summary: value.notice || '',
    }])),
  };
  // JSON is understood by pinned AstrBot; no unsupported new OneBot segment,
  // wallet URL, payment token, amount or claim state is emitted.
  return {type: 'json', data: {data: JSON.stringify(card)}};
}

export async function plugin_init(ctx) {
  const table = ctx.oneBot?.apis?.MsgApi?.rawToOb11Converters;
  if (!table || typeof table !== 'object') throw new Error('Native content converter extension unavailable');
  if (registrations.has(ctx)) return;
  if (typeof table.walletElement !== 'undefined') {
    ctx.logger.info('Wallet converter already supplied; V2 leaves it in place');
    return;
  }
  const converter = async wallet => walletToSegment(wallet);
  table.walletElement = converter;
  registrations.set(ctx, {table, converter});
  ctx.logger.info('V2 display-only wallet converter enabled');
}

export async function plugin_cleanup(ctx) {
  const registered = registrations.get(ctx);
  if (!registered) return;
  if (registered.table.walletElement === registered.converter) delete registered.table.walletElement;
  registrations.delete(ctx);
}
