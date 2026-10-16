/**
 * PT站点签到插件 —— 站点规则采集脚本
 *
 * 用法：
 *   1. 浏览器登录目标 PT 站，打开【签到页面】（不是首页，是真正点签到的那个页面）
 *   2. F12 打开开发者工具 → Console 标签
 *   3. 整段粘贴本脚本 → 回车
 *   4. 复制输出的 JSON，发给我，我据此生成站点规则
 *
 * 说明：
 *   - 本脚本只读取页面 DOM，不会发起任何请求，不会读取或输出 Cookie
 *   - 如果签到按钮需要登录后才显示，请务必先登录再执行
 */
(() => {
  const pick = (el) => {
    if (!el) return null;
    if (el.id) return "#" + el.id;
    if (el.getAttribute("name")) return `${el.tagName.toLowerCase()}[name='${el.getAttribute("name")}']`;
    const cls = [...el.classList].slice(0, 2).map((c) => "." + c).join("");
    return cls ? el.tagName.toLowerCase() + cls : el.tagName.toLowerCase();
  };
  const text = (el) => (el.value || el.textContent || "").trim().replace(/\s+/g, " ").slice(0, 30);

  const buttons = [...document.querySelectorAll("input[type=submit], button, a.btn, a[href*='sign'], a[onclick]")]
    .filter((el) => /签到|簽到|打卡|领取|領取|立即|提交/.test(text(el)));

  const captchaImgs = [...document.querySelectorAll("img")]
    .filter((i) => /captcha|verify|code|imagestring|checkcode/i.test(i.src + i.id + i.className));

  const captchaInputs = [...document.querySelectorAll("input")]
    .filter((i) => /imagestring|captcha|verify|code|checkcode/i.test(i.name + i.id));

  // Turnstile / 人机验证现场：widget 怎么渲染的、token 落在哪里、有没有可调的回调、
  // 有没有提交按钮或表单——决定插件该用 auto（两段式注入提交）还是别的提交方式。
  const turnstile = (() => {
    const roots = [document];
    for (let index = 0; index < roots.length; index += 1) {
      roots[index].querySelectorAll("*").forEach((el) => {
        if (el.shadowRoot) roots.push(el.shadowRoot);
      });
    }
    const pickAll = (selector) => {
      const found = [];
      roots.forEach((root) => {
        root.querySelectorAll(selector).forEach((node) => {
          if (found.indexOf(node) < 0) found.push(node);
        });
      });
      return found;
    };
    const widgets = pickAll(".cf-turnstile, [data-sitekey]");
    const responses = pickAll('input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]');
    const callbackNodes = pickAll("[data-callback]");
    const globals = [];
    try {
      Object.keys(window).forEach((key) => {
        if (typeof window[key] === "function" && /turnstile|callback|verify|captcha/i.test(key)) {
          globals.push(key);
        }
      });
    } catch (err) { /* 跨域属性访问失败时忽略 */ }
    return {
      widgetCount: widgets.length,
      widgetHtml: widgets.map((el) => (el.outerHTML || "").replace(/\s+/g, " ").slice(0, 240)),
      dataCallback: callbackNodes.map((el) => el.getAttribute("data-callback")),
      responseInputs: responses.map((el) => ({ sel: pick(el), valueLen: (el.value || "").length })),
      openShadowRoots: roots.length - 1,
      apiAvailable: !!(window.turnstile && typeof window.turnstile.getResponse === "function"),
      apiTokenLen: (() => {
        try {
          return window.turnstile && window.turnstile.getResponse
            ? (window.turnstile.getResponse() || "").length : 0;
        } catch (err) { return -1; }
      })(),
      candidateGlobals: globals.slice(0, 20),
      submitButtons: [...document.querySelectorAll("input[type=submit], button[type=submit]")]
        .map((el) => ({ sel: pick(el), text: text(el) })),
    };
  })();

  const result = {
    url: location.href,
    title: document.title,
    forms: [...document.querySelectorAll("form")].map((f) => ({
      action: f.action,
      method: (f.method || "get").toUpperCase(),
      fields: [...f.querySelectorAll("input,select,textarea")].map((i) => i.name).filter(Boolean),
      text: (f.textContent || "").replace(/\s+/g, " ").trim().slice(0, 80),
    })),
    buttons: buttons.map((b) => ({ sel: pick(b), text: text(b) })),
    captchaImgs: captchaImgs.map((i) => ({ sel: pick(i), src: i.src.slice(0, 90) })),
    captchaInputs: captchaInputs.map((i) => pick(i)),
    turnstile,
    signals: {
      turnstile: !!document.querySelector(".cf-turnstile, iframe[src*='challenges.cloudflare.com']"),
      cloudflare: !!document.querySelector("#cf-chl-widget, .cf-turnstile, iframe[src*='challenges.cloudflare.com']"),
      recaptcha: !!document.querySelector(".g-recaptcha, iframe[src*='recaptcha']"),
      altcha: !!document.querySelector("altcha-widget"),
      hasImageCaptcha: captchaImgs.length > 0,
      needLogin: /用户登录|會員登入|请先登录|login/i.test(document.body.innerText.slice(0, 3000)),
    },
    pageText: document.body.innerText.replace(/\s+/g, " ").trim().slice(0, 400),
  };

  console.log(JSON.stringify(result, null, 2));
  copy(JSON.stringify(result, null, 2)); // Chrome/Edge：自动复制到剪贴板
  return "已输出并复制到剪贴板，直接粘贴给我即可";
})();
