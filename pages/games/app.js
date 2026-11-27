import {getSharedScope, setSharedScope, formatScopeLabel, msg, tr, setText} from './series-ui.js';

const $ = sel => document.querySelector(sel);
const el = (tag, cls = '', text = undefined) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) setText(n, text);
  return n;
};

let activeTab = 'home';
const groupScope = scope => /^[^:\s]+:GroupMessage:[^\s]+$/.test(scope || '') ? scope : '';
let currentUmo = groupScope(getSharedScope());
let overviewData = null;
let activitiesList = [];
let selectedActivityId = '';
let outcomesList = [];
let selectedOutcomeId = '';
let activityPage = 1;
let outcomePage = 1;
let pendingModalAction = null;
let scopeEpoch = 0;
const requests = {};

function nextRequest(channel) {
  const epoch = scopeEpoch;
  const request = requests[channel] = (requests[channel] || 0) + 1;
  return () => scopeEpoch === epoch && requests[channel] === request;
}

function setLoading(node, loading) {
  node.setAttribute('aria-busy', String(loading));
  if (loading) node.replaceChildren(el('p', 'empty', msg('正在读取…')));
}

function showReadError(node, err) {
  const message = err?.message || '读取失败，请重试。';
  node.replaceChildren(el('p', 'empty', message));
  setNotice(message, true);
}

function listButton(isSelected) {
  const button = el('button', `list-item${isSelected ? ' active' : ''}`);
  button.type = 'button';
  if (isSelected !== undefined) button.setAttribute('aria-pressed', String(isSelected));
  return button;
}

function selectRow(list, row) {
  for (const item of list.querySelectorAll('button.list-item')) {
    item.classList.toggle('active', item === row);
    item.setAttribute('aria-pressed', String(item === row));
  }
}

function renderPagination(selector, page, hasNext, load) {
  const nav = $(selector);
  if (!nav) return;
  nav.replaceChildren();
  nav.hidden = page <= 1 && !hasNext;
  if (nav.hidden) return;
  const previous = el('button', '', msg('上一页'));
  const next = el('button', '', msg('下一页'));
  previous.type = next.type = 'button';
  previous.disabled = page <= 1;
  next.disabled = !hasNext;
  previous.onclick = () => load(page - 1);
  next.onclick = () => load(page + 1);
  nav.append(previous, el('span', 'muted', msg('第 {0} 页', page)), next);
}

window.DynamicsGetActiveScope = () => currentUmo;

function setNotice(msg, isError = false) {
  const node = $('#notice');
  if (!node) return;
  node.setAttribute('role', isError ? 'alert' : 'status');
  setText(node, msg);
  node.style.borderLeftColor = isError ? 'var(--danger)' : 'var(--cyan)';
  node.style.color = isError ? 'var(--danger)' : 'var(--muted)';
}

function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(Number(ts) * 1000);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString(document.documentElement.lang, {hour12: false});
}

async function apiGet(path, params = {}) {
  const bridge = (window.DynamicsPanelBridge || window.AstrBotPluginPage || window.AstrBotPluginView);
  if (!bridge) throw new Error('请从 AstrBot 管理面板打开群间游戏控制台。');
  if (bridge.ready) await bridge.ready();
  const clean = {};
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') clean[k] = String(v);
  }
  const raw = await bridge.apiGet(path, clean);
  if (raw?.status === 'error' || raw?.ok === false) {
    throw new Error(raw.message || raw.error || '请求失败');
  }
  return raw?.data ?? raw;
}

async function apiPost(path, body = {}, valid = () => true) {
  const bridge = (window.DynamicsPanelBridge || window.AstrBotPluginPage || window.AstrBotPluginView);
  if (!bridge) throw new Error('请从 AstrBot 管理面板打开群间游戏控制台。');
  if (bridge.ready) await bridge.ready();
  if (!valid()) return;
  const raw = await bridge.apiPost(path, body);
  if (raw?.status === 'error' || raw?.ok === false) {
    throw new Error(raw.message || raw.error || '操作失败');
  }
  return raw?.data ?? raw;
}

function syncUmoDropdown(knownUmos = []) {
  const sel = $('#global-umo');
  const dl = $('#known-umo-list');
  const merged = new Set(knownUmos);
  if (currentUmo) merged.add(currentUmo);
  const sorted = [...merged].filter(Boolean).sort();

  sel.replaceChildren(el('option', '', msg('全部群聊会话')));
  sel.firstChild.value = '';
  dl.replaceChildren();

  for (const u of sorted) {
    const opt = el('option', '', `${formatScopeLabel(u)} (${u})`);
    opt.value = u;
    sel.append(opt);
    const dlOpt = document.createElement('option');
    dlOpt.value = u;
    dl.append(dlOpt);
  }
  sel.value = currentUmo;
}

function renderStats(stats = {}) {
  const openCount = Number(stats.open_activities ?? 0);
  const items = [
    ['进行中协作', openCount],
    ['已完结成果', stats.completed_activities ?? 0],
    ['群间记忆已存档', stats.memory_recorded ?? 0],
    ['群史记已收录', stats.chronicle_admitted ?? 0],
  ];
  const bar = $('#stats-bar');
  bar.replaceChildren(...items.map(([label, val]) => {
    const box = el('div', 'stat');
    box.append(el('strong', '', val), el('span', '', msg(label)));
    return box;
  }));
  const tabBadge = $('#tab-open-badge');
  if (tabBadge) {
    tabBadge.hidden = openCount <= 0;
    tabBadge.textContent = openCount > 0 ? String(openCount) : '';
  }
}

function buildStageStepper(act) {
  const wrap = el('div', 'card-stepper');
  const status = String(act.status || 'recruiting');
  const phases = [
    {key: 'recruiting', label: '报名'},
    {key: 'active', label: status === 'active' && act.current_stage ? `进行中·${act.current_stage}` : '进行中'},
    {key: 'completed', label: status === 'cancelled' ? '已取消' : '结项'},
  ];
  const rank = status === 'completed' ? 2 : status === 'active' ? 1 : status === 'cancelled' ? -1 : 0;
  wrap.setAttribute('aria-label', `阶段进度：${act.status_label || status} · ${act.current_stage || ''}`);
  phases.forEach((ph, idx) => {
    if (idx > 0) wrap.append(el('span', 'card-step-arrow', '→'));
    let cls = 'card-step';
    if (status === 'cancelled' && idx === 2) {
      cls += ' is-cancelled';
    } else if (status === 'completed') {
      cls += ' is-done';
    } else if (rank >= 0 && idx < rank) {
      cls += ' is-done';
    } else if (idx === rank) {
      cls += ' is-current';
    }
    const pill = el('span', cls);
    pill.append(el('span', 'card-step-dot'), el('span', '', ph.label));
    wrap.append(pill);
  });
  return wrap;
}

function buildMiniProgress(act) {
  const count = Number(act.contribution_count ?? 0);
  const target = Math.max(1, Number(act.target_count || 1));
  const pct = Math.min(100, Math.round((count / target) * 100));
  const row = el('div', 'mini-progress-row');
  const track = el('div', 'mini-progress-track');
  const fill = el('div', 'mini-progress-fill');
  fill.style.width = `${pct}%`;
  track.append(fill);
  row.append(track, el('span', '', `${count}/${target} (${pct}%)`));
  return row;
}

function renderHome() {
  if (!overviewData) return;
  const own = overviewData.ownership || {};
  const ownStack = $('#ownership-list');
  ownStack.replaceChildren();
  for (const [title, desc, tag] of [
    ['群间游戏 · Dynamics Games', own.dynamics_games || '负责报名、阶段、进度与成果', '当前主控'],
    ['群间记忆 · Dynamics Memory', own.dynamics_memory || '负责保存共同参与的活动经历', '经历归档'],
    ['群史记 · Group Chronicle', own.group_chronicle || '负责收录有价值的完结结果', '成果入史'],
  ]) {
    const row = el('div', 'ownership-row');
    const left = el('div');
    left.append(el('strong', '', msg(title)), el('span', '', msg(desc)));
    row.append(left, el('span', 'badge neutral', msg(tag)));
    ownStack.append(row);
  }

  const comp = overviewData.companions || {};
  const mem = comp.memory || {};
  const chr = comp.chronicle || {};
  const totalEngines = (overviewData.engines || []).length;
  const legacyConflicts = (overviewData.legacy_plugins || []).filter(p => p.activated);
  const legacyBadge = $('#legacy-status-badge');
  const contractDetails = $('#contract-details');
  if (legacyConflicts.length) {
    legacyBadge.className = 'badge danger';
    setText(legacyBadge, msg('{0} 个旧插件仍开启',legacyConflicts.length));
    if (contractDetails) contractDetails.open = true;
  } else {
    legacyBadge.className = 'badge';
    setText(legacyBadge, msg('{0} 项引擎已无冲突接管',totalEngines));
  }

  const healthBox = $('#bridge-health');
  healthBox.replaceChildren();
  const addBridgeRow = (name, desc, ok, stateLabel) => {
    const row = el('div', 'ownership-row');
    const left = el('div');
    left.append(el('strong', '', msg(name)), el('span', '', msg(desc)));
    row.append(left, el('span', ok ? 'badge' : 'badge warn', msg(stateLabel)));
    healthBox.append(row);
  };
  addBridgeRow(
    '群间记忆桥接',
    mem.ready ? '完结活动经历将自动写入混合记忆检索索引' : (mem.detail || mem.reason || '未就绪，本地台账仍完整保留，后续可一键补同步'),
    Boolean(mem.ready),
    mem.ready ? '已就绪' : '待连接'
  );
  addBridgeRow(
    '群史记入史桥接',
    chr.ready ? '多人完结有效成果将自动核验入史，流局/取消绝不入史' : '未检测到就绪群史记，成果留存本地台账待同步',
    Boolean(chr.ready),
    chr.ready ? '已就绪' : '待连接'
  );
  if (legacyConflicts.length) {
    addBridgeRow(
      '旧版独立娱乐插件冲突警告',
      msg('请停用旧插件避免双倍回复：{0}',legacyConflicts.map(p => p.title).join('、')),
      false,
      '需停用旧插件'
    );
  } else {
    const enabledCount = (overviewData.engines || []).filter(e => e.enabled).length;
    addBridgeRow(
      '内置群聊娱乐矩阵',
      msg('已加载 {0} 大娱乐引擎，当前启用 {1} 项',totalEngines,enabledCount),
      true,
      msg('{0}/{1} 启用中',enabledCount,totalEngines)
    );
  }

  const catGrid = $('#category-cards');
  catGrid.replaceChildren();
  for (const cat of overviewData.categories || []) {
    const card = el('div', 'category-card');
    card.append(
      el('strong', '', msg('{0} {1}',cat.icon,msg(cat.label))),
      el('div', 'stage-preview', msg('默认阶段：{0}',(cat.default_stages || []).map(stage=>tr(stage)).join(' → ')))
    );
    const btn = el('button', '', msg('发起{0}',msg(cat.label)));
    btn.type = 'button';
    btn.onclick = () => openCreateDialog(cat.key);
    card.append(btn);
    catGrid.append(card);
  }
}

function renderHomeOpenList(openItems = []) {
  const box = $('#home-open-list');
  const epoch = scopeEpoch;
  box.replaceChildren();
  if (!openItems.length) {
    box.append(el('div', 'empty', msg('当前筛选范围内暂无进行中的协作活动。点击「+ 发起新协作」或在群内发送 /活动 发起 即可开启。')));
    return;
  }
  for (const act of openItems.slice(0, 6)) {
    const row = listButton();
    const head = el('span', 'list-item-head');
    head.append(
      el('span', 'list-item-title', `${act.category_icon || ''} [${act.id}] ${act.title}`),
      el('span', act.status === 'recruiting' ? 'badge warn' : 'badge info', `${act.status_label} · ${act.current_stage}`)
    );
    const meta = el('span', 'list-item-meta');
    meta.append(
      el('span', '', `群聊：${formatScopeLabel(act.umo)}`),
      el('span', '', `参与：${act.participant_count ?? 0} 人`)
    );
    row.append(head, buildStageStepper(act), buildMiniProgress(act), meta);
    row.onclick = async () => {
      if (!row.isConnected || epoch !== scopeEpoch) return;
      await switchTab('activities', act.id);
    };
    box.append(row);
  }
}

async function loadActivities(page = activityPage, directId = '') {
  const valid = nextRequest('activities');
  nextRequest('activityDetail');
  const list = $('#activity-list');
  const detail = $('#activity-detail');
  clearActionDialog();
  renderPagination('#activity-pagination', 1, false, loadActivities);
  setLoading(list, true);
  detail.replaceChildren();
  setLoading(detail, false);
  try {
    const data = await apiGet('activities', {
      umo: currentUmo,
      category: $('#act-category-filter').value,
      status: $('#act-status-filter').value,
      include_minigames: $('#act-include-mini').checked ? 'true' : 'false',
      page,
    });
    if (!valid()) return false;
    activityPage = data.page || page;
    activitiesList = data.items || [];
    if (!directId && !activitiesList.length && activityPage > 1) {
      return await loadActivities(activityPage - 1);
    }
    if (directId) selectedActivityId = directId;
    else if (!activitiesList.some(a => a.id === selectedActivityId)) {
      selectedActivityId = activitiesList[0]?.id || '';
    }
    renderPagination('#activity-pagination', activityPage, data.has_next, loadActivities);
    renderActivityList();
    if (selectedActivityId) return await loadActivityDetail(selectedActivityId);
    detail.replaceChildren(el('div', 'empty', msg('当前筛选条件下没有协作活动。可点击右上角「+ 发起协作活动」创建。')));
    return true;
  } catch (err) {
    if (valid()) showReadError(list, err);
    return false;
  } finally {
    if (valid()) setLoading(list, false);
  }
}

function renderActivityList() {
  const pane = $('#activity-list');
  pane.replaceChildren();
  if (!activitiesList.length) {
    pane.append(el('div', 'empty', '暂无匹配的协作活动'));
    return;
  }
  for (const act of activitiesList) {
    const item = listButton(act.id === selectedActivityId);
    const head = el('span', 'list-item-head');
    const stCls = act.status === 'cancelled'
      ? 'badge danger'
      : act.status === 'completed'
        ? 'badge neutral'
        : act.status === 'recruiting'
          ? 'badge warn'
          : 'badge info';
    head.append(
      el('span', 'list-item-title', `${act.category_icon || ''} [${act.id}] ${act.title}`),
      el('span', stCls, act.status_label)
    );
    const meta = el('span', 'list-item-meta');
    meta.append(
      el('span', '', formatScopeLabel(act.umo)),
      el('span', '', `${act.participant_count ?? 0} 人报名`)
    );
    item.append(head, buildStageStepper(act), buildMiniProgress(act), meta);
    item.onclick = async () => {
      if (!item.isConnected) return;
      selectedActivityId = act.id;
      selectRow(pane, item);
      await loadActivityDetail(act.id);
    };
    pane.append(item);
  }
}

async function loadActivityDetail(id) {
  const pane = $('#activity-detail');
  const valid = nextRequest('activityDetail');
  clearActionDialog();
  setLoading(pane, true);
  try {
    const detail = await apiGet('activity_detail', {id, umo: currentUmo});
    if (!valid()) return false;
    const act = detail.activity;
    if (act.id !== id || (currentUmo && act.umo !== currentUmo)) {
      throw new Error('活动已不在当前会话范围，请刷新后重试。');
    }
    const participants = detail.participants || [];
    const contributions = detail.contributions || [];
    const outcome = detail.outcome;

    pane.replaceChildren();
    const top = el('div', 'card-top');
    const titleWrap = el('div');
    titleWrap.append(
      el('h3', '', `${act.category_icon} [${act.id}] ${act.title}`),
      el('p', 'muted', `${act.category_label} · 会话 ${act.umo} · 发起人 ${act.creator_name} · 创建于 ${fmtTime(act.created_at)}`)
    );
    const badgeCls = act.status === 'cancelled' ? 'badge danger' : act.status === 'completed' ? 'badge' : 'badge warn';
    top.append(titleWrap, el('span', badgeCls, `${act.status_label} · ${act.current_stage}`));
    pane.append(top);

    if (act.description) {
      pane.append(el('p', '', act.description));
    }

    // Stage pipeline
    const pipe = el('div', 'stage-pipeline');
    (act.stages || []).forEach((st, idx) => {
      const cls = act.status === 'completed' || idx < act.stage_index
        ? 'stage-step done'
        : (idx === act.stage_index && act.status !== 'cancelled' ? 'stage-step current' : 'stage-step');
      pipe.append(el('span', cls, `${idx + 1}. ${st}`));
    });
    pane.append(pipe);

    // Progress bar
    const progCount = contributions.length;
    const pct = Math.min(100, Math.round((progCount / Math.max(1, Number(act.target_count || 1))) * 100));
    pane.append(el('div', 'muted', `目标进度：${progCount} / ${act.target_count} (${pct}%) · 最低成团 ${act.min_participants} 人`));
    const track = el('div', 'progress-track');
    const fill = el('div', 'progress-fill');
    fill.style.width = `${pct}%`;
    track.append(fill);
    pane.append(track);

    // Roster
    const joined = participants.filter(p => p.status === 'joined');
    pane.append(el('h3', '', `报名成员（${joined.length} 人）`));
    const chips = el('div', 'roster-chips');
    if (!joined.length) {
      chips.append(el('span', 'muted', '暂无人报名'));
    } else {
      for (const p of joined) {
        const roleTxt = p.role_note ? `（${p.role_note}）` : '';
        chips.append(el('span', 'roster-chip', `${p.name || p.uid}${roleTxt} · 贡献 ${p.contribution_count ?? p.contributions_count ?? 0}`));
      }
    }
    pane.append(chips);

    // Contributions timeline
    pane.append(el('h3', '', `阶段打卡与创作记录（${contributions.length} 条）`));
    const tl = el('div', 'timeline');
    if (!contributions.length) {
      tl.append(el('div', 'muted', '暂无打卡或接棒内容。群友可在群内发送 /活动 打卡 内容 或 /活动 提交 内容。'));
    } else {
      contributions.forEach((c, idx) => {
        const row = el('div', 'timeline-item');
        const h = el('div', 'timeline-head');
        h.append(
          el('span', '', `#${idx + 1} ${c.name || c.uid} · 阶段「${c.stage_name || ''}」`),
          el('span', '', fmtTime(c.created_at))
        );
        row.append(h, el('div', 'timeline-body', c.content));
        tl.append(row);
      });
    }
    pane.append(tl);

    // Actions
    const actions = el('div', 'actions-row');
    if (act.status === 'recruiting' || act.status === 'active') {
      if (act.status === 'recruiting' && act.stage_index === 0) {
        const startBtn = el('button', '', '结束招募 · 开启活动');
        startBtn.type = 'button';
        startBtn.onclick = () => runActivityAction(act, 'start', '', valid, startBtn);
        actions.append(startBtn);
      }
      if (act.status === 'active' && act.stage_index + 1 < (act.stages || []).length) {
        const nextStage = act.stages[act.stage_index + 1];
        const advBtn = el('button', 'primary', `推进至「${nextStage}」`);
        advBtn.type = 'button';
        advBtn.onclick = () => runActivityAction(act, 'advance', '', valid, advBtn);
        actions.append(advBtn);
      }
      const doneBtn = el('button', 'primary', '完结定稿并归档');
      doneBtn.type = 'button';
      doneBtn.onclick = () => openActionDialog(act, 'complete', valid);
      actions.append(doneBtn);

      const cancelBtn = el('button', 'danger', '取消 / 流局');
      cancelBtn.type = 'button';
      cancelBtn.onclick = () => openActionDialog(act, 'cancel', valid);
      actions.append(cancelBtn);
    } else if (act.status === 'completed' && outcome) {
      const viewOutBtn = el('button', 'primary', '打开成果汇编与归档回执');
      viewOutBtn.type = 'button';
      viewOutBtn.onclick = async () => {
        if (!valid()) return;
        await switchTab('outcomes', act.id);
      };
      actions.append(viewOutBtn);
    } else if (act.status === 'cancelled') {
      actions.append(el('span', 'muted', '该活动已流局/取消，根据契约不会写入群史记。'));
    }
    pane.append(actions);
    return true;
  } catch (err) {
    if (valid()) showReadError(pane, err);
    return false;
  } finally {
    if (valid()) setLoading(pane, false);
  }
}

async function runActivityAction(act, action, note, valid, button) {
  if (!valid() || button.disabled) return;
  button.disabled = true;
  try {
    const res = await apiPost('activity_action', {
      id: act.id, umo: act.umo, revision: act.revision, action, note,
    }, valid);
    if (!valid()) return;
    await refreshAll(res.message || '操作已完成');
  } catch (err) {
    if (valid()) setNotice(err.message || '操作失败，请重试。', true);
  } finally {
    button.disabled = false;
  }
}

async function loadOutcomes(page = outcomePage, directId = '') {
  const valid = nextRequest('outcomes');
  nextRequest('outcomeDetail');
  const list = $('#outcome-list');
  const detail = $('#outcome-detail');
  renderPagination('#outcome-pagination', 1, false, loadOutcomes);
  setLoading(list, true);
  detail.replaceChildren();
  setLoading(detail, false);
  try {
    const data = await apiGet('outcomes', {umo: currentUmo, page});
    if (!valid()) return false;
    outcomePage = data.page || page;
    outcomesList = data.items || [];
    if (!directId && !outcomesList.length && outcomePage > 1) {
      return await loadOutcomes(outcomePage - 1);
    }
    if (directId) selectedOutcomeId = directId;
    else if (!outcomesList.some(o => o.activity_id === selectedOutcomeId)) {
      selectedOutcomeId = outcomesList[0]?.activity_id || '';
    }
    renderPagination('#outcome-pagination', outcomePage, data.has_next, loadOutcomes);
    renderOutcomeList();
    if (selectedOutcomeId) return await loadOutcomeDetail(selectedOutcomeId);
    detail.replaceChildren(el('div', 'empty', '暂无已完结成果记录。活动完结后将在此处自动生成作品汇编与入史回执。'));
    return true;
  } catch (err) {
    if (valid()) showReadError(list, err);
    return false;
  } finally {
    if (valid()) setLoading(list, false);
  }
}

function renderOutcomeList() {
  const pane = $('#outcome-list');
  pane.replaceChildren();
  if (!outcomesList.length) {
    pane.append(el('div', 'empty', '暂无已完结成果'));
    return;
  }
  for (const out of outcomesList) {
    const item = listButton(out.activity_id === selectedOutcomeId);
    const head = el('span', 'list-item-head');
    head.append(
      el('span', 'list-item-title', `${out.category_icon || ''} [${out.activity_id}] ${out.title}`),
      el('span', out.valuable ? 'badge' : 'badge neutral', out.valuable ? '可入群史记' : '群内留存')
    );
    const meta = el('span', 'list-item-meta');
    meta.append(
      el('span', '', formatScopeLabel(out.umo)),
      el('span', '', `${out.participant_count} 人参与 · ${out.contribution_count} 条成果`),
      el('span', '', out.memory_doc_id ? `记忆 #${out.memory_doc_id}` : `记忆:${out.memory_status || 'pending'}`),
      el('span', '', out.chronicle_event_id ? `群史记 #${out.chronicle_event_id}` : (out.valuable ? `史记:${out.chronicle_status}` : '不入史'))
    );
    item.append(head, meta);
    item.onclick = async () => {
      if (!item.isConnected) return;
      selectedOutcomeId = out.activity_id;
      selectRow(pane, item);
      await loadOutcomeDetail(out.activity_id);
    };
    pane.append(item);
  }
}

async function loadOutcomeDetail(actId) {
  const pane = $('#outcome-detail');
  const valid = nextRequest('outcomeDetail');
  setLoading(pane, true);
  try {
    const out = await apiGet('outcome_detail', {id: actId, umo: currentUmo});
    if (!valid()) return false;
    if (out.activity_id !== actId || (currentUmo && out.umo !== currentUmo)) {
      throw new Error('成果已不在当前会话范围，请刷新后重试。');
    }
    pane.replaceChildren();
    const top = el('div', 'card-top');
    const left = el('div');
    left.append(
      el('h3', '', `${out.category_icon || ''} [${out.activity_id}] ${out.title}`),
      el('p', 'muted', `${formatScopeLabel(out.umo)} · 定稿于 ${fmtTime(out.created_at)} · ${out.worthiness_reason || ''}`)
    );
    top.append(left, el('span', out.valuable ? 'badge' : 'badge neutral', out.valuable ? `重要度 ${out.importance}` : '常规经历'));
    pane.append(top);

    const badges = el('div', 'roster-chips');
    badges.append(
      el('span', 'roster-chip', out.memory_doc_id ? `✓ 群间记忆已存档 (#${out.memory_doc_id})` : `群间记忆状态：${out.memory_status}`),
      el('span', 'roster-chip', out.chronicle_event_id ? `✓ 群史记已收录 (#${out.chronicle_event_id} · ${out.chronicle_status})` : `群史记状态：${out.valuable ? out.chronicle_status : '未达入史门槛'}`)
    );
    pane.append(badges);

    pane.append(el('p', '', out.summary || ''));
    const md = String(out.artifact_markdown || '');
    pane.append(el('pre', 'markdown-viewer', md));

    const actions = el('div', 'actions-row');
    const copyBtn = el('button', '', '复制 Markdown 全文');
    copyBtn.type = 'button';
    copyBtn.onclick = async () => {
      if (!valid()) return;
      try {
        await navigator.clipboard.writeText(md);
        if (valid()) setNotice('成果全文已复制到剪贴板');
      } catch (err) {
        if (valid()) setNotice(err?.message || '复制失败，请重试。', true);
      }
    };
    const dlBtn = el('button', '', '下载 .md 归档');
    dlBtn.type = 'button';
    dlBtn.onclick = () => {
      if (!valid()) return;
      const blob = new Blob([md], {type: 'text/markdown;charset=utf-8'});
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `${out.activity_id}-${out.title}.md`;
      a.click();
      URL.revokeObjectURL(a.href);
    };
    const syncBtn = el('button', 'primary', '重新同步至记忆与群史记');
    syncBtn.type = 'button';
    syncBtn.onclick = async () => {
      if (!valid() || syncBtn.disabled) return;
      syncBtn.disabled = true;
      try {
        const res = await apiPost('sync_outcome', {id: out.activity_id, umo: out.umo}, valid);
        if (!valid()) return;
        await refreshAll(`归档同步完成：${res.report || '已核验'}`, out.activity_id);
      } catch (err) {
        if (valid()) setNotice(err?.message || '同步失败，请重试。', true);
      } finally {
        syncBtn.disabled = false;
      }
    };
    actions.append(copyBtn, dlBtn, syncBtn);
    pane.append(actions);
    return true;
  } catch (err) {
    if (valid()) showReadError(pane, err);
    return false;
  } finally {
    if (valid()) setLoading(pane, false);
  }
}

function renderEngines() {
  const grid = $('#engine-grid');
  grid.replaceChildren();
  const banner = $('#engines-scope-banner');
  if (banner) {
    banner.hidden = !currentUmo;
    if (currentUmo) {
      setText(banner, `当前群作用域：${formatScopeLabel(currentUmo)}（可单独覆盖本群娱乐引擎启停，无需改动全局默认开关）`);
    }
  }
  for (const eng of overviewData?.engines || []) {
    const title = eng.label || eng.title || eng.key;
    const effEnabled = currentUmo ? (eng.group_enabled ?? eng.enabled) : eng.enabled;
    const card = el('article', `engine-card${effEnabled ? '' : ' off'}`);
    const top = el('div', 'card-top');
    const left = el('div');
    const overrideNote = currentUmo && eng.group_override !== null && eng.group_override !== undefined
      ? ` · 本群覆盖：${eng.group_override ? '开启' : '停用'}`
      : '';
    left.append(
      el('h3', '', title),
      el('p', 'muted', `分类：${eng.category || '群聊娱乐'}${eng.active_rooms ? ` · 当前活跃房间/群组 ${eng.active_rooms}` : ''}${overrideNote}`)
    );
    top.append(left, el('span', effEnabled ? 'badge' : 'badge neutral', effEnabled ? '运行中' : '已停用'));
    card.append(top);
    card.append(el('p', '', eng.desc || eng.summary || ''));

    const chips = el('div', 'cmd-chips');
    const entries = eng.commands || String(eng.entry || '').split('·').map(s => s.trim()).filter(Boolean);
    for (const cmd of entries) {
      chips.append(el('code', '', cmd));
    }
    card.append(chips);

    const actions = el('div', 'actions-row');
    const toggleBtn = el('button', eng.enabled ? '' : 'primary', eng.enabled ? '全局停用' : '全局启用');
    toggleBtn.type = 'button';
    toggleBtn.onclick = async () => {
      const epoch = scopeEpoch;
      const valid = () => epoch === scopeEpoch;
      if (!toggleBtn.isConnected || toggleBtn.disabled) return;
      toggleBtn.disabled = true;
      try {
        const res = await apiPost('toggle_module', {key: eng.key, enabled: !eng.enabled, umo: currentUmo}, valid);
        if (!valid() || !overviewData) return;
        overviewData.engines = res.engines || overviewData.engines;
        renderEngines();
        renderHome();
        setNotice(`已全局${res.enabled ? '启用' : '停用'}「${title}」模块`);
      } catch (err) {
        if (valid()) setNotice(err.message, true);
      } finally {
        toggleBtn.disabled = false;
      }
    };
    actions.append(toggleBtn);

    if (currentUmo) {
      const grpBtn = el('button', effEnabled ? '' : 'primary', effEnabled ? '仅本群停用' : '仅本群启用');
      grpBtn.type = 'button';
      grpBtn.disabled = !eng.enabled;
      grpBtn.onclick = async () => {
        const epoch = scopeEpoch;
        const valid = () => epoch === scopeEpoch;
        if (!grpBtn.isConnected || grpBtn.disabled) return;
        grpBtn.disabled = true;
        try {
          const res = await apiPost('toggle_group_module', {
            umo: currentUmo,
            key: eng.key,
            enabled: !effEnabled,
          }, valid);
          if (!valid() || !overviewData) return;
          overviewData.engines = res.engines || overviewData.engines;
          renderEngines();
          setNotice(`已在当前群${res.enabled ? '启用' : '停用'}「${title}」`);
        } catch (err) {
          if (valid()) setNotice(err.message, true);
        } finally {
          grpBtn.disabled = false;
        }
      };
      actions.append(grpBtn);
    }

    if (eng.key === 'steam_monitor') {
      const steamBtn = el('button', 'primary', '管理 Steam 群绑定');
      steamBtn.type = 'button';
      steamBtn.onclick = () => {
        const panel = $('#steam-console-card');
        if (panel) panel.scrollIntoView({behavior: 'smooth', block: 'start'});
        loadSteamConsole();
      };
      actions.append(steamBtn);
    }
    card.append(actions);
    grid.append(card);
  }
}

async function loadSteamConsole() {
  const box = $('#steam-groups-list');
  const badge = $('#steam-runtime-badge');
  if (!box) return;
  const valid = nextRequest('steamOverview');
  setLoading(box, true);
  try {
    const data = await apiGet('steam_overview', {umo: currentUmo});
    if (!valid()) return;
    const rt = data.runtime_summary || {};
    if (badge) {
      badge.className = rt.has_api_key ? 'badge' : 'badge warn';
      setText(
        badge,
        rt.has_api_key
          ? `监控 ${rt.monitored_groups ?? 0} 群 · ${rt.monitored_players ?? 0} 人 · ${rt.playing_count ?? 0} 在玩`
          : '未配置 Steam API Key'
      );
    }
    const gidInput = $('#steam-bind-gid');
    if (gidInput && !gidInput.value && currentUmo.includes(':GroupMessage:')) {
      gidInput.value = currentUmo.split(':GroupMessage:')[1] || '';
    }
    renderSteamGroups(data.groups || [], data.bindings || []);
  } catch (err) {
    if (valid()) {
      if (badge) {
        badge.className = 'badge neutral';
        setText(badge, 'Steam 监控未就绪');
      }
      box.replaceChildren(el('p', 'empty', err?.message || '无法加载 Steam 监控面板'));
    }
  } finally {
    if (valid()) setLoading(box, false);
  }
}

function renderSteamGroups(groups = [], bindings = []) {
  const box = $('#steam-groups-list');
  if (!box) return;
  box.replaceChildren();
  if (!groups.length && !bindings.length) {
    box.append(el('div', 'empty', '暂无 Steam 监控群组或玩家绑定。可在上方表单输入 SteamID64 直接绑定。'));
    return;
  }
  for (const g of groups) {
    const card = el('div', 'steam-group-row');
    const head = el('div', 'card-top');
    const titleBox = el('div');
    titleBox.append(
      el('strong', '', `群 ${g.group_id}`),
      el('span', 'muted', ` · ${g.player_count || 0} 位监控玩家${g.notify_session ? ` · 路由 ${g.notify_session}` : ''}`)
    );
    const switches = el('div', 'steam-switches');
    for (const [field, label] of [
      ['monitor_enabled', '状态推送'],
      ['achievement_enabled', '成就推送'],
      ['rank_push_enabled', '每日日报'],
    ]) {
      const active = Boolean(g[field]);
      const btn = el('button', active ? 'badge' : 'badge neutral', `${label}：${active ? '开' : '关'}`);
      btn.type = 'button';
      btn.onclick = async () => {
        btn.disabled = true;
        try {
          await apiPost('steam_group_switch', {group_id: g.group_id, field, enabled: !active});
          setNotice(`已更新群 ${g.group_id} 的「${label}」开关`);
          await loadSteamConsole();
        } catch (err) {
          setNotice(err?.message || '切换失败', true);
        } finally {
          btn.disabled = false;
        }
      };
      switches.append(btn);
    }
    head.append(titleBox, switches);
    card.append(head);

    const plist = el('div', 'roster-chips');
    if (!(g.players || []).length) {
      plist.append(el('span', 'muted', '本群暂无监控玩家'));
    } else {
      for (const p of g.players) {
        const chip = el('span', 'roster-chip steam-player-chip');
        const playingTxt = p.playing_game ? ` · 🎮 ${p.playing_game}` : '';
        chip.append(el('span', '', `${p.display_name || p.steam_id} (${p.steam_id})${playingTxt}`));
        const rmBtn = el('button', 'chip-del-btn', '移除');
        rmBtn.type = 'button';
        rmBtn.onclick = async () => {
          rmBtn.disabled = true;
          try {
            await apiPost('steam_player_action', {
              action: 'remove_group',
              group_id: g.group_id,
              steam_id: p.steam_id,
            });
            setNotice(`已从群 ${g.group_id} 移除 ${p.display_name || p.steam_id}`);
            await loadSteamConsole();
          } catch (err) {
            setNotice(err?.message || '移除失败', true);
          } finally {
            rmBtn.disabled = false;
          }
        };
        chip.append(rmBtn);
        plist.append(chip);
      }
    }
    card.append(plist);
    box.append(card);
  }

  if (bindings.length) {
    const bWrap = el('div', 'steam-group-row');
    bWrap.append(el('strong', '', `已绑定玩家账号（${bindings.length}）`));
    const bList = el('div', 'roster-chips');
    for (const b of bindings.slice(0, 40)) {
      const chip = el('span', 'roster-chip steam-player-chip');
      const label = `${b.nickname || b.last_persona_name || b.steam_id} · ${b.steam_id}${b.qq_id ? ` · QQ:${b.qq_id}` : ''}`;
      chip.append(el('span', '', label));
      const unbindBtn = el('button', 'chip-del-btn', '解绑');
      unbindBtn.type = 'button';
      unbindBtn.onclick = async () => {
        unbindBtn.disabled = true;
        try {
          await apiPost('steam_player_action', {action: 'unbind', steam_id: b.steam_id});
          setNotice(`已解除 ${b.steam_id} 绑定`);
          await loadSteamConsole();
        } catch (err) {
          setNotice(err?.message || '解绑失败', true);
        } finally {
          unbindBtn.disabled = false;
        }
      };
      chip.append(unbindBtn);
      bList.append(chip);
    }
    bWrap.append(bList);
    box.append(bWrap);
  }
}

function openCreateDialog(defaultCategory = 'reading') {
  const error = $('#create-error');
  if (error) error.textContent = '';
  $('#create-category').value = defaultCategory;
  if (currentUmo && !$('#create-umo').value) {
    $('#create-umo').value = currentUmo;
  } else if (!$('#create-umo').value && overviewData?.stats?.known_umos?.length) {
    $('#create-umo').value = overviewData.stats.known_umos[0];
  }
  $('#create-dialog').showModal();
}

function openActionDialog(act, action, valid) {
  if (!valid()) return;
  pendingModalAction = {act, action, valid};
  $('#action-dialog-title').textContent = action === 'complete'
    ? `完结定稿：[${act.id}] ${act.title}`
    : `取消流局：[${act.id}] ${act.title}`;
  $('#action-dialog-desc').textContent = action === 'complete'
    ? '完结后将自动生成汇编文稿，同步经历至「群间记忆」，并在满足多人协作条件时提交「群史记」。'
    : '取消后的流局活动仅标记为已取消，绝不会进入「群史记」。';
  $('#action-confirm-btn').className = action === 'complete' ? 'primary' : 'danger';
  $('#action-confirm-btn').textContent = action === 'complete' ? '确认完结并归档' : '确认取消活动';
  $('#action-note').value = '';
  $('#action-dialog').showModal();
}

function clearActionDialog() {
  pendingModalAction = null;
  const dialog = $('#action-dialog');
  if (dialog.open) dialog.close();
}

async function switchTab(tab, directId = '') {
  if (activeTab !== tab) {
    if (activeTab === 'activities') {
      nextRequest('activities');
      nextRequest('activityDetail');
      clearActionDialog();
    }
    if (activeTab === 'outcomes') {
      nextRequest('outcomes');
      nextRequest('outcomeDetail');
    }
  }
  activeTab = tab;
  for (const btn of document.querySelectorAll('[data-tab]')) {
    const isSel = btn.dataset.tab === tab;
    btn.classList.toggle('selected', isSel);
    btn.setAttribute('aria-selected', String(isSel));
  }
  for (const name of ['home', 'activities', 'outcomes', 'engines']) {
    $(`#tab-${name}`).hidden = name !== tab;
  }
  if (tab === 'activities') await loadActivities(activityPage, directId);
  if (tab === 'outcomes') await loadOutcomes(outcomePage, directId);
  if (tab === 'engines') {
    renderEngines();
    await loadSteamConsole();
  }
}

async function refreshAll(message = '', directOutcomeId = '') {
  const valid = nextRequest('overview');
  const refresh = $('#refresh-btn');
  refresh.setAttribute('aria-busy', 'true');
  try {
    const [overview, openRes] = await Promise.all([
      apiGet('overview', {umo: currentUmo}),
      apiGet('activities', {umo: currentUmo, status: 'open'}),
    ]);
    if (!valid()) return false;
    overviewData = overview;
    syncUmoDropdown(overviewData.stats?.known_umos || []);
    renderStats(overviewData.stats || {});
    renderHome();
    renderHomeOpenList(openRes.items || []);
    if (activeTab === 'activities' && !await loadActivities()) return false;
    if (activeTab === 'outcomes' && !await loadOutcomes(outcomePage, directOutcomeId)) return false;
    if (activeTab === 'engines') {
      renderEngines();
      await loadSteamConsole();
    }
    if (!valid()) return false;
    setNotice(message || `已同步群间游戏控制台 (${overviewData.version || 'v0.1.0'})${currentUmo ? ' · 当前会话 ' + formatScopeLabel(currentUmo) : ''}`);
    return true;
  } catch (err) {
    if (valid()) setNotice(err.message || '读取失败', true);
    return false;
  } finally {
    if (valid()) refresh.setAttribute('aria-busy', 'false');
  }
}

function changeScope(input, share = false) {
  const next = groupScope(input);
  if (next === currentUmo) return;
  currentUmo = next;
  scopeEpoch += 1;
  selectedActivityId = '';
  selectedOutcomeId = '';
  activityPage = 1;
  outcomePage = 1;
  renderPagination('#activity-pagination', 1, false, loadActivities);
  renderPagination('#outcome-pagination', 1, false, loadOutcomes);
  activitiesList = [];
  outcomesList = [];
  overviewData = null;
  $('#global-umo').value = next;
  for (const sel of ['#stats-bar', '#home-open-list', '#activity-list', '#activity-detail', '#outcome-list', '#outcome-detail', '#engine-grid']) {
    const pane = $(sel);
    pane.replaceChildren();
    setLoading(pane, false);
  }
  clearActionDialog();
  const createDialog = $('#create-dialog');
  if (createDialog.open) createDialog.close();
  $('#create-umo').value = next;
  if (share) setSharedScope(next, {syncUrl: true, emit: false});
  setNotice('正在切换会话…');
  refreshAll();
}

// Event bindings
for (const btn of document.querySelectorAll('[data-tab]')) {
  btn.addEventListener('click', () => switchTab(btn.dataset.tab));
}
$('#refresh-btn').addEventListener('click', () => refreshAll());
$('#global-umo').addEventListener('change', e => changeScope(e.target.value, true));
window.addEventListener('dynamics-scopechange', e => changeScope(e.detail?.umo));
$('#act-category-filter').addEventListener('change', () => loadActivities(1));
$('#act-status-filter').addEventListener('change', () => loadActivities(1));
$('#act-include-mini').addEventListener('change', () => loadActivities(1));

$('#goto-create-btn').addEventListener('click', () => openCreateDialog('reading'));
$('#open-create-dialog').addEventListener('click', () => openCreateDialog($('#act-category-filter').value || 'reading'));
$('#goto-activities-btn').addEventListener('click', () => switchTab('activities'));
const steamRefreshBtn = $('#steam-panel-refresh-btn');
if (steamRefreshBtn) steamRefreshBtn.addEventListener('click', () => loadSteamConsole());
const steamBindForm = $('#steam-bind-form');
if (steamBindForm) {
  steamBindForm.addEventListener('submit', async e => {
    e.preventDefault();
    const btn = $('#steam-bind-submit');
    if (btn?.disabled) return;
    if (btn) btn.disabled = true;
    try {
      const res = await apiPost('steam_player_action', {
        action: 'bind',
        group_id: $('#steam-bind-gid')?.value.trim() || '',
        steam_id: $('#steam-bind-sid')?.value.trim() || '',
        qq_id: $('#steam-bind-qq')?.value.trim() || '',
        nickname: $('#steam-bind-nick')?.value.trim() || '',
      });
      if ($('#steam-bind-sid')) $('#steam-bind-sid').value = '';
      if ($('#steam-bind-qq')) $('#steam-bind-qq').value = '';
      if ($('#steam-bind-nick')) $('#steam-bind-nick').value = '';
      setNotice(res?.message || 'Steam 绑定成功');
      await loadSteamConsole();
    } catch (err) {
      setNotice(err?.message || 'Steam 绑定失败', true);
    } finally {
      if (btn) btn.disabled = false;
    }
  });
}

$('#create-cancel-btn').addEventListener('click', () => $('#create-dialog').close());
$('#create-form').addEventListener('submit', async e => {
  e.preventDefault();
  const submitBtn = $('#create-submit-btn');
  if (submitBtn.disabled) return;
  const error = $('#create-error');
  if (error) error.textContent = '';
  const epoch = scopeEpoch;
  const valid = () => epoch === scopeEpoch;
  submitBtn.disabled = true;
  try {
    const res = await apiPost('activity_create', {
      umo: $('#create-umo').value.trim(),
      category: $('#create-category').value,
      title: $('#create-title').value.trim(),
      description: $('#create-desc').value.trim(),
      stages: $('#create-stages').value.trim(),
      target_count: Number($('#create-target').value || 5),
      min_participants: Number($('#create-min-p').value || 2),
    }, valid);
    if (!valid()) return;
    $('#create-dialog').close();
    $('#create-title').value = '';
    $('#create-desc').value = '';
    $('#create-stages').value = '';
    const createdId = res.activity?.id || '';
    await refreshAll(`已发起活动 [${createdId}]`);
    if (valid()) await switchTab('activities', createdId);
  } catch (err) {
    if (valid()) {
      const message = err?.message || '创建失败，请重试。';
      if (error) setText(error, message);
      else setNotice(message, true);
    }
  } finally {
    submitBtn.disabled = false;
  }
});

$('#action-cancel-btn').addEventListener('click', clearActionDialog);
$('#action-form').addEventListener('submit', async e => {
  e.preventDefault();
  if (!pendingModalAction) return;
  const {act, action, valid} = pendingModalAction;
  const note = $('#action-note').value.trim();
  clearActionDialog();
  await runActivityAction(act, action, note, valid, $('#action-confirm-btn'));
});

const initialQuery = new URLSearchParams(location.search);
const initialTab = initialQuery.get('tab');
const initialId = (initialQuery.get('id') || '').trim();
if (initialTab && ['home', 'activities', 'outcomes', 'engines'].includes(initialTab)) {
  refreshAll().then(() => {
    if (initialTab !== 'home') switchTab(initialTab, initialId);
  });
} else {
  refreshAll();
}

window.addEventListener('dynamics-cmdk-deeplink', e => {
  const link = e.detail?.deep_link;
  if (!link || typeof link !== 'object') return;
  const tab = String(link.tab || '');
  const id = String(link.id || '').trim();
  const umo = groupScope(link.umo || '');
  if (umo && umo !== currentUmo) {
    changeScope(umo, true);
  }
  if (['home', 'activities', 'outcomes', 'engines'].includes(tab)) {
    switchTab(tab, id);
  }
});

window.addEventListener('series-languagechange', () => {
  if (overviewData) renderHome();
  const selector=$('#global-umo');
  if (selector?.firstChild?.value==='') setText(selector.firstChild,msg('全部群聊会话'));
});
