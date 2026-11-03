"""猪猪（piggo.me）专用 Browserless 脚本与 BrowserQL 查询。

这里只生成在远程浏览器中执行的 JS / BrowserQL，不处理站点凭据、HTTP 请求或结果判定；
这些仍由 core.BrowserlessSigner 负责。账号密码只作为脚本变量传入，不写入日志。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional


def _piggo_value(data: Mapping[str, Any], key: str) -> Dict[str, Any]:
    """读取 Piggo BrowserQL 的 evaluate 返回值；解析失败时返回空字典。"""
    item = data.get(key) if isinstance(data, Mapping) else None
    value = item.get("value") if isinstance(item, Mapping) else item
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _piggo_state_script(rule: Mapping[str, Any]) -> str:
    selector = json.dumps(str(rule.get("playwright_state_selector") or ".cal-att-pill"))
    already = json.dumps([str(item) for item in (rule.get("playwright_already_texts") or [])
                          if str(item).strip()], ensure_ascii=False)
    skip = json.dumps([str(item) for item in (rule.get("playwright_skip_texts") or [])
                       if str(item).strip()], ensure_ascii=False)
    calendar = json.dumps(str(rule.get("playwright_calendar_selector") or ".cal-wrap"))
    calendar_loading = json.dumps(
        str(rule.get("playwright_calendar_loading_selector") or ".cal-wrap[aria-busy='true']"))
    return """(() => {
      const node = document.querySelector(%s);
      const visible = (el) => !!el && (el.offsetParent !== null || el.getClientRects().length > 0);
      const label = node ? ((node.innerText || node.textContent || node.value || '') + ' '
        + (node.getAttribute('aria-label') || '') + ' ' + (node.getAttribute('title') || ''))
        .replace(/\\s+/g, ' ').trim() : '';
      const alreadyTexts = %s;
      const skipTexts = %s;
      const disabled = !!node && (!!node.disabled || node.getAttribute('aria-disabled') === 'true');
      const already = alreadyTexts.some((word) => label.includes(word));
      const skipped = skipTexts.some((word) => label.includes(word));
      const actionable = !!node && visible(node) && !disabled && !already && !skipped
        && label.startsWith('签到');
      const calendarNode = document.querySelector(%s);
      const calendarLoading = visible(document.querySelector(%s));
      const loginUser = document.querySelector("input[name='username']");
      const loginPassword = document.querySelector("input[name='password']");
      const loginSubmit = document.querySelector('button.btn');
      const unlockPassword = document.querySelector("form.step-up input[name='password']");
      const unlockSubmit = document.querySelector("form.step-up button[type='submit']");
      const bodyText = (document.body && document.body.innerText) || '';
      return JSON.stringify({
        url: location.href,
        title: document.title || '',
        calendar: {found: !!calendarNode, loading: calendarLoading,
                   ready: !!calendarNode && !calendarLoading},
        login: visible(loginUser) && visible(loginPassword) && visible(loginSubmit),
        locked: visible(document.querySelector('.denied-page, #idle-lock-title'))
          || bodyText.includes('页面已锁定') || bodyText.includes('空闲过久，请验证是你本人'),
        stepUp: visible(unlockPassword) && visible(unlockSubmit),
        control: {found: !!node, present: visible(node), disabled: disabled,
                  text: label, already: already, skip: skipped, actionable: actionable},
      });
    })()""" % (selector, already, skip, calendar, calendar_loading)


def _piggo_login_script(rule: Mapping[str, Any], username: str, password: str) -> str:
    if not str(username or "").strip() or not str(password or ""):
        return "(() => JSON.stringify({submitted:false,reason:'credentials-missing'}))()"
    user_selector = json.dumps(str(rule.get("browser_login_username_selector")
                                   or "input[name='username']"))
    password_selector = json.dumps(str(rule.get("browser_login_password_selector")
                                        or "input[name='password']"))
    submit_selector = json.dumps(str(rule.get("browser_login_submit_selector") or "button.btn"))
    return """(() => {
      const user = document.querySelector(%s);
      const password = document.querySelector(%s);
      const submit = document.querySelector(%s);
      const visible = (el) => !!el && (el.offsetParent !== null || el.getClientRects().length > 0);
      if (!visible(user) || !visible(password) || !visible(submit)) {
        return JSON.stringify({submitted: false, reason: 'not-login'});
      }
      const setValue = (el, value) => {
        const proto = el instanceof HTMLTextAreaElement
          ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, 'value');
        if (setter && setter.set) setter.set.call(el, value); else el.value = value;
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
      };
      setValue(user, %s);
      setValue(password, %s);
      submit.click();
      return JSON.stringify({submitted: true, reason: 'submitted'});
    })()""" % (user_selector, password_selector, submit_selector,
              json.dumps(username, ensure_ascii=False), json.dumps(password, ensure_ascii=False))


def _piggo_unlock_script(rule: Mapping[str, Any], password: str) -> str:
    if not str(password or ""):
        return "(() => JSON.stringify({submitted:false,reason:'credentials-missing'}))()"
    password_selector = json.dumps(str(rule.get("browser_unlock_password_selector")
                                        or "form.step-up input[name='password']"))
    submit_selector = json.dumps(str(rule.get("browser_unlock_submit_selector")
                                     or "form.step-up button[type='submit']"))
    return """(() => {
      const password = document.querySelector(%s);
      const submit = document.querySelector(%s);
      const visible = (el) => !!el && (el.offsetParent !== null || el.getClientRects().length > 0);
      if (!visible(password) || !visible(submit)) {
        return JSON.stringify({submitted: false, reason: 'not-locked'});
      }
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
      if (setter && setter.set) setter.set.call(password, %s); else password.value = %s;
      password.dispatchEvent(new Event('input', {bubbles: true}));
      password.dispatchEvent(new Event('change', {bubbles: true}));
      submit.click();
      return JSON.stringify({submitted: true, reason: 'submitted'});
    })()""" % (password_selector, submit_selector,
              json.dumps(password, ensure_ascii=False), json.dumps(password, ensure_ascii=False))


def _piggo_click_script(rule: Mapping[str, Any]) -> str:
    selector = json.dumps(str(rule.get("playwright_state_selector") or ".cal-att-pill"))
    already = json.dumps([str(item) for item in (rule.get("playwright_already_texts") or [])
                          if str(item).strip()], ensure_ascii=False)
    skip = json.dumps([str(item) for item in (rule.get("playwright_skip_texts") or [])
                       if str(item).strip()], ensure_ascii=False)
    return """(() => {
      const node = document.querySelector(%s);
      const visible = (el) => !!el && (el.offsetParent !== null || el.getClientRects().length > 0);
      const label = node ? ((node.innerText || node.textContent || node.value || '') + ' '
        + (node.getAttribute('aria-label') || '') + ' ' + (node.getAttribute('title') || ''))
        .replace(/\\s+/g, ' ').trim() : '';
      const disabled = !!node && (!!node.disabled || node.getAttribute('aria-disabled') === 'true');
      const already = %s.some((word) => label.includes(word));
      const skipped = %s.some((word) => label.includes(word));
      if (!node || !visible(node) || disabled || already || skipped || !label.startsWith('签到')) {
        return JSON.stringify({clicked: false, text: label,
          reason: already ? 'already' : skipped ? 'skip' : 'not-actionable'});
      }
      node.setAttribute('data-piggo-signin-target', '1');
      node.scrollIntoView({block: 'center'});
      node.click();
      return JSON.stringify({clicked: true, text: label, reason: 'clicked'});
    })()""" % (selector, already, skip)


def _piggo_inject_script(rule: Mapping[str, Any], token: str) -> str:
    selector = json.dumps(str(rule.get("playwright_state_selector") or ".cal-att-pill"))
    already = json.dumps([str(item) for item in (rule.get("playwright_already_texts") or [])
                          if str(item).strip()], ensure_ascii=False)
    skip = json.dumps([str(item) for item in (rule.get("playwright_skip_texts") or [])
                       if str(item).strip()], ensure_ascii=False)
    return """(() => {
      const token = %s;
      const root = document;
      const inputs = Array.from(root.querySelectorAll("input[name='cf-turnstile-response']"));
      const callbacks = [];
      const addCallback = (name) => { name = (name || '').trim();
        if (name && callbacks.indexOf(name) < 0) callbacks.push(name); };
      root.querySelectorAll('[data-callback]').forEach((node) =>
        addCallback(node.getAttribute('data-callback')));
      ['cfCallback', 'turnstileCallback', 'onTurnstileSuccess', 'onTurnstileVerify',
       'onVerify', 'verifyCallback', 'onCaptchaSuccess', 'captchaCallback'].forEach(addCallback);
      inputs.forEach((input) => {
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
        if (setter && setter.set) setter.set.call(input, token); else input.value = token;
        input.dispatchEvent(new Event('input', {bubbles: true}));
        input.dispatchEvent(new Event('change', {bubbles: true}));
      });
      let callbackCalled = '';
      for (const name of callbacks) {
        if (typeof window[name] !== 'function') continue;
        try { window[name](token); callbackCalled = name; break; } catch (err) {}
      }
      try {
        if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
          window.turnstile.getResponse = () => token;
        }
      } catch (err) {}
      const node = document.querySelector(%s);
      const visible = (el) => !!el && (el.offsetParent !== null || el.getClientRects().length > 0);
      const label = node ? ((node.innerText || node.textContent || node.value || '') + ' '
        + (node.getAttribute('aria-label') || '') + ' ' + (node.getAttribute('title') || ''))
        .replace(/\\s+/g, ' ').trim() : '';
      const disabled = !!node && (!!node.disabled || node.getAttribute('aria-disabled') === 'true');
      const alreadyDone = %s.some((word) => label.includes(word));
      const skipped = %s.some((word) => label.includes(word));
      const actionable = !!node && visible(node) && !disabled && !alreadyDone && !skipped
        && label.startsWith('签到');
      if (actionable) {
        node.setAttribute('data-piggo-signin-target', '1');
        node.scrollIntoView({block: 'center'});
        node.click();
      }
      return JSON.stringify({injected: inputs.length > 0, inputs: inputs.length,
        callback: callbackCalled, clicked: actionable, text: label,
        reason: actionable ? 'clicked' : alreadyDone ? 'already' : skipped ? 'skip' : 'not-actionable'});
    })()""" % (json.dumps(token), selector, already, skip)


def build_piggo_prepare_query(user_agent: Optional[str] = None) -> str:
    set_user_agent = "userAgent(userAgent:$userAgent){time}" if user_agent else ""
    user_agent_variable = " $userAgent:String!" if user_agent else ""
    return """mutation PiggoPrepare($cookies:[CookieInput!]! $loginUrl:String! $calendarUrl:String! $waitLogin:Float! $authWait:Float! $loginScript:String! $unlockScript:String! $stateScript:String! $readySelector:String! $calendarWait:Float! $reconnectTimeout:Float!%s) {
      %s
      cookies(cookies:$cookies){cookies{name}}
      loginPage:goto(url:$loginUrl,waitUntil:domContentLoaded){status}
      waitLogin:waitForTimeout(time:$waitLogin){time}
      login:evaluate(content:$loginScript){value}
      waitAuth:waitForTimeout(time:$authWait){time}
      calendar:goto(url:$calendarUrl,waitUntil:domContentLoaded){status}
      waitCalendar:waitForSelector(selector:$readySelector,timeout:$calendarWait,visible:true){selector time}
      waitCalendarSettle:waitForTimeout(time:1000){time}
      unlock:evaluate(content:$unlockScript){value}
      waitUnlock:waitForTimeout(time:$authWait){time}
      state:evaluate(content:$stateScript){value}
      html:html{html}
      session:reconnect(timeout:$reconnectTimeout){browserQLEndpoint}
    }""" % (user_agent_variable, set_user_agent)


def build_piggo_solve_query(user_agent: Optional[str] = None) -> str:
    set_user_agent = "userAgent(userAgent:$userAgent){time}" if user_agent else ""
    user_agent_variable = " $userAgent:String!" if user_agent else ""
    return """mutation PiggoSolve($calendarUrl:String! $readySelector:String! $calendarWait:Float! $beforeWait:Float! $clickWait:Float! $stateScript:String! $clickScript:String! $solveTimeout:Float! $reconnectTimeout:Float!%s) {
      %s
      calendar:goto(url:$calendarUrl,waitUntil:domContentLoaded){status}
      waitCalendar:waitForSelector(selector:$readySelector,timeout:$calendarWait,visible:true){selector time}
      waitBefore:waitForTimeout(time:$beforeWait){time}
      stateBefore:evaluate(content:$stateScript){value}
      click:evaluate(content:$clickScript){value}
      waitAfterClick:waitForTimeout(time:$clickWait){time}
      solve:solve(timeout:$solveTimeout){found solved time token}
      waitAfterSolve:waitForTimeout(time:2000){time}
      stateAfter:evaluate(content:$stateScript){value}
      html:html{html}
      session:reconnect(timeout:$reconnectTimeout){browserQLEndpoint}
    }""" % (user_agent_variable, set_user_agent)


def build_piggo_submit_query(user_agent: Optional[str] = None) -> str:
    set_user_agent = "userAgent(userAgent:$userAgent){time}" if user_agent else ""
    user_agent_variable = " $userAgent:String!" if user_agent else ""
    return """mutation PiggoSubmit($calendarUrl:String! $readySelector:String! $calendarWait:Float! $beforeWait:Float! $stateScript:String! $injectScript:String! $waitAfter:Float!%s) {
      %s
      calendar:goto(url:$calendarUrl,waitUntil:domContentLoaded){status}
      waitCalendar:waitForSelector(selector:$readySelector,timeout:$calendarWait,visible:true){selector time}
      waitBefore:waitForTimeout(time:$beforeWait){time}
      stateBefore:evaluate(content:$stateScript){value}
      inject:evaluate(content:$injectScript){value}
      waitAfter:waitForTimeout(time:$waitAfter){time}
      stateAfter:evaluate(content:$stateScript){value}
      html:html{html}
    }""" % (user_agent_variable, set_user_agent)


def _piggo_step_hint(data: Mapping[str, Any]) -> str:
    """把 BrowserQL 失败步骤翻成固定提示，不转发可能含脚本变量的原始错误。"""
    labels: List[str] = []
    aliases = {
        "waitcalendar": "等待签到日历加载失败",
        "waitlogin": "等待登录页加载失败",
        "loginpage": "打开登录页失败",
        "login": "登录操作失败",
        "calendar": "打开签到页失败",
        "unlock": "空闲解锁失败",
        "click": "点击签到按钮失败",
        "solve": "Turnstile 求解失败",
        "inject": "Turnstile token 注入失败",
    }
    for item in data.get("_errors") or []:
        if not isinstance(item, Mapping):
            continue
        path = str(item.get("path") or "").strip().lower()
        label = next((value for key, value in aliases.items() if key in path), "")
        if label and label not in labels:
            labels.append(label)
    return "；".join(labels)
