// ==UserScript==
// @name         Shopee 台账 · 参数采集
// @namespace    shopee-ledger
// @version      0.1
// @description  在 Shopee 页面上把渲染后的正文送回本机台账，产出「候选值」等人工确认。本脚本不会直接修改任何参数。
// @author       shopee-ledger
// @match        https://shopee.cn/edu/*
// @match        https://seller.shopee.cn/*
// @match        https://help.shopee.tw/*
// @match        https://seller.shopee.tw/*
// @match        https://shopee.tw/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @run-at       document-idle
// ==/UserScript==

/*
 * 为什么必须是浏览器脚本，而不是服务端抓取
 * ------------------------------------------
 * 实测（2026-10-03）服务端抓 4 个公开来源，0 成功：
 *   · shopee.cn/edu 是 JS 空壳 —— 服务端拿到骨架，内容靠浏览器渲染
 *   · help.shopee.tw 超时
 *   · chinatax.gov.cn SSL 证书链不全
 *   · seller.shopee.cn 需要登录态
 * 浏览器正好补上这三点：执行 JS、带登录态、有完整证书链。
 * 提取规则仍然只写在 spec/sources.json 里一份，这里只负责"把渲染后的文本送回去"。
 *
 * 安全边界
 * --------
 * · 只发到 127.0.0.1（本机），不外发。
 * · 默认发整页正文；按住 Alt 再点，只发你选中的那段（更省、更干净）。
 * · 本机端点只写「候选值」，不直接改参数——最坏情况是多一条待确认记录。
 */

(function () {
  'use strict';

  const APP = 'http://127.0.0.1:8765';
  const PANEL_ID = 'shopee-ledger-capture';

  function log(...args) {
    console.log('[台账采集]', ...args);
  }

  function request(options) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: options.method || 'GET',
        url: options.url,
        headers: options.headers,
        data: options.data,
        timeout: 20000,
        onload: (res) => resolve(res),
        onerror: () => reject(new Error('连不上本机台账：' + APP + '。先运行 python -m shopee_ledger web')),
        ontimeout: () => reject(new Error('本机台账无响应')),
      });
    });
  }

  async function loadSources() {
    const res = await request({ url: APP + '/sources.json' });
    if (res.status !== 200) throw new Error('配方清单读取失败：HTTP ' + res.status);
    return JSON.parse(res.responseText);
  }

  async function sendCapture(paramId, text) {
    const body = JSON.stringify({
      param_id: paramId,
      url: location.href,
      text: text,
      captured_at: new Date().toISOString(),
    });
    const res = await request({
      method: 'POST',
      url: APP + '/ingest',
      headers: { 'Content-Type': 'application/json' },
      data: body,
    });
    return JSON.parse(res.responseText);
  }

  // 列表页：把 <a> 和它所在条目的文字（含日期）一起送回去。
  // 链接和日期只存在于 DOM 里，innerText 拿不到——所以列表要走结构化通道。
  function collectLinks() {
    const found = [];
    document.querySelectorAll('a[href*="/article/"]').forEach((anchor) => {
      const box = anchor.closest('li, tr, article, div') || anchor.parentElement;
      found.push({
        href: anchor.href,
        text: (anchor.innerText || '').trim(),
        date: ((box && box.innerText) || '').slice(0, 200),
      });
    });
    return found;
  }

  async function loadWatches() {
    const res = await request({ url: APP + '/watches.json' });
    if (res.status !== 200) return [];
    return JSON.parse(res.responseText);
  }

  function sameUrl(a, b) {
    try {
      const left = new URL(a);
      const right = new URL(b);
      const norm = (u) => u.origin.replace('://www.', '://') + u.pathname.replace(/\/$/, '') + u.search;
      return norm(left) === norm(right);
    } catch (err) {
      return false;
    }
  }

  // 自动报送：打开的就是已登记的列表页时，每天送一次。
  // 不设定时器——列表页是浏览器渲染的，服务端抓不到；"你打开过"就是最好的触发条件。
  async function autoReportIfWatched() {
    const watches = await loadWatches();
    const hit = watches.find((item) => item.url && sameUrl(item.url, location.href));
    if (!hit) return null;
    const key = 'sl-listing-sent:' + hit.id;
    const today = new Date().toISOString().slice(0, 10);
    if (localStorage.getItem(key) === today) return null;
    const result = await sendListing();
    if (result && result.ok) localStorage.setItem(key, today);
    return { watch: hit, result: result };
  }

  // 把页面真实发出的数据请求报回去。
  // 列表页是 SPA，HTML 里没有文章链接；接口地址藏在混淆过的 JS 包里（试过 24 种组合全 404）。
  // 但浏览器已经在调它了——performance entries 里就是真实 URL。与其猜，不如让它自己报。
  function discoverApiCalls() {
    try {
      return performance
        .getEntriesByType('resource')
        .filter((entry) => ['xmlhttprequest', 'fetch'].includes(entry.initiatorType))
        .map((entry) => entry.name)
        .filter((url, index, all) => all.indexOf(url) === index)
        .slice(0, 20);
    } catch (err) {
      return [];
    }
  }

  async function sendListing() {
    const res = await request({
      method: 'POST',
      url: APP + '/ingest',
      headers: { 'Content-Type': 'application/json' },
      data: JSON.stringify({
        url: location.href,
        links: collectLinks(),
        api_calls: discoverApiCalls(),
      }),
    });
    return JSON.parse(res.responseText);
  }

  function buildPanel(sources) {
    const box = document.createElement('div');
    box.id = PANEL_ID;
    box.style.cssText = [
      'position:fixed', 'right:16px', 'bottom:16px', 'z-index:2147483647',
      'width:320px', 'padding:12px', 'border-radius:10px',
      'background:#18181b', 'color:#fafafa', 'font:12px/1.5 system-ui,sans-serif',
      'box-shadow:0 8px 24px rgba(0,0,0,.35)',
    ].join(';');

    const options = sources
      .map((s) => `<option value="${s.param_id}">${s.param_id} · ${s.access === 'login' ? '需登录' : '公开'}</option>`)
      .join('');

    box.innerHTML = `
      <div style="font-weight:600;margin-bottom:8px">台账采集 <span style="color:#a1a1aa;font-weight:400">· 只产出候选</span></div>
      <select id="sl-param" style="width:100%;padding:6px;border-radius:6px;margin-bottom:6px">${options}</select>
      <div style="color:#a1a1aa;margin-bottom:8px">默认发整页正文；按住 <b>Alt</b> 再点只发选中部分。</div>
      <button id="sl-send" style="width:100%;padding:8px;border:0;border-radius:6px;background:#1d4ed8;color:#fff;cursor:pointer">抓这一页</button>
      <button id="sl-list" style="width:100%;margin-top:6px;padding:6px;border:1px solid #3f3f46;border-radius:6px;background:transparent;color:#d4d4d8;cursor:pointer">这是列表页 → 只看有哪些新文档</button>
      <pre id="sl-out" style="white-space:pre-wrap;margin:8px 0 0;color:#a1a1aa;max-height:180px;overflow:auto"></pre>
      <div id="sl-toggle" style="margin-top:6px;color:#52525b;cursor:pointer;text-align:right">收起</div>
    `;
    document.body.appendChild(box);

    const out = box.querySelector('#sl-out');
    const button = box.querySelector('#sl-send');
    const listButton = box.querySelector('#sl-list');
    const select = box.querySelector('#sl-param');

    function show(text) {
      out.textContent = typeof text === 'string' ? text : JSON.stringify(text, null, 2);
    }

    listButton.addEventListener('click', async () => {
      show('读取列表…');
      try {
        const result = await sendListing();
        if (!result.ok) {
          show('❌ ' + (result.error || result.status || '未解析出条目'));
          return;
        }
        const fresh = (result.new || [])
          .map((item) => '  · ' + (item.published_at || '日期未知') + '  ' + item.title)
          .join('\n');
        show(
          '📄 这个列表共 ' + result.total + ' 篇（已有记录 ' + (result.total - (result.new || []).length) + ' 篇）\n' +
          (result.new && result.new.length
            ? '🆕 新出现 ' + result.new.length + ' 篇：\n' + fresh
            : '（没有新文档）') +
          (result.gone && result.gone.length ? '\n⚠️ 消失 ' + result.gone.length + ' 篇（可能翻页变化）' : '') +
          (result.api_calls && result.api_calls.length
            ? '\n\n🔌 顺手记下 ' + result.api_calls.length + ' 个数据接口（可用于服务端翻页）：\n' +
              result.api_calls.slice(0, 3).map((u) => '  ' + u.slice(0, 78)).join('\n')
            : '') +
          '\n\n只发现，不取值。要取值就点进文章，切到对应参数再点「抓这一页」。'
        );
      } catch (err) {
        show('❌ ' + err.message);
      }
    });

    button.addEventListener('click', async (event) => {
      const paramId = select.value;
      const selection = String(window.getSelection() || '').trim();
      const useSelection = event.altKey && selection.length > 0;
      const text = useSelection ? selection : document.body.innerText;
      show('发送中…（' + (useSelection ? '选中部分 ' : '整页 ') + text.length + ' 字）');
      try {
        const result = await sendCapture(paramId, text);
        if (result.ok) {
          show(
            '✅ 已记为候选 #' + result.candidate_id + '\n' +
            '参数 ' + result.param_id + '\n' +
            '抓到 ' + JSON.stringify(result.value) + '\n' +
            '当前 ' + JSON.stringify(result.current_value) + '\n' +
            (result.changed ? '⚠️ 与当前值不同\n' : '（与当前值一致）\n') +
            '快照 ' + (result.snapshot_ref || '—') + '\n\n' + result.hint
          );
        } else {
          show('❌ ' + result.status + '\n' + (result.message || result.error) + '\n\n' + (result.hint || ''));
        }
      } catch (err) {
        show('❌ ' + err.message);
      }
    });

    box.querySelector('#sl-toggle').addEventListener('click', () => {
      const hidden = out.style.display === 'none';
      out.style.display = hidden ? 'block' : 'none';
      button.style.display = hidden ? 'block' : 'none';
      listButton.style.display = hidden ? 'block' : 'none';
      select.style.display = hidden ? 'block' : 'none';
      box.querySelector('#sl-toggle').textContent = hidden ? '收起' : '展开';
    });
  }

  (async function main() {
    try {
      const sources = await loadSources();
      if (Array.isArray(sources) && sources.length > 0) {
        buildPanel(sources);
        log('已加载 ' + sources.length + ' 条配方');
      }
      const auto = await autoReportIfWatched();
      if (auto && auto.result && auto.result.ok) {
        const fresh = (auto.result.new || []).length;
        log('已自动报送列表页 ' + auto.watch.id + '：共 ' + auto.result.total +
            ' 篇，新出现 ' + fresh + ' 篇');
        if (fresh > 0) {
          console.log('[台账采集] 🆕 新文档：',
                      (auto.result.new || []).map((i) => i.published_at + ' ' + i.title));
        }
      }
    } catch (err) {
      log('未注入面板：' + err.message);
    }
  })();
})();
