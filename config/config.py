"""
配置文件 (config/config.py)
聚合 Setting 模块与动态生成的路径配置。

【LanceDB 架构说明】
====================
系统使用 LanceDB 作为数据存储后端：
- 存储层 (LanceDB): 向量化存储，高效查询，列式存储
- 分析层 (Polars): 复杂表达式计算，向量化执行

配置项：
- LANCEDB_PATH: LanceDB 存储路径
- ARROW_BATCH_SIZE: Arrow 批处理大小
"""
import os
from pathlib import Path
from config.setting import Setting

class Config:
    """配置类 - 统一引用 Setting 模块"""
    # 1. 基础路径配置
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    # 2. 数据库配置
    DB_PATH = Setting.DB_PATH or os.path.join(
        BASE_DIR, 'data', 'database', 'stock_data.db'
    )
    DATABASE_PATH = DB_PATH
    DB_BACKEND = Setting.DB_BACKEND
    ENABLE_PARQUET_FALLBACK = Setting.ENABLE_PARQUET_FALLBACK
    
    # 3. LanceDB 配置
    LANCEDB_PATH = Setting.LANCEDB_PATH or os.path.join(
        BASE_DIR, 'data', 'lancedb'
    )
    
    # 4. Apache Arrow 交互层配置
    ARROW_BATCH_SIZE = Setting.ARROW_BATCH_SIZE
    ARROW_ZERO_COPY = Setting.ARROW_ZERO_COPY
    
    # 5. 核心目录
    DATA_DIR = os.path.join(BASE_DIR, 'data')
    PARQUET_DIR = os.path.join(BASE_DIR, 'data', 'parquet_data')
    SPIDER_DATA_DIR = os.path.join(BASE_DIR, 'data', 'spider_data')
    LOGS_DIR = os.path.join(BASE_DIR, 'logs')
    MODELS_DIR = os.path.join(BASE_DIR, 'models')
    
    # 7. 核心 Token & API Key (从 Setting 读取)
    TUSHARE_TOKEN = Setting.TUSHARE_TOKEN
    DRAGON_USERNAME = Setting.DRAGON_USERNAME
    DRAGON_PASSWORD = Setting.DRAGON_PASSWORD
    DRAGON_TOKEN = Setting.DRAGON_TOKEN
    FEISHU_WEBHOOK = Setting.FEISHU_WEBHOOK

    # 飞书机器人配置
    FEISHU_APP_ID = Setting.FEISHU_APP_ID
    FEISHU_APP_SECRET = Setting.FEISHU_APP_SECRET

    # OpenClaw/ClawBot 配置
    OPENCLAW_WEBHOOK = Setting.OPENCLAW_WEBHOOK
    OPENCLAW_WEIXIN_TARGET = Setting.OPENCLAW_WEIXIN_TARGET
    OPENCLAW_WEIXIN_ACCOUNT = Setting.OPENCLAW_WEIXIN_ACCOUNT
    OPENCLAW_WEIXIN_CHANNEL = Setting.OPENCLAW_WEIXIN_CHANNEL

    # PushPlus 配置（个人微信/QQ 推送，免费）
    PUSHPLUS_TOKEN = Setting.PUSHPLUS_TOKEN
    
    # LLM 配置
    LLM_API_BASE = Setting.LLM_API_BASE
    LLM_API_KEY = Setting.LLM_API_KEY
    LLM_MODEL_NAME = Setting.LLM_MODEL_NAME
    LLM_BACKUP_API_BASE = Setting.LLM_BACKUP_API_BASE
    LLM_BACKUP_API_KEY = Setting.LLM_BACKUP_API_KEY
    LLM_BACKUP_MODEL_NAME = Setting.LLM_BACKUP_MODEL_NAME
    LLM_DAILY_TOKEN_BUDGET = Setting.LLM_DAILY_TOKEN_BUDGET
    LLM_USAGE_LOG_PATH = Setting.LLM_USAGE_LOG_PATH
    LLM_TEMPERATURE = 0.1
    LLM_MAX_TOKENS = 4096

    # 研究/交易开关
    RESEARCH_DRY_RUN = Setting.RESEARCH_DRY_RUN
    RESEARCH_CONTROL_PLANE_ENABLED = Setting.RESEARCH_CONTROL_PLANE_ENABLED
    RESEARCH_CONTROL_DB_PATH = Setting.RESEARCH_CONTROL_DB_PATH
    LEARNING_PLAN_EXECUTION_ENABLED = Setting.LEARNING_PLAN_EXECUTION_ENABLED
    # 主动学习总开关：默认关闭，关闭时搜索摘要不直接成为证据，必须经过 LLM Claim 抽取
    ACTIVE_LEARNING_ENABLED = Setting.ACTIVE_LEARNING_ENABLED
    
    # 8. 策略行情阈值
    MIN_MARKET_CAP = Setting.MIN_MARKET_CAP
    MAX_MARKET_CAP = Setting.MAX_MARKET_CAP
    MIN_PRICE = Setting.MIN_PRICE
    INITIAL_CAPITAL = Setting.INITIAL_CAPITAL
    
    # 过滤开关
    EXCLUDE_ST = True
    EXCLUDE_KC = True
    EXCLUDE_CY = True
    
    # 交易细则
    COMMISSION_RATE = 0.0005
    MIN_COMMISSION = 5.0
    SELL_TAX = 0.001
    BOARD_LOT = 100
    
    # 9. 环境/中间件配置
    USE_GPU_ACCELERATION = Setting.USE_GPU
    REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
    REDIS_TASK_QUEUE = os.getenv('REDIS_TASK_QUEUE', 'aqua_tasks')
    REDIS_NOTIFICATION_CHANNEL_PREFIX = os.getenv('REDIS_NOTIFICATION_CHANNEL_PREFIX', 'aqua_notifications')
    OPTUNA_REDIS_STORAGE_URL = os.getenv('OPTUNA_REDIS_STORAGE_URL', 'redis://localhost:6379/0')
    
    # 10. 架构选择辅助方法
    @classmethod
    def is_lancedb_backend(cls):
        """
        检查是否使用 LanceDB 后端
        
        Returns:
            bool: 是否使用 LanceDB 后端
        """
        return cls.DB_BACKEND.lower() == 'lancedb'

    @classmethod
    def parquet_fallback_enabled(cls):
        """
        Return True only when legacy Parquet reads are explicitly requested.

        DB_BACKEND=parquet keeps the old Parquet mode available. In LanceDB mode,
        Parquet fallbacks are disabled unless ENABLE_PARQUET_FALLBACK is set.
        """
        return cls.DB_BACKEND.lower() == 'parquet' or bool(cls.ENABLE_PARQUET_FALLBACK)
    
    @classmethod
    def get_data_interface(cls):
        """
        获取当前配置推荐的数据接口
        
        Returns:
            str: 数据接口类型描述
        """
        if cls.is_lancedb_backend():
            return "LanceDB + Polars (存储层 + 分析层)"
        elif cls.DB_BACKEND.lower() == 'parquet':
            return "Parquet 文件存储"
        else:
            return f"未知后端: {cls.DB_BACKEND}"
