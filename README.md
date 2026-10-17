# MoviePilot-Plugins

放自己用的 MoviePilot 插件。

## PT站点签到

PT 站自动签到。装好勾上站点就能跑，V2、V3 都支持。

- 签到走 HTTP 直连，大部分 NexusPHP 站不用额外配
- 有验证码或 Cloudflare 的站，填个 Browserless 就自动拿浏览器签
- Cookie 用 MoviePilot 站点管理里那份，插件里不再存一遍；想让它自动换 Cookie，把账号密码写在该站的附加信息里
- 签完顺手读一下站点的「最近动向」，显示在登录状态里
- 今天签过的站不会再跑，通知就一行

## Browserless Token

只有需要过验证码 / 过盾的站才用它，用不上就跳过这节。

去 https://browserless.io/signup/email?plan=free 注册个免费号，邮箱填进去就行。注册完打开 https://browserless.io/account/ ，页面上有个 API Key，点旁边的 Copy 复制下来。想多要几个 Key 就点 Manage API keys，再点 Create API key 起个名字，生成后只显示一次，记得当场复制。

复制到的 Key 贴到插件设置里的 Browserless Token，地址留默认的 https://production-sfo.browserless.io 就行。也可以换 production-ams 或 production-lon，不过我这测下来 sfo 最快，一次请求 0.7 秒左右。

免费额度按月算，只有用到浏览器的站才扣，平常签到够用。Key 别外传，插件日志里是打码的。

## 安装

在 MoviePilot 的「插件市场」里加上这个地址，刷新市场后搜插件名：

```
https://github.com/qingyukb/MoviePilot-Plugins
```

也可以手动放：把插件目录丢到 `/config/plugins/` 下重启。

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
├── plugins.v3/ptsitesignin/   # 插件源码（V3，和 V2 一样）
└── tests/v3/ptsitesignin/     # 测试
```

配置项、内置了哪些站、自定义规则、常见问题都写在 [插件 README](plugins.v2/ptsitesignin/README.md) 里。

## 加站点规则

内置预设没覆盖的站，先浏览器登录，打开签到页，F12 控制台里跑 `plugins.v2/ptsitesignin/tools/collect-rule-snippet.js`，把输出填进插件的「自定义站点规则 JSON」。
