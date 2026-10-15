(() => {
  'use strict';

  const API = '/api';
  const state = { data: null, active: null, selectedRaw: null, reply: null, attachments: [], polling: false, renderedEvents: null, renderedConversation: null, scenario: null, scenarioStamp: null, faults: [], evidence: null };
  const $ = (selector) => document.querySelector(selector);
  const el = {
    connectionDot: $('#connection-dot'), connectionLabel: $('#connection-label'),
    conversationList: $('#conversation-list'), memberList: $('#member-list'), sender: $('#sender-select'),
    messages: $('#messages'), chatName: $('#chat-name'), chatSubtitle: $('#chat-subtitle'), chatAvatar: $('#chat-avatar'),
    target: $('#send-target'), input: $('#message-input'), composer: $('#composer'), rawContent: $('#raw-content'),
    inspector: $('#inspector'), toast: $('#toast'), attachments: $('#attachments'), file: $('#file-input'),
    replyBanner: $('#reply-banner'), replyLabel: $('#reply-label'), fixtures: $('#fixture-picker'),
  };

  async function api(path, options = {}) {
    const response = await fetch(`${API}${path}`, options);
    const type = response.headers.get('content-type') || '';
    const result = type.includes('application/json') ? await response.json() : await response.text();
    if (!response.ok) throw new Error(result?.error || result?.detail || `请求失败 (${response.status})`);
    return result;
  }

  function escText(value) { return value == null ? '' : String(value); }
  function idOf(value) { return String(value ?? ''); }
  function avatarLabel(name) { const s = escText(name).trim(); return s ? [...s][0].toUpperCase() : '?'; }
  function showToast(message, error = false) {
    el.toast.textContent = message;
    el.toast.classList.toggle('error', error);
    el.toast.hidden = false;
    clearTimeout(showToast.timer);
    showToast.timer = setTimeout(() => { el.toast.hidden = true; }, 2800);
  }

  function activeConversation() {
    return state.data?.conversations?.find((c) => state.active && c.type === state.active.type && idOf(c.id) === idOf(state.active.id));
  }
  function convKey(conv) { return `${conv.type}:${conv.id}`; }

  function rawEvent(record) { return record?.event || record?.raw_event || record?.data || record || {}; }
  function normalizeMessage(record) {
    const event = rawEvent(record);
    let message = event?.message ?? event?.raw_event?.message ?? event?.data?.message ?? [];
    if (typeof message === 'string') {
      try { message = JSON.parse(message); } catch { message = [{ type: 'text', data: { text: message } }]; }
    }
    if (!Array.isArray(message)) message = [];
    return message;
  }
  function eventId(record, index) {
    const event = rawEvent(record);
    return String(record?.message_id ?? event?.message_id ?? event?.messageId ?? record?.id ?? event?.id ?? event?.event_id ?? `event-${index}`);
  }
  function eventConversation(record) {
    if (record?.conversation?.type && record?.conversation?.id != null) return { type: record.conversation.type, id: idOf(record.conversation.id) };
    const source = rawEvent(record);
    const type = source?.message_type || (source?.group_id != null ? 'group' : 'private');
    const id = source?.group_id ?? source?.user_id ?? source?.conversation_id ?? event?.conversation?.id;
    return id == null ? null : { type, id: String(id) };
  }
  function eventSender(record) {
    const source = rawEvent(record);
    const sender = source?.sender || {};
    const id = record?.sender_id ?? sender.user_id ?? source?.user_id ?? source?.self_id ?? '';
    const member = state.data?.members?.find((m) => idOf(m.id) === idOf(id));
    const bot = record?.direction === 'outbound' || idOf(state.data?.bot?.id) === idOf(id);
    return { id: idOf(id), name: bot ? (state.data?.bot?.nickname || 'Bot') : (sender.card || sender.nickname || member?.card || member?.nickname || `成员 ${id}`), bot };
  }
  function displayEvents() {
    const all = state.data?.events || [];
    const conv = activeConversation();
    if (!conv) return [];
    return all.map((record, index) => ({ record, event: rawEvent(record), index, id: eventId(record, index), messageId: record?.message_id ?? rawEvent(record)?.message_id, direction: record?.direction, createdAt: record?.created_at, conv: eventConversation(record), sender: eventSender(record), segments: normalizeMessage(record) }))
      .filter((item) => item.conv && item.conv.type === conv.type && idOf(item.conv.id) === idOf(conv.id));
  }
  function feedbackFor(record) {
    const messageId = String(record?.message_id ?? rawEvent(record)?.message_id ?? '');
    return (state.data?.actions || []).find((action) => String(action.response?.data?.message_id ?? '') === messageId)?.feedback || null;
  }

  function localAssetHref(value) {
    let candidate = escText(value);
    if (candidate.startsWith('sandbox://')) candidate = `/api/assets/${encodeURIComponent(candidate.slice('sandbox://'.length))}`;
    try {
      const url = new URL(candidate, window.location.origin);
      return url.origin === window.location.origin && url.pathname.startsWith('/api/assets/') ? `${url.pathname}${url.search}` : null;
    } catch { return null; }
  }
  function appendMedia(container, segment) {
    const data = segment.data || {};
    const assetId = data.asset_id || data.file_id || (escText(data.file).startsWith('sandbox://') ? escText(data.file).slice(10) : '');
    const href = localAssetHref(data.url || (assetId ? `/api/assets/${encodeURIComponent(assetId)}` : data.file));
    const name = safeBasename(data.name || data.file_name || data.file || (segment.type === 'image' ? '图片' : '文件'));
    if (segment.type === 'image' && href) {
      const link = document.createElement('a'); link.className = 'image-preview-link'; link.href = href; link.target = '_blank'; link.rel = 'noopener'; link.title = name;
      const image = document.createElement('img'); image.className = 'image-preview'; image.src = href; image.alt = name; image.loading = 'lazy';
      link.append(image); container.append(link); return;
    }
    if (href && segment.type === 'file') {
      const link = document.createElement('a'); link.className = 'download-card'; link.href = href; link.download = name;
      const icon = document.createElement('span'); icon.textContent = '▤';
      const label = document.createElement('span'); label.className = 'download-label'; label.textContent = name;
      const hint = document.createElement('small'); hint.textContent = data.size ? `${formatSize(data.size)} · 下载` : '下载文件';
      label.append(hint); link.append(icon, label); container.append(link); return;
    }
    const card = document.createElement('span'); card.className = `media-segment ${segment.type}`;
    card.textContent = `${segment.type === 'image' ? '▧' : segment.type === 'file' ? '▤' : '◉'} ${name}`; container.append(card);
  }
  function formatSize(size) { const value = Number(size); return !Number.isFinite(value) || value < 0 ? '' : value < 1024 ? `${value} B` : value < 1048576 ? `${(value / 1024).toFixed(1)} KB` : `${(value / 1048576).toFixed(1)} MB`; }
  function appendMessageText(container, segments, depth = 0) {
    for (const seg of segments) {
      const data = seg?.data || {};
      if (seg?.type === 'text') container.append(document.createTextNode(escText(data.text)));
      else if (seg?.type === 'at') {
        const chip = document.createElement('span'); chip.className = 'at-chip';
        const target = idOf(data.qq);
        const member = state.data?.members?.find((m) => idOf(m.id) === target);
        chip.textContent = `@${target === 'all' ? '全体成员' : (member?.card || member?.nickname || target)}`;
        container.append(chip);
      } else if (seg?.type === 'face' || seg?.type === 'emoji') {
        const face = document.createElement('span'); face.className = 'face-segment'; face.textContent = `表情 ${escText(data.id ?? data.face_id ?? '☺')}`; container.append(face);
      } else if (['image', 'file', 'video', 'record'].includes(seg?.type)) {
        appendMedia(container, seg);
      } else if (seg?.type === 'reply') {
        const chip = document.createElement('span'); chip.className = 'reply-segment'; chip.textContent = `↶ 引用 ${escText(data.id ?? '')}`; container.append(chip);
      } else if (seg?.type === 'node' || seg?.type === 'forward') {
        if (Array.isArray(data.content)) {
          const node = document.createElement('details'); node.className = 'forward-node';
          node.open = seg.type === 'node' && depth > 0;
          const summary = document.createElement('summary'); summary.textContent = `▤ ${escText(data.name || data.nickname || data.uin || data.user_id || '转发节点')}`;
          const content = document.createElement('div'); content.className = 'forward-node-content'; appendMessageText(content, data.content, depth + 1);
          node.append(summary, content); container.append(node);
        } else {
          const node = document.createElement('details'); node.className = 'forward-node';
          const summary = document.createElement('summary'); summary.textContent = `▤ 合并转发 · ${escText(data.name || data.nickname || data.uin || data.user_id || '展开')}`; node.append(summary);
          if (data.id && depth < 4) summary.addEventListener('click', async () => {
            if (node.dataset.loaded) return;
            node.dataset.loaded = 'loading'; summary.textContent = '正在读取转发消息…';
            try {
              const result = await api(`/forward/${encodeURIComponent(data.id)}`);
              const content = document.createElement('div'); content.className = 'forward-node-content';
              const nodes = Array.isArray(result?.message) ? result.message : Array.isArray(result?.messages) ? result.messages : [];
              for (const child of nodes) { const row = document.createElement('div'); row.className = 'forward-child'; const who = document.createElement('b'); who.textContent = child?.data?.name || child?.data?.nickname || child?.data?.uin || child?.data?.user_id || '成员'; const body = document.createElement('div'); appendMessageText(body, Array.isArray(child?.data?.content) ? child.data.content : [child], depth + 1); row.append(who, body); content.append(row); }
              if (!nodes.length) content.textContent = '没有可展开的节点';
              node.append(content); node.dataset.loaded = '1'; summary.textContent = `▤ 合并转发 (${nodes.length})`;
            } catch (error) { summary.textContent = `▤ 展开失败：${error.message}`; delete node.dataset.loaded; }
          });
          container.append(node);
        }
      } else {
        const other = document.createElement('span'); other.className = 'unknown-segment'; other.textContent = `[${escText(seg?.type || '消息段')}]`; container.append(other);
      }
    }
  }
  function safeBasename(value) {
    const s = escText(value).split(/[\\/]/).filter(Boolean).pop() || '附件';
    return s.length > 48 ? `${s.slice(0, 45)}…` : s;
  }

  function renderConversations() {
    el.conversationList.replaceChildren();
    for (const conv of state.data?.conversations || []) {
      const button = document.createElement('button'); button.className = 'conversation-item';
      if (state.active && state.active.type === conv.type && idOf(state.active.id) === idOf(conv.id)) button.classList.add('active');
      const avatar = document.createElement('span'); avatar.className = `avatar ${conv.type === 'group' ? 'avatar-purple' : 'avatar-cyan'}`; avatar.textContent = conv.type === 'group' ? '群' : avatarLabel(conv.title || conv.id);
      const copy = document.createElement('span'); copy.className = 'conversation-copy';
      const title = document.createElement('b'); title.textContent = conv.title || (conv.type === 'group' ? `群 ${conv.id}` : `私聊 ${conv.id}`);
      const desc = document.createElement('small'); desc.textContent = `${conv.type === 'group' ? '群聊' : '私聊'} · ${conv.id}`;
      copy.append(title, desc); button.append(avatar, copy); button.addEventListener('click', () => selectConversation(conv));
      el.conversationList.append(button);
    }
  }
  function renderMembers() {
    el.memberList.replaceChildren();
    const members = state.data?.members || [];
    for (const member of members) {
      const row = document.createElement('div'); row.className = 'member-row';
      const avatar = document.createElement('span'); avatar.className = 'mini-avatar'; avatar.textContent = avatarLabel(member.card || member.nickname);
      const name = document.createElement('span'); name.className = 'member-name'; name.textContent = member.card || member.nickname || member.id;
      const id = document.createElement('small'); id.textContent = member.id;
      const edit = document.createElement('button'); edit.className = 'member-edit'; edit.textContent = '⋯'; edit.title = '编辑成员';
      edit.addEventListener('click', () => editMember(member)); row.append(avatar, name, id, edit); el.memberList.append(row);
    }
    const bot = state.data?.bot || { id: '99000', nickname: 'Bot' };
    const row = document.createElement('div'); row.className = 'member-row bot-row';
    const avatar = document.createElement('span'); avatar.className = 'mini-avatar bot-mini'; avatar.textContent = 'Q';
    const name = document.createElement('span'); name.className = 'member-name'; name.textContent = bot.nickname || 'Bot';
    const id = document.createElement('small'); id.textContent = bot.id;
    row.append(avatar, name, id); el.memberList.append(row);
  }
  function renderSender() {
    const current = el.sender.value;
    el.sender.replaceChildren();
    const people = state.data?.members || [];
    for (const p of people) {
      const option = document.createElement('option'); option.value = idOf(p.id); option.textContent = `${p.card || p.nickname || p.id} · ${p.id}`; el.sender.append(option);
    }
    const conv = activeConversation();
    if (conv?.type === 'private') {
      const peer = people.find((p) => idOf(p.id) === idOf(conv.id));
      el.sender.value = idOf(peer?.id ?? people[0]?.id ?? '');
      el.sender.disabled = true;
    } else {
      if (people.some((p) => idOf(p.id) === current)) el.sender.value = current;
      el.sender.disabled = false;
    }
  }
  function renderMessages() {
    const wasNearBottom = el.messages.scrollHeight - el.messages.scrollTop - el.messages.clientHeight < 56;
    el.messages.replaceChildren();
    const conv = activeConversation();
    const items = displayEvents();
    if (!conv) {
      const empty = document.createElement('div'); empty.className = 'empty-state';
      const icon = document.createElement('span'); icon.className = 'empty-icon'; icon.textContent = '✦';
      const title = document.createElement('strong'); title.textContent = '开始一轮安全测试';
      const p = document.createElement('p'); p.textContent = '选择群聊或私聊，再以不同成员身份发送消息。'; empty.append(icon, title, p); el.messages.append(empty); return;
    }
    if (!items.length) {
      const empty = document.createElement('div'); empty.className = 'empty-state compact';
      const title = document.createElement('strong'); title.textContent = '这里还没有消息';
      const p = document.createElement('p'); p.textContent = '发送一条消息开始测试 Bot 的实际收发链路。'; empty.append(title, p); el.messages.append(empty); return;
    }
    let lastDay = '';
    for (const item of items) {
      const timestamp = item.event?.time ? new Date(Number(item.event.time) * 1000) : item.createdAt ? new Date(item.createdAt) : null;
      const day = timestamp && !Number.isNaN(timestamp.valueOf()) ? timestamp.toLocaleDateString() : '';
      if (day && day !== lastDay) { const sep = document.createElement('div'); sep.className = 'date-separator'; sep.textContent = day; el.messages.append(sep); lastDay = day; }
      const wrap = document.createElement('article'); wrap.className = `message ${item.sender.bot ? 'from-bot' : ''}`;
      const head = document.createElement('div'); head.className = 'message-head';
      const name = document.createElement('b'); name.textContent = item.sender.name;
      const feedbackDropped = item.record.feedback === 'dropped' || item.record.feedback_status === 'dropped' || item.record.feedback_dropped === true;
      const feedback = item.direction === 'outbound' ? feedbackFor(item.record) : null;
      const meta = document.createElement('span'); meta.textContent = `${item.sender.id}${item.direction ? ` · ${item.direction === 'outbound' ? '出站' : '入站'}` : ''}${feedbackDropped || feedback === 'dropped' ? ' · 反馈已丢弃' : feedback === 'delivered' ? ' · 反馈已确认' : ''}${timestamp && !Number.isNaN(timestamp.valueOf()) ? ` · ${timestamp.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}` : ''}`;
      head.append(name, meta);
      const bubble = document.createElement('div'); bubble.className = 'bubble'; appendMessageText(bubble, item.segments);
      const actions = document.createElement('div'); actions.className = 'message-actions';
      const reply = document.createElement('button'); reply.textContent = '引用'; reply.addEventListener('click', () => setReply(item));
      const raw = document.createElement('button'); raw.textContent = '{ }'; raw.title = '检查原始 JSON'; raw.addEventListener('click', () => showRaw(item.record));
      actions.append(reply, raw); wrap.append(head, bubble, actions); el.messages.append(wrap);
    }
    const conversationChanged = state.renderedConversation !== (activeConversation() ? convKey(activeConversation()) : '');
    if (conversationChanged || wasNearBottom) el.messages.scrollTop = el.messages.scrollHeight;
  }
  function renderHeader() {
    const conv = activeConversation();
    el.chatName.textContent = conv ? (conv.title || (conv.type === 'group' ? `群 ${conv.id}` : `私聊 ${conv.id}`)) : '请选择会话';
    el.chatSubtitle.textContent = conv ? `${conv.type === 'group' ? '群聊' : '私聊'} · ${conv.id} · 消息进入实际 AstrBot 流程` : '所有消息只发往本地模拟适配器';
    el.chatAvatar.textContent = conv?.type === 'group' ? '群' : avatarLabel(conv?.title || conv?.id);
    el.chatAvatar.className = `avatar ${conv?.type === 'group' ? 'avatar-purple' : 'avatar-cyan'}`;
    el.target.textContent = conv ? `${conv.title || conv.id} (${conv.id})` : '—';
  }
  function renderConnection() {
    const connected = !!state.data?.connected;
    el.connectionDot.classList.toggle('offline', !connected);
    const dsh = state.data?.mode === 'live' ? 'DSH 实时处理' : state.data?.mode === 'scripted' ? '脚本响应（非真实模型）' : state.data?.mode === 'disabled' ? 'DSH 已禁用' : 'DSH 状态未知';
    el.connectionLabel.textContent = `${connected ? '模拟适配器已连接' : '模拟适配器已断开'} · ${dsh}`;
  }
  function scenarioDrawerOpen() { return !$('#scenario-drawer').hidden; }
  function renderScenario() {
    const scenario = state.scenario || {};
    const owner = scenario.owner_id ?? scenario.owner?.id ?? null;
    $('#scenario-owner').textContent = owner == null ? '未启用 Owner 正例' : `${scenario.owner?.name || '模拟 Owner'} · ${owner}`;
    const mode = state.data?.mode || scenario.mode || 'unknown';
    const badge = $('#scenario-mode');
    badge.textContent = mode === 'live' ? '真实 DSH' : mode === 'scripted' ? '脚本响应' : mode === 'disabled' ? 'DSH 已禁用' : '模式未知';
    badge.className = `mode-badge ${mode}`;
    renderAdmission(); renderFaultRules(); renderEvidenceConversationOptions();
  }
  function renderAdmission() {
    const host = $('#admission-list');
    const conversations = state.data?.conversations || [];
    if (!conversations.length) { host.textContent = '当前没有模拟会话'; return; }
    const admitted = new Set((state.scenario?.admitted || []).map((x) => typeof x === 'string' ? x : x.key || convKey(x)));
    const fragment = document.createDocumentFragment();
    for (const conv of conversations) {
      const key = convKey(conv);
      const label = document.createElement('label'); label.className = 'admission-item';
      const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.value = key; checkbox.checked = admitted.has(key); checkbox.dataset.conversationKey = key;
      const text = document.createElement('span'); text.textContent = `${conv.type === 'group' ? '群' : '私聊'} · ${conv.title || conv.id}`;
      const id = document.createElement('small'); id.textContent = key; label.append(checkbox, text, id); fragment.append(label);
    }
    host.replaceChildren(fragment);
  }
  function renderFaultRules() {
    const host = $('#fault-rule-list'); host.replaceChildren();
    const rules = Array.isArray(state.faults) ? state.faults : state.faults?.faults || state.faults?.rules || [];
    if (!rules.length) { host.textContent = '暂无故障规则'; return; }
    for (const rule of rules) {
      const row = document.createElement('div'); row.className = 'rule-row';
      row.dataset.faultId = idOf(rule.id);
      const label = document.createElement('span'); label.textContent = `${rule.target || '目标'} / ${rule.kind || '故障'} · 第 ${rule.nth || 1} 次${rule.hit ? ' · 已命中' : ''}`;
      const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = '移除'; remove.addEventListener('click', async () => {
        try { await api(`/faults/${encodeURIComponent(rule.id)}`, { method: 'DELETE', headers: {'content-type':'application/json'}, body: '{}' }); await loadControlData(); }
        catch (error) { showToast(error.message, true); }
      }); row.append(label, remove); host.append(row);
    }
  }
  function renderEvidenceConversationOptions() {
    const select = $('#evidence-conversation');
    const selected = select.value;
    select.replaceChildren();
    for (const conv of state.data?.conversations || []) {
      const option = document.createElement('option'); option.value = convKey(conv); option.textContent = `${conv.type === 'group' ? '群' : '私聊'} ${conv.title || conv.id}`; select.append(option);
    }
    if ([...select.options].some((option) => option.value === selected)) select.value = selected;
    else if (state.active) select.value = convKey(state.active);
  }
  function prettyEvidence(value) {
    const pre = document.createElement('pre'); pre.textContent = JSON.stringify(value, null, 2); return pre;
  }
  function renderEvidence() {
    const host = $('#evidence-content'); host.replaceChildren();
    if (!state.evidence) { host.textContent = '选择会话并刷新以读取 Journal、Router、审计和回执。'; return; }
    const evidence = state.evidence;
    const sections = [
      ['Canonical Journal', evidence.journal ?? evidence.canonical_journal],
      ['Router / Session / Cursor / Pending', evidence.states ?? evidence.router ?? evidence.routing ?? evidence.sessions],
      ['Turn 与工具事件', evidence.events ?? evidence.lifecycle],
      ['审计', evidence.audit],
      ['发送回执与传输差异', evidence.receipts ?? evidence.actions ?? evidence.differences],
      ['完整原始证据', evidence],
    ].filter(([, value]) => value !== undefined);
    for (const [title, value] of sections) {
      const details = document.createElement('details'); details.className = 'evidence-block';
      const summary = document.createElement('summary'); summary.textContent = title;
      details.append(summary, prettyEvidence(value)); host.append(details);
    }
    if (!sections.length) host.append(prettyEvidence(evidence));
    const states = evidence.states || [];
    const pending = Array.isArray(evidence.pending) ? evidence.pending
      : (Array.isArray(states) ? states : []).filter((entry) => entry?.pending_upper_cursor != null);
    if (pending.length) {
      const select = document.createElement('select'); select.id = 'pending-select';
      for (const entry of pending) {
        const option = document.createElement('option');
        const id = typeof entry === 'string' ? entry : entry.pending_upper_cursor ?? entry.id ?? entry.pending_id;
        option.value = String(id); option.dataset.conversationKey = entry.conversation_key || '';
        option.dataset.domain = entry.domain || '';
        option.textContent = `${entry.domain || '状态'} · ${entry.conversation_key || ''} · Journal ${id}`; select.append(option);
      }
      const row = document.createElement('label'); row.className = 'pending-pick'; row.append(document.createTextNode('待恢复项 '), select); host.prepend(row);
    }
  }
  async function loadControlData() {
    try {
      const [scenario, faults] = await Promise.all([api('/scenario'), api('/faults')]);
      state.scenario = scenario; state.scenarioStamp = JSON.stringify(scenario); state.faults = faults; renderScenario();
    } catch (error) { showToast(`测试控制信息读取失败：${error.message}`, true); }
  }
  async function loadEvidence() {
    const params = new URLSearchParams();
    if ($('#evidence-conversation').value) params.set('conversation_key', $('#evidence-conversation').value);
    if ($('#evidence-session').value.trim()) params.set('session_id', $('#evidence-session').value.trim());
    try { state.evidence = await api(`/evidence?${params}`); renderEvidence(); }
    catch (error) { showToast(`证据读取失败：${error.message}`, true); }
  }
  function render() {
    if (!state.data) return;
    if (!activeConversation() && state.data.conversations?.length) state.active = { type: state.data.conversations[0].type, id: state.data.conversations[0].id };
    const conversationKey = activeConversation() ? convKey(activeConversation()) : '';
    renderConnection();
    if (state.data.scenario) {
      const stamp = JSON.stringify(state.data.scenario);
      if (stamp !== state.scenarioStamp) { state.scenario = state.data.scenario; state.scenarioStamp = stamp; if (scenarioDrawerOpen()) renderScenario(); }
    }
    const rosterStamp = JSON.stringify({ members: state.data.members, bot: state.data.bot });
    const chatsStamp = JSON.stringify(state.data.conversations);
    const changedChat = state.renderedConversation !== conversationKey;
    if (changedChat || state.renderedChats !== chatsStamp) renderConversations();
    if (state.renderedRoster !== rosterStamp) renderMembers();
    if (changedChat || state.renderedRoster !== rosterStamp) renderSender();
    state.renderedChats = chatsStamp; state.renderedRoster = rosterStamp;
    renderHeader();
    renderEvidenceConversationOptions();
    const feedbackStamp = (state.data.actions || []).filter((action) => Boolean(action.feedback))
      .map((action) => [action.response?.data?.message_id, action.feedback]);
    const eventsStamp = JSON.stringify({ events: state.data.events || [], feedback: feedbackStamp });
    if (state.renderedEvents !== eventsStamp || state.renderedConversation !== conversationKey) {
      renderMessages(); state.renderedEvents = eventsStamp; state.renderedConversation = conversationKey;
    }
  }
  async function refresh() {
    if (state.polling) return;
    state.polling = true;
    try { state.data = await api('/state'); render(); }
    catch (error) { el.connectionDot.classList.add('offline'); el.connectionLabel.textContent = '沙箱服务不可用'; }
    finally { state.polling = false; }
  }
  function showRaw(value) {
    state.selectedRaw = value;
    const messageId = value?.message_id ?? value?.messageId ?? value?.id;
    const canReplay = messageId != null && value?.direction !== 'outbound';
    for (const button of [$('#replay-event'), $('#replay-new-message')]) {
      button.disabled = !canReplay;
      button.dataset.messageId = canReplay ? String(messageId) : '';
    }
    const pre = document.createElement('pre'); pre.textContent = JSON.stringify(value, null, 2);
    el.rawContent.replaceChildren(pre);
    const actions = state.data?.actions || [];
    if (actions.length) {
      const section = document.createElement('section'); section.className = 'action-log';
      const title = document.createElement('strong'); title.textContent = '最近的适配器调用'; section.append(title);
      for (const action of actions.slice(-8).reverse()) {
        const row = document.createElement('div'); row.className = 'action-row';
        const name = document.createElement('b'); name.textContent = action.action || 'OneBot API';
        const status = document.createElement('span'); status.textContent = action.status || 'unknown'; status.className = `action-status ${String(action.status || '').toLowerCase()}`;
        const detail = document.createElement('small'); detail.textContent = action.error || action.feedback || action.response?.message || action.response?.status || '';
        row.append(name, status); if (detail.textContent) row.append(detail); section.append(row);
      }
      el.rawContent.append(section);
    }
    el.inspector.hidden = false;
    $('#raw-toggle').classList.add('selected');
  }
  function setReply(item) {
    state.reply = item;
    el.replyLabel.textContent = `${item.sender.name}：${segmentPreview(item.segments)}`;
    el.replyBanner.hidden = false; el.input.focus();
  }
  function selectConversation(conv) {
    const next = { type: conv.type, id: conv.id };
    if (state.active && convKey(state.active) === convKey(next)) return;
    state.active = next;
    state.reply = null;
    el.replyBanner.hidden = true;
    el.input.value = '';
    state.attachments = [];
    el.fixtures.hidden = true;
    renderAttachments();
    render();
  }
  function segmentPreview(segments) {
    return segments.map((s) => s.type === 'text' ? escText(s.data?.text) : `[${s.type}]`).join('').slice(0, 100) || '消息';
  }
  function getFaults() {
    const faults = {};
    document.querySelectorAll('input[name="fault"]:checked').forEach((input) => { faults[input.value] = true; });
    return faults;
  }

  function addTextSegment(message, text) {
    if (!text) return;
    const last = message[message.length - 1];
    if (last?.type === 'text') last.data.text += text;
    else message.push({ type: 'text', data: { text } });
  }
  function textWithMentions(text) {
    const segments = [];
    const pattern = /@\{(\d+)\}/g;
    let cursor = 0, match;
    while ((match = pattern.exec(text))) {
      addTextSegment(segments, text.slice(cursor, match.index));
      segments.push({ type: 'at', data: { qq: match[1] } }); cursor = pattern.lastIndex;
    }
    addTextSegment(segments, text.slice(cursor));
    return segments;
  }
  async function sendMessage({ extra = [], text = el.input.value, forward = false } = {}) {
    const conv = activeConversation();
    if (!conv) return showToast('请先选择会话', true);
    if (conv.type === 'private' && idOf(el.sender.value) !== idOf(conv.id)) return showToast('私聊发送身份必须与会话成员一致', true);
    const message = forward ? extra : [...textWithMentions(text), ...state.attachments, ...extra];
    if (!message.length) return showToast('先输入消息或添加附件', true);
    const replyId = state.reply?.messageId;
    const body = { conversation: { type: conv.type, id: idOf(conv.id) }, sender_id: el.sender.value, message, faults: getFaults() };
    if (replyId != null) body.reply_to = String(replyId);
    try {
      const result = await api('/send', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
      el.input.value = ''; state.attachments = []; renderAttachments(); state.reply = null; el.replyBanner.hidden = true;
      document.querySelectorAll('input[name="fault"]').forEach((input) => { input.checked = false; });
      if (result?.feedback === 'dropped' || result?.feedback_status === 'dropped' || result?.drop_feedback || result?.feedback_dropped || result?.feedback?.status === 'dropped') showToast('消息已处理，发送反馈已丢弃；请按原始事件核对状态');
      else if (result?.feedback === 'delivered' || result?.feedback_status === 'delivered') showToast('发送反馈已确认');
      else if (result?.warning) showToast(result.warning); else showToast('消息已提交到模拟适配器，等待实际处理结果');
      await refresh();
    } catch (error) { showToast(error.message, true); }
  }
  function renderAttachments() {
    el.attachments.replaceChildren(); el.attachments.hidden = state.attachments.length === 0;
    for (const segment of state.attachments) {
      const chip = document.createElement('span'); chip.className = 'attachment-chip';
      chip.textContent = `${segment.type === 'image' ? '▧' : '▤'} ${safeBasename(segment.data?.name || segment.data?.file || segment.data?.url || '附件')}`;
      el.attachments.append(chip);
    }
  }
  function renderFixtures() {
    el.fixtures.replaceChildren();
    const assets = state.data?.assets || [];
    for (const asset of assets) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'fixture-item';
      const image = String(asset.content_type || '').startsWith('image/');
      const icon = document.createElement('span'); icon.textContent = image ? '▧' : '▤';
      const name = document.createElement('span'); name.textContent = asset.name || asset.id;
      const size = document.createElement('small'); size.textContent = formatSize(asset.size);
      button.append(icon, name, size);
      button.addEventListener('click', () => {
        const segmentType = image ? 'image' : 'file';
        const id = idOf(asset.id);
        state.attachments.push({ type: segmentType, data: { file: `sandbox://${id}`, file_id: id, name: asset.name || id } });
        renderAttachments(); el.fixtures.hidden = true;
      });
      el.fixtures.append(button);
    }
  }
  async function uploadFiles(files) {
    for (const file of files) {
      const form = new FormData(); form.append('file', file, file.name);
      try { const segment = await api('/upload', { method: 'POST', body: form }); state.attachments.push(segment.segment || segment); }
      catch (error) { showToast(`${file.name}: ${error.message}`, true); }
    }
    el.file.value = ''; renderAttachments();
  }
  function messageSegmentsForForward(item) {
    return item.segments.map((segment) => {
      const copy = { type: segment.type, data: { ...(segment.data || {}) } };
      if (['image', 'file', 'record', 'video'].includes(copy.type) && copy.data.file_id) copy.data.file = `sandbox://${copy.data.file_id}`;
      return copy;
    });
  }
  async function createForward() {
    const items = displayEvents();
    const selected = items.slice(-5);
    if (!selected.length) return showToast('当前会话没有可转发的消息', true);
    const nodes = selected.map((item) => ({ type: 'node', data: { name: item.sender.name, uin: item.sender.id || '0', content: messageSegmentsForForward(item) } }));
    await sendMessage({ extra: nodes, forward: true });
  }

  async function addMember() {
    const nickname = prompt('成员昵称', '新成员');
    if (nickname == null || !nickname.trim()) return;
    const id = prompt('QQ 号（留空自动分配）', '');
    const body = { nickname: nickname.trim() }; if (id?.trim()) body.id = id.trim();
    try { await api('/members', { method: 'POST', headers: {'content-type':'application/json'}, body: JSON.stringify(body) }); await refresh(); showToast('成员已添加'); }
    catch (error) { showToast(error.message, true); }
  }
  async function editMember(member) {
    const nickname = prompt('成员昵称', member.nickname || ''); if (nickname == null) return;
    const card = prompt('群名片（可留空）', member.card || ''); if (card == null) return;
    try { await api(`/members/${encodeURIComponent(member.id)}`, { method: 'PATCH', headers: {'content-type':'application/json'}, body: JSON.stringify({ nickname: nickname.trim(), card: card.trim() }) }); await refresh(); }
    catch (error) { showToast(error.message, true); }
  }
  async function createConversation() {
    const type = confirm('创建群聊？\n选择“确定”创建群聊，选择“取消”创建私聊。') ? 'group' : 'private';
    let id;
    if (type === 'private') {
      const members = state.data?.members || [];
      const choices = members.map((m, i) => `${i + 1}. ${m.card || m.nickname} (${m.id})`).join('\n');
      const choice = prompt(`选择私聊成员：\n${choices}`, '1'); if (choice == null) return;
      const peer = members[Number(choice) - 1]; if (!peer) return showToast('成员序号无效', true);
      id = idOf(peer.id);
    }
    const title = prompt(type === 'group' ? '群名称' : '私聊标题', type === 'group' ? '测试群' : (state.data?.members?.find((m) => idOf(m.id) === id)?.card || state.data?.members?.find((m) => idOf(m.id) === id)?.nickname)); if (title == null) return;
    try {
      const body = { type, title: title.trim() }; if (id) body.id = id;
      const conv = await api('/conversations', { method: 'POST', headers: {'content-type':'application/json'}, body: JSON.stringify(body) });
      await refresh(); if (conv?.id) state.active = { type, id: conv.id }; render();
    } catch (error) { showToast(error.message, true); }
  }

  el.composer.addEventListener('submit', (event) => { event.preventDefault(); sendMessage(); });
  el.input.addEventListener('keydown', (event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); el.composer.requestSubmit(); } });
  el.messages.addEventListener('click', (event) => {
    const message = event.target.closest('.message');
    if (message) return;
  });
  document.querySelector('[data-insert="face"]').addEventListener('click', () => {
    const face = prompt('QQ 表情 ID（OneBot face 段）', '14'); if (face == null) return;
    state.attachments.push({ type: 'face', data: { id: face.trim() } }); renderAttachments();
  });
  $('#at-button').addEventListener('click', () => {
    if (activeConversation()?.type !== 'group') return showToast('@ 仅用于群聊消息', true);
    const bot = state.data?.bot || { id: '99000', nickname: 'Bot' };
    const members = [{ id: bot.id, card: bot.nickname || 'Bot' }, ...(state.data?.members || [])];
    if (!members.length) return showToast('请先创建模拟成员', true);
    const list = members.map((m, i) => `${i + 1}. ${m.card || m.nickname} (${m.id})`).join('\n');
    const choice = prompt(`选择要 @ 的成员，输入序号：\n${list}`, '1');
    if (choice == null) return;
    const member = members[Number(choice) - 1];
    if (!member) return showToast('成员序号无效', true);
    const marker = `@{${member.id}} `;
    const start = el.input.selectionStart ?? el.input.value.length;
    const end = el.input.selectionEnd ?? start;
    el.input.setRangeText(marker, start, end, 'end'); el.input.focus();
  });
  el.file.addEventListener('change', () => uploadFiles([...el.file.files]));
  $('#fixture-button').addEventListener('click', () => {
    const assets = state.data?.assets || [];
    if (!assets.length) return showToast('沙箱还没有测试素材', true);
    renderFixtures(); el.fixtures.hidden = !el.fixtures.hidden;
  });
  $('#forward-button').addEventListener('click', createForward);
  $('#reply-button').addEventListener('click', () => {
    const items = displayEvents();
    if (!items.length) return showToast('当前会话没有可引用的消息', true);
    const item = items[items.length - 1]; setReply(item);
  });
  $('#clear-reply').addEventListener('click', () => { state.reply = null; el.replyBanner.hidden = true; });
  $('#add-member').addEventListener('click', addMember);
  $('#new-conversation').addEventListener('click', createConversation);
  $('#reconnect').addEventListener('click', async () => {
    try { await api('/connect', { method: 'POST', headers: {'content-type':'application/json'}, body: JSON.stringify({ connected: true }) }); await refresh(); }
    catch (error) { showToast(error.message, true); }
  });
  $('#fault-toggle').addEventListener('click', () => { $('#fault-panel').hidden = !$('#fault-panel').hidden; });
  $('#raw-toggle').addEventListener('click', () => { el.inspector.hidden = !el.inspector.hidden; });
  $('#close-inspector').addEventListener('click', () => { el.inspector.hidden = true; });
  async function replayMessage(buttonId, mode) {
    const messageId = $(buttonId).dataset.messageId;
    if (!messageId) return;
    try {
      await api('/replay', { method: 'POST', headers: {'content-type':'application/json'}, body: JSON.stringify({ message_id: messageId, mode }) });
      showToast(mode === 'new' ? '已作为新消息重新发送' : '已按原 message_id 重放原始事件');
      await refresh();
    } catch (error) { showToast(error.message, true); }
  }
  $('#replay-event').addEventListener('click', () => replayMessage('#replay-event', 'duplicate'));
  $('#replay-new-message').addEventListener('click', () => replayMessage('#replay-new-message', 'new'));

  $('#scenario-toggle').addEventListener('click', async () => {
    const drawer = $('#scenario-drawer'); drawer.hidden = !drawer.hidden;
    if (!drawer.hidden) { await loadControlData(); renderEvidenceConversationOptions(); }
  });
  $('#close-scenario').addEventListener('click', () => { $('#scenario-drawer').hidden = true; });
  $('#save-admission').addEventListener('click', async () => {
    const button = $('#save-admission'); button.disabled = true;
    const admitted = [...document.querySelectorAll('#admission-list input:checked')].map((input) => input.value);
    try {
      const updated = await api('/scenario', { method: 'PUT', headers: {'content-type':'application/json'}, body: JSON.stringify({ revision: state.scenario?.revision, admitted }) });
      state.scenario = updated; renderScenario(); $('#admission-status').textContent = '名单已保存'; showToast('DSH 准入名单已更新');
    } catch (error) { $('#admission-status').textContent = error.message; showToast(`准入名单未保存：${error.message}`, true); await loadControlData(); }
    finally { button.disabled = false; }
  });
  $('#precise-fault-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const conv = activeConversation();
    if (!conv) return showToast('先选择要限定的会话', true);
    const body = {
      target: $('#fault-target').value, kind: $('#fault-kind').value, conversation_key: convKey(conv),
      nth: Math.max(1, Number($('#fault-nth').value) || 1), phase: $('#fault-phase').value,
      delay_ms: Math.max(0, Number($('#fault-delay').value) || 0),
      limit_chars: Math.max(1, Number($('#fault-limit').value) || 1),
    };
    const callId = $('#fault-call-id').value.trim(); const sessionId = $('#fault-session-id').value.trim(); const turnId = $('#fault-turn-id').value.trim();
    if (callId) body.call_id = callId; if (sessionId) body.session_id = sessionId; if (turnId) body.native_turn_id = turnId;
    try { await api('/faults', { method: 'POST', headers: {'content-type':'application/json'}, body: JSON.stringify(body) }); await loadControlData(); showToast('一次性故障规则已添加'); }
    catch (error) { showToast(`故障规则未添加：${error.message}`, true); }
  });
  $('#event-wait-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const conv = activeConversation();
    const output = $('#wait-result'); output.textContent = '等待真实生命周期事件…';
    const selectedEvent = $('#wait-event').value;
    const [eventName, stopReason] = selectedEvent.split(':', 2);
    const body = { event: eventName, timeout_seconds: 30 };
    if (stopReason) body.stop_reason = stopReason;
    if (conv) body.conversation_key = convKey(conv);
    if ($('#wait-tool').value.trim()) body.tool = $('#wait-tool').value.trim();
    if ($('#evidence-session').value.trim()) body.session_id = $('#evidence-session').value.trim();
    const lifecycle = state.evidence?.lifecycle;
    if (Array.isArray(lifecycle) && lifecycle.length) {
      const last = lifecycle[lifecycle.length - 1];
      body.after = last.audit_event_id || last.runtime_event_id || undefined;
    }
    try {
      const result = await api('/wait', { method: 'POST', headers: {'content-type':'application/json'}, body: JSON.stringify(body) });
      output.replaceChildren(prettyEvidence(result));
      showToast(result?.matched === false ? '等待超时，未观察到事件' : '已观察到生命周期事件');
    } catch (error) { output.textContent = error.message; showToast(`事件等待失败：${error.message}`, true); }
  });
  async function sendOwnerControl(command) {
    const owner = state.scenario?.owner_id ?? state.scenario?.owner?.id;
    if (owner == null) return showToast('该场景没有启动时配置的模拟 Owner', true);
    const conversation = { type: 'private', id: String(owner) };
    try {
      if (!(state.data?.conversations || []).some((item) => item.type === 'private' && idOf(item.id) === String(owner))) {
        await api('/conversations', { method: 'POST', headers: {'content-type':'application/json'}, body: JSON.stringify({ type: 'private', id: String(owner), title: state.scenario?.owner?.name || '模拟 Owner' }) });
        await refresh();
      }
      await api('/send', { method: 'POST', headers: {'content-type':'application/json'}, body: JSON.stringify({
        conversation, sender_id: String(owner), message: [{ type: 'text', data: { text: command } }],
      }) });
      showToast('Owner 命令已作为私聊消息提交'); await refresh();
    } catch (error) { showToast(`Owner 命令未提交：${error.message}`, true); }
  }
  $('#cancel-turn').addEventListener('click', () => sendOwnerControl('/v2agent cancel'));
  $('#recover-pending').addEventListener('click', () => {
    const select = $('#pending-select'); const option = select?.selectedOptions?.[0];
    if (!option) return showToast('证据中没有可恢复的 pending 项', true);
    const owner = state.scenario?.owner_id ?? state.scenario?.owner?.id;
    const ownerPrivate = owner == null ? '' : `private:${owner}`;
    if (option.dataset.domain !== 'owner' || option.dataset.conversationKey !== ownerPrivate) {
      return showToast('恢复命令只支持当前启动 Owner 的私聊 pending；请先核对对应会话证据', true);
    }
    sendOwnerControl(`/v2agent recover ${select.value}`);
  });
  $('#refresh-evidence').addEventListener('click', loadEvidence);
  $('#export-evidence').addEventListener('click', async () => {
    const params = new URLSearchParams();
    if ($('#evidence-conversation').value) params.set('conversation_key', $('#evidence-conversation').value);
    if ($('#evidence-session').value.trim()) params.set('session_id', $('#evidence-session').value.trim());
    try {
      const response = await fetch(`${API}/evidence/export?${params}`);
      if (!response.ok) { const error = await response.json().catch(() => ({})); throw new Error(error.error || `请求失败 (${response.status})`); }
      const blob = await response.blob(); const href = URL.createObjectURL(blob);
      const link = document.createElement('a'); link.href = href; link.download = 'sandbox-evidence.json'; link.click();
      setTimeout(() => URL.revokeObjectURL(href), 30000);
      showToast('证据 JSON 已导出');
    } catch (error) { showToast(`证据导出失败：${error.message}`, true); }
  });
  $('#evidence-conversation').addEventListener('change', loadEvidence);

  refresh(); setInterval(refresh, 1400);
})();
