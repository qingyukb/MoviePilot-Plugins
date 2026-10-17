# MoviePilot-Plugins

个人维护的 [MoviePilot](https://github.com/jxxghp/MoviePilot) 第三方插件仓库。

## 插件

### PT站点签到

自动签到 PT 站点。HTTP 直连签到，遇到验证码 / Cloudflare 自动改用浏览器；可真实登录换 Cookie，签完顺手读一下站点的「最近动向」。V2、V3 都能用。

- 凭证直接用 MoviePilot「站点管理」里的 Cookie，不额外存账号密码；要自动登录就把账号密码写在该站「附加信息」里（`{"username":"...","password":"..."}`）
- 常用站点已内置预设（观众、OpenCD、学校、憨憨、AGSVPT、LuckPT、HDSky 等），按域名自动匹配
- 今天已签到的站默认跳过；想重签就打开一次「清理本日缓存」
- 通知就一行：`签到成功 N｜登录成功 N｜失败 N`，有失败才列出「站点：原因」

配置文件、自定义规则和常见问题看 [插件 README](plugins.v2/ptsitesignin/README.md)。代码在 [`plugins.v2/ptsitesignin`](plugins.v2/ptsitesignin) 与 [`plugins.v3/ptsitesignin`](plugins.v3/ptsitesignin)（两份内容一致）。

## 安装

**通过插件市场**：在 MoviePilot 的「插件市场」里加上本仓库地址，然后刷新市场。

```
https://github.com/qingyukb/MoviePilot-Plugins
```

**手动**：把插件目录复制到 MoviePilot 的 `/config/plugins/` 下再重启。

```bash
git clone --depth 1 https://github.com/qingyukb/MoviePilot-Plugins.git /tmp/mp-plugins
cp -r /tmp/mp-plugins/plugins.v2/ptsitesignin /config/plugins/   # V2
cp -r /tmp/mp-plugins/plugins.v3/ptsitesignin /config/plugins/   # V3
```

## 目录

```
.
├── package.json               # 默认插件市场清单
├── package.v2.json            # V2 市场清单
├── package.v3.json            # V3 市场清单
├── icons/                     # 市场卡片图标
├── plugins.v2/ptsitesignin/   # 插件源码（V2）
├── plugins.v3/ptsitesignin/   # 插件源码（V3，与 V2 一致）
└── tests/v3/ptsitesignin/     # 测试
```

目录名、索引字段、版本号规则都按 [官方插件仓库](https://github.com/jxxghp/MoviePilot-Plugins) 的提交要求来：目录名 = 插件主类名小写，`plugin_version` 与索引 `version`、最新 `history` 一致，版本历史按语义版本降序。

## 加站点规则

内置预设没覆盖的站，浏览器登录后打开签到页，F12 控制台执行 `plugins.v2/ptsitesignin/tools/collect-rule-snippet.js`，按输出填到插件的「自定义站点规则 JSON」里。字段说明见 [插件 README](plugins.v2/ptsitesignin/README.md#自定义站点规则)。
