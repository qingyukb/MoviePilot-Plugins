# MoviePilot-Plugins

MoviePilot 第三方插件仓库。

## PT站点签到

PT 站点自动签到，V2 / V3 都能装。

主要功能：

- **签到**：HTTP 直连签到接口，大部分 NexusPHP 站开箱能用
- **过盾**：撞上验证码或 Cloudflare 时自动改用 Browserless 浏览器签到
- **登录**：用「站点管理」里的 Cookie 签到，也可以填账号密码自动登录换新 Cookie
- **动向**：签完读一下站点的「最近动向」，写在登录状态里
- **省事**：今天签过的站自动跳过，通知只发一行

站点多、Cookie 老是要换的话，配个 Browserless 就省事了。

### Browserless Token 怎么拿

只有需要过验证码 / Cloudflare 的站才用到它，普通签到可以跳过这一节。

1. 打开 <https://browserless.io/signup/email?plan=free> 注册免费账号，邮箱填进去就行；
2. 登录后打开 <https://browserless.io/account/>，页面上 **API Key** 那一栏点 **Copy** 复制；
3. 想多把 Key 就点 **Manage API keys** → **Create API key**，起个名字生成（新 Key 只显示一次，当场复制）；
4. 打开插件设置，把 Key 贴到 **Browserless Token**，地址填 `https://production-sfo.browserless.io`；
5. 保存后跑一轮，日志里出现 `Browserless` 字样就说明通了。

几点提醒：

- 地址也能换 `production-ams` / `production-lon`，我这边的网络实测 sfo 最快（一次请求约 0.7 秒）；
- 免费账号有额度，只有走浏览器的站才消耗，日常签到用不完；
- Key 等同密码，别发到公开地方，插件日志里对它打码。

## 安装

**插件市场**：在 MoviePilot 的「插件市场」里加上本仓库地址，刷新市场后搜索插件名。

```
https://github.com/qingyukb/MoviePilot-Plugins
```

**手动**：把插件目录复制到 MoviePilot 的 `/config/plugins/` 下，重启。

```bash
git clone --depth 1 https://github.com/qingyukb/MoviePilot-Plugins.git /tmp/mp-plugins
cp -r /tmp/mp-plugins/plugins.v2/ptsitesignin /config/plugins/   # V2
cp -r /tmp/mp-plugins/plugins.v3/ptsitesignin /config/plugins/   # V3
```

## 目录

```
.
├── package.json               # 插件市场清单
├── package.v2.json            # V2 市场清单
├── package.v3.json            # V3 市场清单
├── icons/                     # 市场图标
├── plugins.v2/ptsitesignin/   # 插件源码（V2）
├── plugins.v3/ptsitesignin/   # 插件源码（V3，和 V2 一致）
└── tests/v3/ptsitesignin/     # 测试
```

配置项、内置站点、自定义规则和常见问题都在 [插件 README](plugins.v2/ptsitesignin/README.md)。

## 加站点规则

内置预设没覆盖的站，浏览器登录后打开签到页，F12 控制台执行 `plugins.v2/ptsitesignin/tools/collect-rule-snippet.js`，把输出填到插件的「自定义站点规则 JSON」里。
