/**
 * PT站点签到插件 —— 站点规则采集脚本（两段式）
 *
 * 为什么分两段：光看页面 DOM 只能知道按钮长什么样，不点到、不抓到那个请求，
 * 就没法确定签到到底发给谁、成功后页面变成什么。所以要先把监听挂上，再点一次签到。
 *
 * 用法：
 *   1. 浏览器登录目标站，打开签到页（按钮在菜单里的，先别展开）
 *   2. F12 → Console，整段粘贴本脚本，回车（会出现「已挂好监听」）
 *   3. 点一次签到按钮（该展开菜单的照常展开、照常点）
 *   4. 回到 Console 执行：__ptsigninDump()
 *   5. 把输出的 JSON 发出来即可
 *
 * 只监听，不改页面逻辑；不会读取或输出 Cookie；请求体超过 600 字会截断。
 */
(() => {
  const MAX = 600;
  const state = {
    startedAt: new Date().toISOString(),
    before: (document.body.innerText || "").replace(/\s+/g, " ").trim().slice(0, 1500),
    clicks: [],
    requests: [],
  };
  window.__ptsigninCollector = state;

  const selectorOf = (el) => {
    if (!el || el.nodeType !== 1) return "";
    if (el.id) return "#" + el.id;
    const cls = [...el.classList].slice(0, 3).map((c) => "." + c).join("");
    const attr = el.getAttribute("name") ? `[name='${el.getAttribute("name")}']` : "";
    return el.tagName.toLowerCase() + cls + attr;
  };
  const textOf = (el) => ((el && (el.value || el.textContent)) || "").trim().replace(/\s+/g, " ").slice(0, 60);
  const trim = (value) => String(value || "").slice(0, MAX);

  // 1) 抓点击：记下点了谁、按钮长什么样、页面上有哪些可点的元素（菜单项可能就是后面才渲染的）
  document.addEventListener("click", (event) => {
    const el = event.target;
    if (!el || state.clicks.length >= 6) return;
    state.clicks.push({
      selector: selectorOf(el),
      text: textOf(el),
      tag: el.tagName,
      html: trim((el.outerHTML || "").replace(/\s+/g, " ")),
      parentHtml: trim(((el.parentElement && el.parentElement.outerHTML) || "").replace(/\s+/g, " ")),
      at: new Date().toISOString(),
    });
  }, true);

  // 2) 抓 fetch
  const originalFetch = window.fetch;
  if (typeof originalFetch === "function") {
    window.fetch = async function (...args) {
      const request = args[0];
      const init = args[1] || {};
      const url = typeof request === "string" ? request : (request && request.url) || "";
      const method = (init.method || (request && request.method) || "GET").toUpperCase();
      let body = init.body;
      if (body instanceof FormData) {
        body = [...body.entries()].map(([k, v]) => `${k}=${trim(v)}`).join("&");
      }
      const record = { kind: "fetch", method, url: trim(url), body: trim(body) };
      state.requests.push(record);
      try {
        const response = await originalFetch.apply(this, args);
        record.status = response.status;
        try {
          const clone = response.clone();
          record.response = trim(await clone.text());
        } catch (err) { /* 响应不可复制时忽略 */ }
        return response;
      } catch (error) {
        record.error = String(error);
        throw error;
      }
    };
  }

  // 3) 抓 XMLHttpRequest
  const originalOpen = XMLHttpRequest.prototype.open;
  const originalSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.__ptsignin = { kind: "xhr", method: (method || "GET").toUpperCase(), url: trim(url) };
    return originalOpen.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function (body) {
    const record = this.__ptsignin || { kind: "xhr" };
    record.body = body instanceof FormData
      ? [...body.entries()].map(([k, v]) => `${k}=${trim(v)}`).join("&")
      : trim(body);
    state.requests.push(record);
    this.addEventListener("loadend", () => {
      record.status = this.status;
      try { record.response = trim(this.responseText); } catch (err) { /* 忽略 */ }
    });
    return originalSend.call(this, body);
  };

  // 4) 输出：点完之后执行 __ptsigninDump()
  window.__ptsigninDump = () => {
    const buttons = [...document.querySelectorAll("a, button, input[type=submit], [role='button'], [role='menuitem'], li")]
      .filter((el) => /签到|簽到|打卡|领取|領取|立即/.test(textOf(el)))
      .slice(0, 12)
      .map((el) => ({ selector: selectorOf(el), text: textOf(el), html: trim((el.outerHTML || "").replace(/\s+/g, " ")) }));
    const result = {
      url: location.href,
      title: document.title,
      after: (document.body.innerText || "").replace(/\s+/g, " ").trim().slice(0, 1500),
      clicks: state.clicks,
      requests: state.requests.slice(0, 12),
      signinButtons: buttons,
      forms: [...document.querySelectorAll("form")].slice(0, 6).map((form) => ({
        action: form.action,
        method: (form.method || "get").toUpperCase(),
        fields: [...form.querySelectorAll("input,select,textarea")].map((i) => i.name).filter(Boolean),
      })),
      signals: {
        waf: /正在进行安全检测|雷池|SafeLine/i.test(document.body.innerText || ""),
        cloudflare: !!document.querySelector("#cf-chl-widget, iframe[src*='challenges.cloudflare.com']"),
        turnstile: !!document.querySelector(".cf-turnstile"),
        imageCaptcha: !!document.querySelector("img[src*='captcha' i], img[src*='verify' i]"),
      },
    };
    const payload = JSON.stringify(result, null, 2);
    console.log(payload);
    try { copy(payload); } catch (err) { /* 非 Chrome 时手动复制 */ }
    return "已复制到剪贴板（若没自动复制，从上面的输出里全选复制）";
  };

  return "已挂好监听。现在点一次签到按钮，再执行 __ptsigninDump() 复制结果。";
})();
