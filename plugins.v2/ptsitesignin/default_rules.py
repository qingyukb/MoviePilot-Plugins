"""PT 签到内置站点规则。

规则只描述站点差异；HTTP / Playwright / Browserless 执行逻辑仍在 core.py。
单独维护可避免规则表持续挤占核心模块，并保留 ``core.DEFAULT_RULES`` 兼容入口。
"""

from __future__ import annotations

from typing import Any, Dict


DEFAULT_RULES: Dict[str, Dict[str, Any]] = {
    # ---- 以下为需要浏览器交互的站点预设（来自 captchasignin 参考实现）----
    # ---- 不可躺 / PlayLet：NexusPHP 签到页 ----
    # 不可躺使用页内 Turnstile；PlayLet 使用图片验证码。
    # 两者都通过 Browserless 在真实页面中完成验证后提交 attendance.php。
    "tangpt.top": {
        "engine": "browserless",
        "mode": "cloudflare",
        "path": "/attendance.php",
        "preflight_path": "/attendance.php",
        "submit_method": "auto",
        "auto_wait": 30000,
        "cloudflare_wait": 15,
        "solve_count": 1,
        "solve_timeout": 40000,
        "wait": 15000,
        "verify_success_path": "/attendance.php",
        "verify_wait": 15000,
        "success_keywords": ["签到成功", "簽到成功", "本次签到获得", "本次簽到獲得"],
        "already_keywords": ["今天已经签到", "今日已签到", "已经签到", "今天已签到",
                             "今日已簽到", "已經簽到"],
        "pending_keywords": ["验证图片", "驗證圖片", "立即签到", "立即簽到"],
    },
    "playlet.cc": {
        "engine": "browserless",
        "mode": "image",
        "path": "/attendance.php",
        "preflight_path": "/attendance.php",
        "captcha_selector": "form:has(input[name='imagestring']) img[alt='CAPTCHA']",
        "captcha_input_selector": "form input[name='imagestring']",
        "submit_selector": "form input[type='submit'][value='立即签到']",
        "submit_method": "click",
        "wait": 8000,
        "verify_success_path": "/attendance.php",
        "verify_wait": 8000,
        "success_keywords": ["签到成功", "簽到成功", "本次签到获得", "本次簽到獲得"],
        "already_keywords": ["今天已经签到", "今日已签到", "已经签到", "今天已签到",
                             "今日已簽到", "已經簽到"],
    },
    "open.cd": {
        # 皇后：plugin_sign-in.php 的图片验证码表单，交给 Browserless 图片模式解。
        # 签到成功后页面会出现「[查看簽到記錄]」，验证码表单消失，按这个判成功。
        "engine": "browserless",
        "mode": "image",
        "path": "/plugin_sign-in.php",
        "preflight_path": "/",
        "verify_success_path": "/",
        # 最近动向在个人页；没有 id 会显示「选择用户」，read_activity 会用 Cookie 的
        # c_secure_uid 拼 ?id= 打开自己的资料页
        "activity_path": "/userdetails.php",
        "captcha_selector": "#frmSignin img",
        "captcha_input_selector": "#imagestring",
        "submit_selector": "#ok",
        "submit_method": "ajax",
        "success_json": {"field": "/state", "values": ["success"]},
        "success_keywords": ["查看簽到記錄", "簽到成功", "签到成功"],
        "already_keywords": ["查看簽到記錄", "已簽到", "已签到", "今日已签", "今日已簽"],
        "strict_status_keywords": True,
    },
    "p.t-baozi.cc": {
        "engine": "browserless",
        "mode": "image",
        "path": "/attendance.php",
        "captcha_selector": "form:has(input[name='imagestring']) img",
        "captcha_input_selector": "input[name='imagestring']",
        "submit_selector": "input[type='submit']",
        "already_keywords": ["签到成功"],
    },
    "pt.muxuege.org": {
        "engine": "browserless",
        "mode": "open_page",
        "path": "/attendance.php",
        "submit_selector": "input[type='submit'], button[type='submit']",
        "submit_method": "dom_click",
        "submit_text": "立即签到",
    },
    "oshen.win": {
        "engine": "browserless",
        "mode": "image",
        "path": "/attendance.php",
        "captcha_selector": "form:has(input[name='imagestring']) img[alt='CAPTCHA']",
        "captcha_input_selector": "form input[name='imagestring']",
        "submit_selector": "form input[type='submit'][value='立即签到']",
        "submit_method": "ajax",
    },
    "ptsbao.club": {
        # 烧包：attendance.php 的图片验证码签到（和 oshen.win 同款 NexusPHP 表单）
        "engine": "browserless",
        "mode": "image",
        "path": "/attendance.php",
        "captcha_selector": "form:has(input[name='imagestring']) img[alt='CAPTCHA']",
        "captcha_input_selector": "form input[name='imagestring']",
        "submit_selector": "form input[type='submit'][value='立即签到']",
        "submit_method": "ajax",
        "success_keywords": ["签到成功", "簽到成功", "本次获得", "本次獲得"],
        "already_keywords": ["已经签到", "已經簽到", "已签到", "已簽到", "今日已签到"],
    },
    "pt.luckpt.de": {
        # 只做日常签到：/attendance.php。之前走 /medal_collection.php 领「纪念套装」的
        # 幸运星，勋章凑齐后按钮消失，反而把签到判成失败。若站点首页就是签到页，
        # fallback_rule / discover 会再兜底。
        "engine": "browserless",
        "mode": "open_page",
        "path": "/attendance.php",
        "submit_selector": "input[type='submit'], button[type='submit'], a[href*='attendance']",
        "submit_method": "dom_click",
        "submit_text": "签到",
        "success_keywords": ["签到成功", "已获得", "本次获得"],
        "already_keywords": ["已签到", "今天已签到", "今日已签到"],
    },
    "yemapt.org": {
        "engine": "browserless",
        "mode": "altcha",
        "path": "/",
        "route_fragment": "#/user/growth?tab=checkIn",
        "altcha_checkbox_selector": "altcha-widget input[type='checkbox']",
        "altcha_payload_selector": "altcha-widget .altcha[data-state='verified'] input[name='altchaPayload']",
        "submit_selector": "button.ant-btn-primary.ant-btn-lg.ant-btn-block:not([disabled])",
        "submit_method": "altcha_click",
        "submit_text": "立即签到",
        "already_keywords": ["已签到，明日继续", "你的今日状态：已签到"],
        "preflight_wait": 8000,
        "wait": 5000,
    },
    "hdsky.me": {
        "engine": "browserless",
        "mode": "trigger_image",
        "path": "/index.php",
        "trigger_selector": "#showup",
        "captcha_selector": "#showupimg",
        "captcha_input_selector": "#imagestring",
        "submit_selector": "#showupbutton",
        "submit_method": "dom_click",
        "submit_text": "Let's Go",
        "success_keywords": ["已签到"],
    },
    "dstudio.me": {"engine": "browserless", "mode": "cloudflare", "path": "/attendance.php"},
    # ---- AGSVPT（agsvpt.com）：NexusPHP 签到页，页面上是一个「立即签到」表单提交按钮 ----
    # GET /attendance.php 只会把按钮渲染出来，不点按钮不会签到（实测未登录时 302 到
    # login.php?returnto=attendance.php），所以 HTTP 引擎必须用 POST；万一站点开了验证码 /
    # 撞上盾，自动回退到浏览器引擎时会按下面的选择器与按钮文字点「立即签到」。
    # 判定文案沿用默认正则（签到成功 / 已经签到），不额外收窄。
    "agsvpt.com": {
        "engine": "http",
        "method": "POST",
        "path": "/attendance.php",
        "submit_selector": "input[type='submit'], button[type='submit'], button",
        "submit_method": "dom_click",
        "submit_text": "立即签到",
    },
    "mua.xloli.cc": {
        "engine": "browserless",
        "mode": "cloudflare",
        "path": "/attendance.php",
        "submit_selector": "form[action*='attendance.php'] input[type='submit']",
    },
    "share.ilolicon.com": {
        "engine": "browserless",
        "mode": "cloudflare",
        "path": "/attendance.php",
        "submit_selector": "form[action*='attendance.php'] input[type='submit']",
    },
    # ---- TTG（totheglory.im）：没有 attendance.php，签到入口在站点页面上 ----
    # 探测：/attendance.php、/signin.php、/checkin.php 都是 404，站点在 Cloudflare 后面，
    # 所以走浏览器打开首页点「签到」。若你那边入口在别的页面，改 path 即可。
    "totheglory.im": {
        "engine": "http",
        "method": "GET",
        # 和 thsrite 的站点模块同一套流程：首页有绿色「已签到」就是今天签过了；
        # 否则从首页 JS 里取 signed_timestamp / signed_token，POST 到 /signed.php。
        "path": "/index.php",
        "already": "<b[^>]*>\\s*已签到\\s*</b>",
        "already_keywords": ["今天已签到过", "不要太贪", "已連續簽到", "您已連續簽到"],
        "signin_path": "/signed.php",
        "signin_method": "POST",
        "signin_token_patterns": {
            "signed_timestamp": "signed_timestamp:\\s*\"(\\d{10})\"",
            "signed_token": "signed_token:\\s*\"([^\"]+)\"",
        },
        "success_keywords": ["您已连续签到", "已連續簽到", "签到成功"],
    },
    # ---- HDKylin（hdkyl.in）：NexusPHP，签到在 /attendance.php ----
    # 站点在 Cloudflare 后面（访客请求直接 468），HTTP 直连多半要挑战，默认走浏览器。
    "hdkyl.in": {
        "engine": "browserless",
        "mode": "open_page",
        # 签到入口是首页的「签到」链接，点完会跳雷池 WAF 检测页，等放行后再读结果
        "path": "/index.php",
        "submit_selector": "a, input[type='submit'], button, [role='button'], span",
        "submit_method": "dom_click",
        "submit_text": "签到",
        "wait": 12000,
        "pending_keywords": ["正在进行安全检测", "安全检测"],
        "success_keywords": ["签到成功", "簽到成功", "签到完成", "已签到", "本次签到"],
        "already_keywords": ["今日已签到", "今天已签到", "已签到", "已簽到", "已领取"],
    },
    # ---- 以下站点流程参考 thsrite「站点自动签到」的 sites/ 模块 ----
    # 只收录能用规则表达的：POST 固定字段、首页抓 CSRF、JSON 返回判定这几种。
    # ---- hdarea（hdarea.club）：POST sign_in.php，返回页带「此次签到您获得」 ----
    "hdarea.club": {
        "engine": "http",
        "method": "POST",
        "path": "/sign_in.php",
        "data": {"action": "sign_in"},
        "success_keywords": ["此次签到您获得"],
        "already_keywords": ["请不要重复签到哦"],
    },
    # ---- NexusHD（v6.nexushd.org）：POST signin.php ----
    "nexushd.org": {
        "engine": "http",
        "method": "POST",
        "path": "/signin.php",
        "data": {"action": "post", "content": ""},
        "success_keywords": ["本次签到获得"],
        "already_keywords": ["你今天已经签到过了"],
    },
    # ---- HDCTV（hdcity.city）：签到页 /sign 直接给结果 ----
    "hdcity.city": {
        "engine": "http",
        "method": "GET",
        "path": "/sign",
        "success_keywords": ["本次签到获得魅力"],
        "already_keywords": ["已签到"],
    },
    # ---- HDUPT（pt.hdupt.com）：GET added.php?action=qiandao ----
    "hdupt.com": {
        "engine": "http",
        "method": "GET",
        "path": "/added.php?action=qiandao",
        "success_keywords": ["本次签到获得魅力"],
        # 已签到时首页会把这个 span 打出来
        "already": "<span[^>]*id=\"yiqiandao\"",
    },
    # ---- PTT（pttime.org）：attendance.php 直接签 ----
    "pttime.org": {
        "engine": "http",
        "method": "GET",
        "path": "/attendance.php",
        "success_keywords": ["签到成功"],
        "already_keywords": ["今日已签到", "今天已签到", "已签到"],
    },
    # ---- Hares（club.hares.top）：attendance.php?action=sign 返回 JSON ----
    "hares.top": {
        "engine": "http",
        "method": "GET",
        "path": "/attendance.php?action=sign",
        "success_keywords": ["签到成功", "\"success\":true"],
        "already_keywords": ["已签到", "已经签到"],
    },
    # ---- PterClub（pterclub.com）：attendance-ajax.php 返回 JSON，status=1 即成功 ----
    "pterclub.com": {
        "engine": "http",
        "method": "GET",
        "path": "/attendance-ajax.php",
        "success_keywords": ["\"status\":\"1\"", "签到已成功", "本次签到获得"],
        "already_keywords": ["您今天已经签到过了", "请勿重复刷新"],
    },
    # ---- 海胆（haidan.video）：signin.php 触发签到，回首页看按钮是否变「已经打卡」 ----
    "haidan.video": {
        "engine": "http",
        "method": "GET",
        # 签到后首页按钮会变成「已经打卡」，所以先读首页判断，再去 signin.php，
        # 拿不到结论时插件会自动回读首页确认（日志里「未匹配到签到成功标志」就是这么来的）
        "path": "/index.php",
        "already_keywords": ["已经打卡", "已打卡"],
        # 站点首页那个 id=modalBtn 的按钮点下去是 jQuery AJAX 打 signin.php，
        # 带 X-Requested-With 才认；不带会被跳到登录页（之前一直误报失败的原因）
        "signin_path": "/signin.php",
        "signin_method": "GET",
        "signin_headers": {"X-Requested-With": "XMLHttpRequest"},
        "success_keywords": ["已经打卡", "已打卡", "打卡成功", "签到成功"],
    },
    # ---- HDChina（hdchina.org）：首页取 x-csrf，POST plugin_sign-in.php?cmd=signin ----
    "hdchina.org": {
        "engine": "http",
        "method": "GET",
        "path": "/index.php",
        "already": "<a[^>]*class=\"label label-default\"[^>]*>已签到</a>",
        "already_html": (
            r'<a\b(?=[^>]*\bclass=["\'][^"\']*\blabel\b[^"\']*\blabel-default[^"\']*["\'])'
            r'[^>]*>\s*(?:已签到|已簽到)\s*</a>'
        ),
        "signin_path": "/plugin_sign-in.php?cmd=signin",
        "signin_method": "POST",
        "signin_token_patterns": {"csrf": "<meta name=\"x-csrf\" content=\"([^\"]+)\""},
        "success_keywords": ["\"state\":\"success\"", "\"state\": \"success\""],
    },
    # ---- 好学（hxpt.org）：Next.js API 签到；具体 CSRF 流程见 sites/hxpt.py ----
    "hxpt.org": {
        "engine": "http",
        "method": "GET",
        "path": "/api/v1/home",
        "already_keywords": ["\"checked_today\":true", "\"checked_today\": true",
                             "今日已签到"],
        "strict_status_keywords": True,
    },
    # ---- 52PT（52pt.site）：首页点击「做种会员签到处」，完成 Turnstile 后提交 ----
    # 2026-11-07 实测：未签到首页链接为 /bakatestfinal.php；签到页按钮文案是
    # 「验证已通过，点此提交」；提交成功后仍停留在该页并显示「连续签到 N 天，获得 X 魔力值」。
    # 签到后首页链接改为 /nebuirobot.php（该地址 404），因此用这个链接差异判断今日已签到，
    # 不能再次点击已签到入口，也不能直接打开 /bakatestfinal.php（站点会判定为绕过入口）。
    "52pt.site": {
        "engine": "browserless",
        "mode": "cloudflare",
        "path": "/index.php",
        "preflight_path": "/index.php",
        "preflight_wait": 4000,
        "activity_path": "/userdetails.php",
        "pre_click_selector": "a#game[href*='bakatestfinal']",
        "pre_click_wait": 4000,
        # solve 只生成 token，不会触发站点回调；auto 会在第二段重新点入口、回填 token，
        # 调用 onTurnstileSuccess，并按表单实际 action 提交。
        "submit_method": "auto",
        "auto_wait": 30000,
        "cloudflare_wait": 15,
        "solve_timeout": 45000,
        "wait": 8000,
        "pending_keywords": ["请先完成人机验证", "等待上方验证通过"],
        "already_keywords": ["今天已经签过到了", "今日已签到", "今天已经签到", "今天已签到"],
        "already_html": (
            r'<a\b(?=[^>]*\bhref=["\'](?:https?://52pt\.site)?/?nebuirobot\.php)'
            r'[^>]*>.*?做种会员签到处.*?</a>'
        ),
        "success": r"连续签到\s*\d+\s*天，获得\s*[\d,]+\s*魔力值",
        "success_keywords": ["已自动加入本期"],
        # 首页正文里可能夹杂英文 announcement 的 already；只认本规则明确的状态文案。
        "strict_status_keywords": True,
    },
    # ---- 彩虹岛（CHDBits）：bakatest 选择题签到 ----
    # 未配置答案时沿用官方 autosignin 的随机策略；固定题可用 quiz_answer 指定
    # choice[] 值，多题可用 quiz_answers 按题目文本映射。提交后必须回读签到页，
    # 看到「今天已经签过到了」才算真正完成，页面顶部的魔力值不能当作成功标志。
    "ptchdbits.co": {
        "engine": "http",
        "form_mode": "quiz",
        "path": "/bakatest.php",
        "verify_success_path": "/bakatest.php",
        "success": r"\d+\s*点魔力值",
        "already": r"今天已经签过到了?",
        "quiz_failure": r"(回答错误|答案错误|请重新|不正确|失去)",
        "quiz_fields": {"usercomment": "太难了！", "wantskip": "不会"},
    },
    # ---- U2（u2.dmhy.org）：showup 四选一，提交后必须回读确认 ----
    # 独立模块负责精确解析 req/hash/form/_csrf 和四个答案按钮；这里保留 HTTP
    # 兜底规则，但不把 JS 跳转当作成功，避免答错或未真正签到时误报。
    "u2.dmhy.org": {
        "engine": "http",
        "method": "GET",
        "path": "/showup.php",
        "already": r"(?:已签到|已簽到|今日已签到|今日已簽到)",
        "signin_path": "/showup.php?action=show",
        "signin_method": "POST",
        "signin_pick": "random",
        "signin_data": {"message": "一切随缘~"},
        "already_keywords": ["已签到", "已簽到"],
        "strict_status_keywords": True,
        "verify_success_path": "/showup.php",
    },
    # ---- 朱雀（zhuque.in）：首页取 x-csrf-token，POST JSON 释放技能（就是它的签到）----
    "zhuque.in": {
        "engine": "http",
        "method": "GET",
        "path": "/",
        "signin_path": "/api/gaming/fireGenshinCharacterMagic",
        "signin_method": "POST",
        "signin_token_patterns": {"csrf": "<meta name=\"x-csrf-token\" content=\"([^\"]+)\""},
        "signin_headers": {"x-csrf-token": "{csrf}",
                           "Content-Type": "application/json; charset=utf-8"},
        "signin_json_raw": "{\"all\": 1, \"resetModal\": \"true\"}",
        "success_keywords": ["\"status\":200", "FIRE_GENSHIN_CHARACTER_MAGIC_SUCCESS"],
    },
    # ---- 高清杜比（hddolby.com，2026-10 起换到 uhddolby.com）：NexusPHP 签到页，
    # 已签到时首页显示「鲸币 (签到已得N)」。新域名通过 DOMAIN_ALIASES 指到这条规则。 ----
    "hddolby.com": {
        "engine": "http",
        "method": "GET",
        "path": "/attendance.php",
        "success_keywords": ["签到成功", "签到完成", "领取成功"],
        "already_keywords": ["签到已得", "今日已签到", "已经签到", "请不要重复签到"],
    },
    # ---- SUNNYPT（sunnypt.top）：站点自带签到页 /user/attendance（Next.js + JSON API） ----
    # 头像菜单里的「每日签到」只是跳这个页面，不用再点菜单；
    # 已签到时页面上是「今日签到已完成 … 已连续签到 N 天」，按钮变灰的「今日已签到」。
    "sunnypt.top": {
        # 本机浏览器打开签到页：与手动操作同一 IP，避免站点侧异常校验
        "engine": "playwright",
        "method": "GET",
        "path": "/user/attendance",
        "submit_method": "dom_click",
        "submit_selector": "button, [role='button'], a",
        # 未签到时按钮是「立即签到」，已签到时是灰的「今日已签到」——用完整的四个字更精准
        "submit_text": "立即签到",
        # Cookie 失效时站点会 302 到 /auth/sign-in?shouldClearAuth=true：
        # core 的 login_page() 已能识别，会直接报「Cookie 可能失效」而不是误报签到失败
        # 站点是 Next.js + WebSocket 推状态：点完页面要等接口/推送回来才变成「今日签到已完成」，
        # 所以多留一点时间再读快照（8 秒）
        "wait": 8000,
        # 不能用「点到就算成功」：已签到时按钮是灰的「今日已签到」，照样能被点到，
        # 于是天天误报签到成功。这里改成看页面状态：
        # 点之前就有「今日已签到」= 已签到（插件会先探一次，不会瞎点）；
        # 点之后出现「今日签到已完成 / 已连续签到 N 天」= 本次签到成功。
        "success_keywords": ["本次获得", "签到成功"],
        "already_keywords": ["今日已签到", "今日签到已完成", "已连续签到", "已签到"],
    },
    # ---- 海棠PT（htpt.cc）：/attendance.php 的「每日签到」日历，点按钮签到 ----
    # 已签到时页面直接写「今日已签到 ✓（连续 N 天，今日获得 X 魔力值）」，没有表单；
    # 未签到时是按钮，按文字点。最近动向在 userdetails.php（id 从 Cookie 的 c_secure_uid 取）。
    # ---- 自由农场（0ff.cc / pt.0ff.cc）：滑块接口直连，签到逻辑在 sites/farm.py ----
    "0ff.cc": {
        "engine": "http",
        "method": "GET",
        "path": "/attendance.php",
        "already_keywords": ["今日已签到", "已签到"],
        "success_keywords": ["签到成功", "已簽到"],
    },
    "pt.0ff.cc": {
        "engine": "http",
        "method": "GET",
        "path": "/attendance.php",
        "already_keywords": ["今日已签到", "已签到"],
        "success_keywords": ["签到成功", "已簽到"],
    },
    "htpt.cc": {
        # 签到交给 sites/htpt.py：GET 签到页 → POST action=sign → 回读确认。
        # 实测这条路直接可用（表单无验证码），不必再开浏览器。
        "engine": "http",
        "method": "GET",
        "path": "/attendance.php",
        "activity_path": "/userdetails.php",
        "already_keywords": ["今日已签到", "今日已经签到", "今天已签到"],
        "success_keywords": ["签到成功", "簽到成功"],
        "bonus": r"(今日获得\s*[\d,]+\s*魔力值)",
    },
    # ---- 憨憨（hhanclub.net）：日历式签到页，点「签到」后当天按钮变成「已领取」 ----
    # 页面是 Vue 渲染的日历，签到要点击完成；判定不认日历里过去几天的「已领取」，
    # 只认签到后的提示语（签到成功 / 领取成功），奖励取「本次签到获得 N 个憨豆」。
    "hhanclub.net": {
        "engine": "browserless",
        "mode": "open_page",
        "path": "/attendance.php",
        "submit_selector": "button, a[role='button'], div[role='button'], input[type='submit']",
        "submit_method": "dom_click",
        "submit_text": "签到",
        "success_keywords": ["签到成功", "簽到成功", "领取成功", "簽到完成", "签到完成"],
        # 这站点完没有任何提示，按钮直接变「已领取」：点到了就算成功
        "success_on_click": True,
        # 点完按钮才做判定，页面上「本次签到获得…」就代表今天已经领到了
        "already_keywords": ["今日已签到", "今日已经签到", "今天已签到", "今日已領取",
                              "本次签到获得", "已连续签到"],
        "bonus": r"(本次签到获得\s*[\d,]+\s*个?憨豆)",
    },
    # ---- BTSCHOOL（btschool.club，学校）：签到按钮直接挂在首页，没有独立签到页 ----
    # 实测 /attendance.php、/signin.php、/checkin.php 等全是 404，首页上是一个「每日签到」
    # 提交按钮，所以直接 POST 首页即可；判定沿用默认正则（今天已签到 / 签到成功），
    # 「您获得 N 点魔力值」这类奖励是每天不同的，由 extract_bonus 每次从页面文本抓取，不写死。
    "btschool.club": {
        "engine": "http",
        "method": "GET",
        # 和 thsrite 的站点模块同一套判断：
        # 首页没有「每日签到」入口 = 今天已签；有就访问 addbonus，入口消失即签到成功。
        "path": "/index.php",
        "absent_keywords": ["每日签到"],
        "absent_status": "already",
        "signin_path": "/index.php?action=addbonus",
        "signin_absent_status": "success",
        "success_keywords": ["签到成功", "领取成功"],
    },
    # ---- RousiPro（rousi.pro）：PeerGo 个人 API Key 签到 ----
    # 站点使用 api-token 请求头；POST JSON {"mode":"fixed"}。已签到时返回 HTTP 400
    # + code=1，message 含「已签到」，由 http_sign 单独识别为 already。
    "rousi.pro": {
        "auth": "api_key",
        "engine": "http",
        "method": "POST",
        "path": "/api/points/attendance",
        "json": {"mode": "fixed"},
        "api_key_source": "apikey",
        "api_key_header": "api-token",
        "api_key_prefix": "",
        "success": r'"code"\s*:\s*0\b',
        "already": r"(已经签到|已签到|重复签到|今日已经|今天已经)",
        "credential_check_path": "/api/v1/profile",
        "credential_check_method": "GET",
        "credential_check_success": r'"code"\s*:\s*0\b',
    },
    # ---- Piggo（piggo.me）：SPA 日历页本机 Playwright 签到 ----
    # 签到入口固定为 /calendar。点击签到按钮后会进入 Cloudflare 人机验证，
    # 验证通过后页面自行提交。使用 MoviePilot 所在网络的本机浏览器，避免
    # 云端 Browserless 的机房 IP 触发异地登录验证；失败时不回退 Browserless。
    "piggo.me": {
        "engine": "playwright",
        "cookie_mode": "browser",
        "path": "/calendar",
        "submit_method": "dom_click",
        "submit_selector": ".cal-att-pill.act",
        "submit_text": "签到",
        "playwright_state_selector": ".cal-att-pill",
        "playwright_already_texts": ["已签到", "今日已签到", "今天已签到", "已簽到"],
        "playwright_skip_texts": ["补签", "補簽"],
        "captcha_response_path": "/v1/web/calendar/attendance",
        "playwright_captcha_token_selector": "input[name='cf-turnstile-response']",
        "playwright_captcha_wait": 30000,
        "playwright_wait_for_ready": True,
        "playwright_ready_wait": 45000,
        "playwright_calendar_selector": ".cal-wrap",
        "playwright_calendar_loading_selector": ".cal-wrap[aria-busy='true']",
        "playwright_page_ready_selector": (
            ".cal-att-pill, .cal-wrap:not([aria-busy='true']), "
            "input[name='username'], form.step-up input[name='password'], "
            ".denied-page, #idle-lock-title"
        ),
        "browser_auth_mode": "login_and_unlock",
        "browser_login_url": "/login?redirect=/calendar",
        "browser_login_username_selector": "input[name='username']",
        "browser_login_password_selector": "input[name='password']",
        "browser_login_submit_selector": "button.btn",
        "browser_unlock_password_selector": "form.step-up input[name='password']",
        "browser_unlock_submit_selector": "form.step-up button[type='submit']",
        "cloudflare_wait": 60,
        "wait": 8000,
        "auto_wait": 30000,
        # Piggo 的个人页由 SPA 提供，直接 HTTP 访问会被 Cloudflare/风控拦成 403。
        # 用户资料页的 /userdetails 会自动跳转到 /userdetails/<自己的 uid>，
        # 因此这里只需要给出稳定入口，再由本机 Playwright 登录后读取「最近动向」。
        "activity_path": "/userdetails",
        "activity_engine": "playwright",
        "activity_wait": 8000,
    },
    # ---- 观众（Audiences）：签到页内嵌 Turnstile，验证通过后由站点自行提交 ----
    # 该站 Turnstile 渲染在 closed shadow DOM 里（本地 Edge 复现），页面 JS 读不到
    # cf-turnstile-response，DOM 上也没有可调的 data-callback；而 Browserless 的 solve 只
    # 负责生成 token（写在响应的 token 字段里），不会触发站点挂在自己 widget 上的提交逻辑。
    # 所以 submit_method=auto 走两段式：
    #   第一段：打开签到页 + solve 拿 token + reconnect 留住同一个浏览器会话；
    #   第二段：在同一会话里把 token 回填页面、试着调站点回调，必要时按表单真实 action 兜底
    #          POST（上游 NexusPHP 的契约就是 POST /attendance.php 带 cf-turnstile-response），
    #          最后在同一个会话里重新打开 attendance.php 核验真实状态。
    "audiences.me": {
        "engine": "browserless",
        "mode": "cloudflare",
        "path": "/attendance.php",
        "submit_method": "auto",
        "auto_wait": 30000,
        "cloudflare_wait": 15,
        "solve_count": 1,
        "solve_timeout": 40000,
        "wait": 15000,
        # 签到成功后页面可能跳回首页；同一浏览器会话重新打开 attendance.php 再确认一次，
        # 避免只看到首页就误报失败，也避免把未完成的验证当成成功。
        "verify_success_path": "/attendance.php",
        "verify_wait": 15000,
        # 「已领取 / 已获得」不能作为成功标志：签到后刷新页面仍会显示奖励，
        # 很容易把已签到状态误判成刚刚签到。
        "success_keywords": ["签到成功", "簽到成功", "成功签到", "签到完成", "领取成功"],
        "already_keywords": ["今日已签到", "今天已经签到", "您今天已经签到", "已经签到",
                             "已签到", "今日已领取", "已连续签到", "明日再来", "明天再来"],
        "pending_keywords": ["验证通过后将自动完成签到", "完成人机验证即可领取今日爆米花奖励"],
    },
    # ---- 未内置的站点默认走 HTTP 直连（无需浏览器交互）----
    # GET 打开签到页即完成签到，判定文案取自站点实际页面：
    #   签到成功 -> 「签到成功」；已签到 -> 「您今天已经签到过了」
    # already 只保留上述「已签到」原文案：签到成功页往往同时带「已签到 N 天」「您已连续
    # 签到 N 天」之类的状态行，一旦把裸「已签到」写进 already，新签到就会被误判成
    # 「今日已签到」。
    # 页面内嵌 Turnstile 的站点 HTTP 拿不到结果，会自动回退到 Browserless（见 judge_http）。
}
