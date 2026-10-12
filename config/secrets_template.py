"""
敏感配置文件 (config/secrets.py)
==============================
存放所有 API 密钥、Token 等敏感信息。

【安全说明】
- 此文件已在 .gitignore 中，不会被提交到 Git
- 首次使用请复制 secrets_template.py 为 secrets.py 并填入真实值
- 切勿将此文件分享给他人或上传到公开仓库
"""

class Secrets:
    """敏感配置类 - 存放所有密钥和凭证"""
    
    # ============================================
    # Tushare 金融数据接口
    # 获取地址：https://tushare.pro/register
    # ============================================
    TUSHARE_TOKEN = ""
    
    # ============================================
    # DragonEye 龙虎榜监控凭证
    # ============================================
    DRAGON_USERNAME = ""
    DRAGON_PASSWORD = ""
    DRAGON_TOKEN = ""
    
    # ============================================
    # QuickTiny (stock.quicktiny.cn) 爬虫凭证
    # 用于获取龙头股、市场情绪等数据
    # ============================================
    QUICKTINY_TOKEN = ""
    QUICKTINY_USERNAME = ""
    QUICKTINY_PASSWORD = ""
    
    # ============================================
    # 飞书配置
    # ============================================
    # Webhook 推送地址（用于单向消息推送）
    FEISHU_WEBHOOK = ""

    # ============================================
    # OpenClaw/ClawBot 配置
    # ============================================
    # OpenClaw Webhook 推送地址（用于通知消息推送）
    OPENCLAW_WEBHOOK = ""
    # OpenClaw 微信 CLI 推送目标。没有 Webhook 时可用：
    # openclaw message send --channel openclaw-weixin --target <OPENCLAW_WEIXIN_TARGET>
    OPENCLAW_WEIXIN_TARGET = ""
    OPENCLAW_WEIXIN_ACCOUNT = ""
    OPENCLAW_WEIXIN_CHANNEL = "openclaw-weixin"

    # ============================================
    # PushPlus 配置（个人微信/QQ 推送，免费）
    # 获取地址：https://www.pushplus.plus/
    # 登录后扫描微信二维码绑定，在"一对一发送"页面获取 token
    # 推送内容会直接发送到你绑定的微信/QQ
    # ============================================
    PUSHPLUS_TOKEN = ""
    
    # 机器人配置（用于 WebSocket 长连接）
    # 获取方式：飞书开放平台 -> 开发者后台 -> 应用详情页 -> 凭证与基础信息
    # 
    # 【必需权限】
    # 在飞书开放平台 -> 权限管理 中开通以下权限：
    # - im:message - 获取与发送消息
    # - im:message:send_as_bot - 以应用身份发消息
    # - im:message.p2p_msg:readonly - 读取用户发给机器人的单聊消息
    # - im:message.group_at_msg:readonly - 接收群聊中@机器人消息事件
    # - im:chat - 获取群组信息
    # - im:resource - 获取消息中的图片/文件资源
    # - optical_char_recognition:optical_char_recognition - OCR 文字识别
    # 
    # 【事件订阅】
    # 在事件与回调 -> 事件配置 中添加：
    # - im.message.receive_v1 - 接收消息事件
    # 
    # 【订阅方式】
    # 选择「使用长连接接收事件」
    FEISHU_APP_ID = ""
    FEISHU_APP_SECRET = ""
    
    # ============================================
    # LLM 大模型配置
    # ============================================
    LLM_API_BASE = "http://127.0.0.1:1234/v1"
    LLM_API_KEY = ""
    LLM_MODEL_NAME = "qwen2.5-7b"
    LLM_BACKUP_API_BASE = ""
    LLM_BACKUP_API_KEY = ""
    LLM_BACKUP_MODEL_NAME = ""
    LLM_DAILY_TOKEN_BUDGET = "0"
    LLM_USAGE_LOG_PATH = ""
    
    # ============================================
    # Redis 配置（如需密码认证）
    # ============================================
    REDIS_PASSWORD = ""

    # ============================================
    # 问财 (iwencai) 配置
    # 获取地址：https://www.iwencai.com
    # ============================================
    IWENCAI_BASE_URL = "https://openapi.iwencai.com"
    IWENCAI_API_KEY = ""
    IWENCAI_COOKIE = ""

    # ============================================
    # 同花顺 iFinD 配置
    # 获取地址：https://quantapi.51ifind.com
    # ============================================
    IFIND_REFRESH_TOKEN = ""
    IFIND_ACCESS_TOKEN = ""

    # ============================================
    # 本地 LLM 配置 (Research Data Engine)
    # 用于本地部署的 LLM 服务，如 Ollama、LM Studio 等
    # ============================================
    LOCAL_LLM_API_BASE = ""  # e.g. http://localhost:11434/v1
    LOCAL_LLM_MODEL_NAME = ""  # e.g. qwen2.5-7b, 若为空则回退到 LLM_MODEL_NAME
    LOCAL_LLM_TIMEOUT = 60  # 秒
    LOCAL_LLM_MAX_BATCH = 5  # 单批最大 chunk 数
    LOCAL_LLM_ENABLED = False  # 布尔开关，为 False 时自动使用 rule_based

    # ============================================
    # SearXNG 搜索引擎配置（紫苏叶选股法联网搜索）
    # 如未配置，自动降级到 DuckDuckGo 搜索
    # ============================================
    SEARXNG_BASE_URL = ""  # e.g. http://localhost:8888
    SEARXNG_ENGINES = (
        "duckduckgo,bing,brave,wikipedia,wikidata,baidu,baidu kaifa"
    )

    # ============================================
    # 站点外部可访问地址（用于飞书推送中的详情链接）
    # 本地开发填 http://localhost:5173
    # 公网部署填 https://your-domain.com
    # 飞书手机端打不开 localhost，需填公网地址
    # ============================================
    SITE_BASE_URL = "http://localhost:5173"

    # ============================================
    # 其他 API 密钥
    # ============================================
    # 在此添加其他第三方服务密钥

    # ============================================
    # 百度 AI 搜索配置
    # 获取地址：https://console.bce.baidu.com/ai-search/qianfan/ais/console/apiKey
    # 日限100次，超出后降级到 DuckDuckGo
    # ============================================
    BAIDU_API_KEY = ""
