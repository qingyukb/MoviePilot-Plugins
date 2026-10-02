"""M-Team 自动登录 / 账号保活（免验证码版）——MoviePilot V2、V3 兼容。

只使用 **用户名 + 密码** 完成站点真实登录，不涉及动态验证码（TOTP），
适合**未开启二次验证**的账号。登录走站点真实接口，令牌由服务端签发。

核心流程：
1. POST /api/login                    真实登录，响应头 Authorization 即真实令牌
2. POST /api/member/profile           校验令牌并读取账号数据
3. POST /api/member/updateLastBrowse  刷新最后访问时间（保活）

若账号实际开启了二次验证，服务端会返回 code=1001，本插件不处理验证码，
会给出明确提示——此时请改用完整版插件「M-Team 自动登录」，或关闭二次验证。
"""

import base64
import hashlib
import hmac
import random
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

try:  # MoviePilot V3 命名空间
    from app.sdk.events import Event, eventmanager
    from app.sdk.logging import logger
except Exception:  # MoviePilot V1/V2 命名空间
    from app.core.event import Event, eventmanager
    from app.log import logger

from app.plugins import _PluginBase
from app.schemas.types import EventType


class MTeamAutoSimple(_PluginBase):
    """M-Team 免验证码自动登录与保活插件（仅账号密码）。"""

    # 插件名称
    plugin_name = "M-Team 自动登录（免验证码）"
    # 插件描述
    plugin_desc = "只用账号密码真实登录 M-Team 并刷新最后访问时间保活，适用于未开启二次验证的账号。"
    # 插件图标
    plugin_icon = "mteamautosimple.png"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "local"
    # 作者主页
    author_url = ""
    # 插件配置项 ID 前缀
    plugin_config_prefix = "mteamautosimple_"
    # 加载顺序
    plugin_order = 22
    # 可使用的用户级别
    auth_level = 1

    # -------- 固定常量 --------
    _SIGN_KEY = "HLkPcWmycL57mfJt"
    _API_HOST = "api.m-team.io"
    _REFERER = "https://kp.m-team.cc/"
    _DEFAULT_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )
    _CHARSET = "abcdefghijklmnopqrstuvwxyz0123456789"

    def __init__(self):
        """初始化插件运行时变量。"""
        super().__init__()
        self._enabled: bool = False
        self._notify: bool = True
        self._username: str = ""
        self._password: str = ""
        self._login_cron: str = "0 */6 * * *"
        self._proxy: str = ""
        self._random_delay: int = 0
        self._run_once: bool = False
        self._token: str = ""
        self._did: str = ""
        self._visitor_id: str = ""
        self._running: bool = False

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def init_plugin(self, config: dict = None) -> None:
        """读取配置并初始化插件运行状态。"""
        self.stop_service()
        self._enabled = False
        if not config:
            return

        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify", True))
        self._username = str(config.get("username") or "").strip()
        self._password = str(config.get("password") or "")
        self._login_cron = str(config.get("login_cron") or "").strip()
        self._proxy = str(config.get("proxy") or "").strip()
        self._random_delay = int(config.get("random_delay") or 0)
        self._run_once = bool(config.get("run_once"))

        self._token = self.get_data("token") or ""
        self._did = self.get_data("did") or ""
        self._visitor_id = self.get_data("visitor_id") or ""

        if self._random_delay > 30:
            self._random_delay = 30

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    def stop_service(self) -> None:
        """停用插件时清理运行时状态。"""
        self._running = False

    # ------------------------------------------------------------------ #
    # 配置表单 / 详情页
    # ------------------------------------------------------------------ #
    @staticmethod
    def _col(component: dict, cols: int = 12, md: int = 4) -> dict:
        """构造一个栅格列。"""
        return {"component": "VCol", "props": {"cols": cols, "md": md}, "content": [component]}

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置页的 JSON 与默认配置。"""
        return (
            [
                {
                    "component": "VForm",
                    "content": [
                        {
                            "component": "VRow",
                            "content": [
                                {
                                    "component": "VCol",
                                    "props": {"cols": 12},
                                    "content": [
                                        {
                                            "component": "VAlert",
                                            "props": {
                                                "type": "info",
                                                "variant": "tonal",
                                                "title": "适用条件",
                                                "text": (
                                                    "本插件只用账号密码登录，不处理动态验证码。"
                                                    "请确认站点账号未开启二次验证；"
                                                    "若已开启，登录时会被要求验证码，插件会提示改用完整版插件。"
                                                ),
                                            },
                                        }
                                    ],
                                }
                            ],
                        },
                        {
                            "component": "VRow",
                            "content": [
                                self._col(
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    },
                                    md=4,
                                ),
                                self._col(
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "notify", "label": "发送通知"},
                                    },
                                    md=4,
                                ),
                                self._col(
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "run_once", "label": "立即运行一次"},
                                    },
                                    md=4,
                                ),
                            ],
                        },
                        {
                            "component": "VRow",
                            "content": [
                                self._col(
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "username", "label": "用户名 / 邮箱"},
                                    },
                                    md=6,
                                ),
                                self._col(
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "password",
                                            "label": "密码",
                                            "type": "password",
                                        },
                                    },
                                    md=6,
                                ),
                            ],
                        },
                        {
                            "component": "VRow",
                            "content": [
                                self._col(
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "login_cron",
                                            "label": "登录周期 Cron 表达式",
                                            "placeholder": "0 */6 * * *",
                                            "hint": (
                                                "五段式 Cron：分 时 日 月 周。"
                                                "0 */6 * * * = 每 6 小时；0 */2 * * * = 每 2 小时；"
                                                "30 8 * * * = 每天 8:30"
                                            ),
                                            "persistent-hint": True,
                                        },
                                    },
                                    md=6,
                                ),
                                self._col(
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "random_delay",
                                            "label": "随机延迟（分钟，0-30）",
                                            "type": "number",
                                        },
                                    },
                                    md=3,
                                ),
                                self._col(
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "proxy",
                                            "label": "代理地址（可选）",
                                            "placeholder": "http://192.168.1.2:7890",
                                        },
                                    },
                                    md=3,
                                ),
                            ],
                        },
                    ],
                }
            ],
            {
                "enabled": False,
                "notify": True,
                "username": "",
                "password": "",
                "login_cron": "0 */6 * * *",
                "proxy": "",
                "random_delay": 0,
                "run_once": False,
            },
        )

    def get_page(self) -> List[dict]:
        """返回插件详情页的 JSON。"""
        history = self.get_data("history") or []
        lines = ["暂无执行记录"] if not history else [
            f"{item.get('time', '')}  {item.get('status', '')}  {item.get('message', '')}"
            for item in history[:20]
        ]
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "title": "运行状态",
                    "text": f"插件状态：{'已启用' if self._enabled else '未启用'}；"
                            f"登录周期：{self._login_cron or '未设置'}；"
                            f"当前令牌：{'有效缓存' if self._token else '无'}",
                },
            },
            {
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mt-3"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "props": {"class": "text-subtitle-1"},
                        "text": "最近执行记录",
                    },
                    {
                        "component": "VCardText",
                        "props": {"class": "text-body-2"},
                        "text": "\n".join(lines),
                    },
                ],
            },
        ]

    # ------------------------------------------------------------------ #
    # 服务 / 命令 / API
    # ------------------------------------------------------------------ #
    def get_service(self) -> List[Dict[str, Any]]:
        """注册定时登录服务。"""
        services: List[Dict[str, Any]] = []
        if self._run_once:
            services.append(
                {
                    "id": "MTeamAutoSimple.Once",
                    "name": "M-Team 自动登录（立即运行一次）",
                    "trigger": DateTrigger(run_date=datetime.now() + timedelta(seconds=5)),
                    "func": self.do_login,
                    "kwargs": {"force": True, "once": True},
                }
            )
        if self._enabled and self._login_cron:
            trigger = None
            try:
                trigger = CronTrigger.from_crontab(self._login_cron)
            except Exception as err:
                logger.error(f"M-Team 免验证码登录：Cron 表达式不合法 {self._login_cron} - {err}")
            if trigger:
                services.append(
                    {
                        "id": "MTeamAutoSimple.Login",
                        "name": "M-Team 自动登录保活",
                        "trigger": trigger,
                        "func": self.do_login,
                        "kwargs": {},
                    }
                )
        return services

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册远程命令。"""
        return [
            {
                "cmd": "/mteam_login_simple",
                "event": EventType.PluginAction,
                "desc": "手动执行 M-Team 免验证码登录保活",
                "category": "插件命令",
                "data": {"action": "mteam_login_simple"},
            }
        ]

    @eventmanager.register(EventType.PluginAction)
    def mteam_login_command(self, event: Event) -> None:
        """响应远程命令触发。"""
        if not event:
            return
        event_data = event.event_data or {}
        if event_data.get("action") != "mteam_login_simple":
            return
        self.do_login(force=True)

    def get_api(self) -> List[Dict[str, Any]]:
        """声明插件 API。"""
        return [
            {
                "path": "/login_now",
                "endpoint": self.api_login_now,
                "methods": ["POST", "GET"],
                "auth": "bear",
                "summary": "立即执行一次 M-Team 登录",
            },
            {
                "path": "/status",
                "endpoint": self.api_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查询插件运行状态",
            },
        ]

    def api_login_now(self) -> dict:
        """API：立即执行一次登录。"""
        if self._running:
            return {"success": False, "message": "任务正在执行中"}
        ok = self.do_login(force=True)
        return {"success": ok, "message": "执行完成，详情见插件日志"}

    def api_status(self) -> dict:
        """API：返回运行状态。"""
        return {
            "success": True,
            "enabled": self._enabled,
            "running": self._running,
            "cron": self._login_cron,
            "has_token": bool(self._token),
            "history": self.get_data("history") or [],
        }

    # ------------------------------------------------------------------ #
    # 核心逻辑
    # ------------------------------------------------------------------ #
    def do_login(self, force: bool = False, once: bool = False) -> bool:
        """执行一次登录 + 保活流程（仅账号密码）。"""
        if self._running:
            logger.warn("M-Team 免验证码登录：上一次任务尚未结束，本次跳过")
            return False
        if not self._username or not self._password:
            logger.warn("M-Team 免验证码登录：未配置用户名或密码，跳过执行")
            return False

        self._running = True
        try:
            if self._random_delay > 0:
                delay = random.randint(0, self._random_delay * 60)
                logger.info(f"M-Team 免验证码登录：随机延迟 {delay} 秒执行")
                time.sleep(delay)

            session = self._create_session()

            # 1. 先复用已保存的令牌，避免每次都拿密码去登录
            if not force and self._token:
                ok, summary = self._fetch_profile(session)
                if ok:
                    self._after_success(summary, mode="缓存令牌校验")
                    return True
                logger.info("M-Team 免验证码登录：缓存令牌已失效，开始重新登录")
                self._token = ""
                self.save_data("token", "")

            # 2. 真实登录（只有账号密码）
            result_code = {"value": 0}
            token = self._real_login(session, result_code)
            if not token:
                if result_code["value"] == 1001:
                    self._after_failure(
                        "账号已开启二次验证，服务端要求提交动态验证码。"
                        "请关闭站点二次验证，或改用完整版插件「M-Team 自动登录」"
                    )
                else:
                    self._after_failure("登录失败，请检查用户名与密码")
                return False
            self._token = token
            self.save_data("token", token)

            # 3. 拉取账号信息并刷新最后访问时间
            ok, summary = self._fetch_profile(session)
            if not ok:
                self._after_failure(summary or "登录成功但账号信息校验失败")
                return False

            self._after_success(summary, mode="真实登录")
            return True
        except Exception as err:
            logger.error(f"M-Team 免验证码登录：执行异常 - {err}")
            self._after_failure(f"执行异常：{err}")
            return False
        finally:
            self._running = False
            if once:
                self.update_config({**self._current_config(), "run_once": False})

    def _real_login(self, session, code_out: Optional[dict] = None) -> str:
        """调用 /api/login 完成真实登录，返回服务端令牌。"""
        resp = self._post(session, "/api/login", {
            "username": self._username,
            "password": self._password,
            "turnstile": "",
        })
        result = self._parse(resp)
        if result is None:
            logger.error("M-Team 免验证码登录：登录接口无有效响应")
            return ""

        if code_out is not None:
            code_out["value"] = int(result.get("code") or 0)

        if result.get("message") != "SUCCESS":
            if code_out is not None and code_out["value"] == 1001:
                logger.error(
                    "M-Team 免验证码登录：服务端要求二次验证，本插件不处理验证码，"
                    "请关闭站点二次验证或改用完整版插件"
                )
            else:
                logger.error(f"M-Team 免验证码登录：登录失败 - {result}")
            return ""

        token, did = "", ""
        try:
            for key, value in resp.headers.items():
                low = key.lower()
                if low == "authorization":
                    token = value
                elif low == "did":
                    did = value
        except Exception:
            pass
        if did:
            self._did = did
            self.save_data("did", did)
        if not token:
            logger.error("M-Team 免验证码登录：登录返回成功但未取到 Authorization 令牌")
        return token

    def _fetch_profile(self, session) -> Tuple[bool, str]:
        """调用 /api/member/profile 校验令牌并读取账号数据。"""
        resp = self._post(session, "/api/member/profile", {}, token=self._token, origin=True)
        if resp is None:
            return False, "网络请求失败"
        if resp.status_code != 200:
            return False, f"令牌校验失败 status={resp.status_code}"
        result = self._parse(resp)
        if not result:
            return False, "响应解析失败"
        if result.get("message") != "SUCCESS":
            return False, f"令牌无效：{result.get('message')}"

        data = result.get("data") or {}
        count = data.get("memberCount") or {}
        status = data.get("memberStatus") or {}
        summary = (
            f"账号：{data.get('username', '-')}；"
            f"上传 {self._to_gb(count.get('uploaded'))}；"
            f"下载 {self._to_gb(count.get('downloaded'))}；"
            f"魔力值 {count.get('bonus', '-')}；"
            f"最后访问 {status.get('lastBrowse', '-')}"
        )

        # 保活：刷新最后访问时间
        keep = self._post(session, "/api/member/updateLastBrowse", {},
                          token=self._token, origin=True)
        keep_result = self._parse(keep) if keep is not None else None
        if keep_result and keep_result.get("message") == "SUCCESS":
            logger.info("M-Team 免验证码登录：最后访问时间已刷新")
        else:
            logger.warn(f"M-Team 免验证码登录：刷新最后访问时间失败 - {keep_result}")
        return True, summary

    # ------------------------------------------------------------------ #
    # HTTP 支撑
    # ------------------------------------------------------------------ #
    def _create_session(self):
        """创建 HTTP 会话，优先使用可绕过 Cloudflare 的指纹库。"""
        try:
            from curl_cffi import requests as curl_requests  # type: ignore

            logger.debug("M-Team 免验证码登录：使用 curl_cffi 指纹会话")
            return curl_requests.Session(impersonate="chrome")
        except Exception:
            pass
        try:
            import cloudscraper  # type: ignore

            logger.debug("M-Team 免验证码登录：使用 cloudscraper 会话")
            return cloudscraper.create_scraper()
        except Exception:
            logger.debug("M-Team 免验证码登录：使用 requests 会话")
            return requests.Session()

    def _post(self, session, path: str, extra: dict, token: str = "", origin: bool = False):
        """发送带签名与鉴权头的接口请求。"""
        ts_ms = int(time.time() * 1000)
        body = dict(extra)
        body["_timestamp"] = str(ts_ms)
        body["_sgin"] = self._sign("POST", path, ts_ms)

        headers = {
            "User-Agent": self._DEFAULT_UA,
            "referer": self._REFERER,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "application/json;charset=UTF-8",
            "Ts": str(int(time.time())),
            "Did": self._did or self._random_string(16),
            "visitorid": self._visitor_id or self._ensure_visitor_id(),
        }
        if origin:
            headers["origin"] = self._REFERER
        if token:
            headers["Authorization"] = token

        proxies = {"http": self._proxy, "https": self._proxy} if self._proxy else None
        try:
            resp = session.request("POST", f"https://{self._API_HOST}{path}",
                                   data=body, headers=headers, proxies=proxies, timeout=30)
        except Exception as err:
            logger.error(f"M-Team 免验证码登录：请求 {path} 失败 - {err}")
            return None

        did = resp.headers.get("Did") or resp.headers.get("did")
        if did and did != self._did:
            self._did = did
            self.save_data("did", did)
        return resp

    @staticmethod
    def _parse(resp) -> Optional[dict]:
        """安全解析响应 JSON。"""
        if resp is None:
            return None
        try:
            data = resp.json()
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _sign(self, method: str, path: str, ts_ms: int) -> str:
        """计算接口签名：base64(HMAC-SHA1("METHOD&path&时间戳"))。"""
        message = f"{method}&{path}&{ts_ms}".encode("utf-8")
        digest = hmac.new(self._SIGN_KEY.encode("utf-8"), message, hashlib.sha1).digest()
        return base64.b64encode(digest).decode("utf-8")

    @classmethod
    def _random_string(cls, length: int) -> str:
        """生成随机小写字母数字串。"""
        return "".join(random.choice(cls._CHARSET) for _ in range(length))

    def _ensure_visitor_id(self) -> str:
        """获取或生成持久化的 visitorid。"""
        if self._visitor_id:
            return self._visitor_id
        self._visitor_id = self._random_string(32)
        self.save_data("visitor_id", self._visitor_id)
        return self._visitor_id

    @staticmethod
    def _to_gb(value: Any) -> str:
        """字节转换为 GB 展示。"""
        try:
            return f"{int(value) / 1073741824:.2f} GB"
        except Exception:
            return "-"

    # ------------------------------------------------------------------ #
    # 结果处理
    # ------------------------------------------------------------------ #
    def _current_config(self) -> dict:
        """返回当前生效的配置字典。"""
        return {
            "enabled": self._enabled,
            "notify": self._notify,
            "username": self._username,
            "password": self._password,
            "login_cron": self._login_cron,
            "proxy": self._proxy,
            "random_delay": self._random_delay,
            "run_once": False,
        }

    def _after_success(self, summary: str, mode: str) -> None:
        """记录并通知成功结果。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        message = f"{mode}成功；{summary}"
        logger.info(f"M-Team 免验证码登录：{message}")
        self._append_history({"time": now, "status": "成功", "message": message})
        if self._notify:
            self.post_message(title="【M-Team 自动登录】成功", text=f"{now}\n{message}")

    def _after_failure(self, message: str) -> None:
        """记录并通知失败结果。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.error(f"M-Team 免验证码登录：{message}")
        self._append_history({"time": now, "status": "失败", "message": message})
        if self._notify:
            self.post_message(title="【M-Team 自动登录】失败", text=f"{now}\n{message}")

    def _append_history(self, item: dict) -> None:
        """追加执行历史（仅保留最近 20 条）。"""
        history = self.get_data("history") or []
        history.insert(0, item)
        self.save_data("history", history[:20])
