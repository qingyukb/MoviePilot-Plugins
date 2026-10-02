# MoviePilot 插件仓库（自用）

个人 MoviePilot 插件集合，按官方仓库规范组织，可直接在 MoviePilot 中通过「添加插件仓库」安装。

仓库地址填入：`https://github.com/qingyu/MoviePilot-Plugins`

## 插件列表

| 插件 | ID | 适用账号 | 说明 |
| --- | --- | --- | --- |
| M-Team 自动登录（免验证码） | `MTeamAutoSimple` | 未开二次验证 | 只用账号密码登录并保活，配置项最少 |
| M-Team 自动登录 | `MTeamAutoLogin` | 任意 | 支持动态验证码（TOTP）二次验证、手动令牌模式、预热访问等 |

两个插件都走站点真实登录接口（`POST /api/login`），令牌由服务端签发，并调用
`/api/member/updateLastBrowse` 刷新最后访问时间。**只装你需要的那个即可。**

## 目录结构

```
├── package.json            # V1 索引（条目内含 "v2": true）
├── package.v2.json         # V2 索引
├── package.v3.json         # V3 索引
├── icons/
│   ├── mteamautologin.png
│   └── mteamautosimple.png
├── plugins/
│   ├── mteamautologin/__init__.py
│   └── mteamautosimple/__init__.py
├── plugins.v2/
│   ├── mteamautologin/__init__.py
│   └── mteamautosimple/__init__.py
└── plugins.v3/
    ├── mteamautologin/__init__.py
    └── mteamautosimple/__init__.py
```

宿主按自身版本读取对应索引，并在与之匹配的目录中查找代码：

| 索引文件 | 代码目录 | 适用宿主 |
| --- | --- | --- |
| `package.v2.json` | `plugins.v2/` | MoviePilot 2.x（优先读取） |
| `package.json` | `plugins/` | 仅读 `package.json` 的 2.x 宿主 |
| `package.v3.json` | `plugins.v3/` | MoviePilot 3.x |

三份代码内容完全一致，无关目录不会被加载。插件使用了 V2/V3 双命名空间导入兜底
（先试 `app.sdk.*`，失败回落 `app.core.*` / `app.log`）。

## 添加方式

1. MoviePilot →「设定 → 插件 → 插件市场」
2. 右上角「添加插件仓库」，粘贴本仓库地址
3. 在市场中搜索插件名 → 安装 → 配置

> 仓库必须是**公开**的。若 MoviePilot 所在网络访问 GitHub 不稳定，需要给容器配置代理。

## 新增 / 更新插件

1. 源码放到 `plugins/`、`plugins.v2/`、`plugins.v3/` 三个目录的同名文件夹下，内容保持一致
2. 在三个索引文件中新增/修改条目
3. 三处版本号必须一致：源码 `plugin_version`、索引 `version`
4. `icon` 填 `icons/` 下的文件名即可；指向别人仓库的图标则填完整 raw URL

## 说明

- 条目中不要加 `"release": true`，该标记表示插件通过 GitHub Release 附件分发，与本仓库的目录分发方式不同
- 插件不额外引入第三方依赖；M-Team 有 Cloudflare 防护，容器内可选安装 `curl_cffi` 提升成功率
