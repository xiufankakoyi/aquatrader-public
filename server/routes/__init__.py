"""
路由模块
统一导出所有路由蓝图
"""
from flask import Blueprint
import logging

logger = logging.getLogger(__name__)

# 延迟导入，避免循环依赖
def register_routes(app):
    """注册所有路由到 Flask 应用"""

    # 路由模块属于应用启动契约。导入失败必须让启动失败，避免接口静默消失。
    route_modules = [
        ('server.routes.strategy_routes', 'strategy_bp'),
        ('server.routes.backtest_routes', 'backtest_bp'),
        ('server.routes.data_routes', 'data_bp'),
        ('server.routes.data_health_routes', 'data_health_bp'),
        ('server.routes.scatter_routes', 'scatter_bp'),
        ('server.routes.sentiment_routes', 'sentiment_bp'),
        ('server.routes.optimization_routes', 'optimization_bp'),
        ('server.routes.system_routes', 'system_bp'),
        ('server.routes.dragon_eye_routes', 'dragon_bp'),
        ('server.routes.portfolio_routes', 'portfolio_bp'),
        ('server.routes.game_routes', 'game_bp'),
        ('server.routes.screener_routes', 'screener_bp'),
        ('server.routes.export_routes', 'export_bp'),
        ('server.routes.similarity_routes', 'similarity_bp'),
        ('server.routes.event_routes', 'event_bp'),
        ('server.routes.pattern_routes', 'pattern_bp'),
        ('server.routes.pattern_jobs_routes', 'pattern_jobs_bp'),
        ('server.concept_lab.concept_api', 'concept_bp'),
        ('server.routes.news_routes', 'news_bp'),
        ('server.industry_chain.api', 'industry_chain_bp'),
        ('server.routes.research_routes', 'research_bp'),
        ('server.routes.research_engine_routes', 'research_engine_bp'),
        ('server.routes.earnings_routes', 'earnings_bp'),
        ('server.routes.expectations_routes', 'expectations_bp'),
        ('server.routes.valuation_routes', 'valuation_bp'),
        ('server.routes.reporting_routes', 'reporting_bp'),
        ('server.routes.candidate_routes', 'candidate_bp'),
        ('server.routes.adaptation_routes', 'adaptation_bp'),
        ('server.routes.mobile_auth_routes', 'mobile_auth_bp'),
        ('server.routes.mobile_routes', 'mobile_bp'),
        ('server.routes.command_routes', 'command_bp'),
        ('server.routes.task_routes', 'task_bp'),
        ('server.routes.openclaw_routes', 'openclaw_bp'),
        ('server.routes.sse_routes', 'sse_bp'),
        ('server.routes.perilla_routes', 'perilla_bp'),
        ('server.routes.quant_routes', 'quant_bp'),
        ('server.routes.theme_routes', 'theme_bp'),
        ('server.routes.research_committee_routes', 'committee_bp'),
        ('server.routes.research_model_routes', 'research_model_bp'),
        ('server.routes.observability_routes', 'observability_bp'),
    ]

    for module_name, bp_name in route_modules:
        module = __import__(module_name, fromlist=[bp_name])
        blueprint = getattr(module, bp_name)
        app.register_blueprint(blueprint)
        logger.info(f"路由注册成功: {module_name}")
