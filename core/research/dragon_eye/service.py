"""
DragonEye 模块核心服务：编排爬虫、清洗、入库与推送
支持 SSE 实时日志推送和任务状态管理
"""
import subprocess
import sys
import json
import threading
import queue
import os
import polars as pl
from pathlib import Path
from typing import Optional, Dict, Any, Callable, List
from datetime import datetime

from config.config import Config
from config.logger import get_logger
from core.research.dragon_eye.manager import DragonEyeManager
from core.research.dragon_eye.job_manager import job_manager, JobStatus
from core.research.dragon_eye.sentiment_analyzer import (
    analyze_sentiment,
    score_to_dict,
)

logger = get_logger(__name__)


def _find_quant_dir() -> Path:
    """
    动态查找quant目录位置
    基于当前文件位置向上查找，找到包含quant目录的项目根目录
    """
    # 从当前文件开始向上查找
    current_file = Path(__file__).resolve()
    
    # 向上遍历目录树，查找quant目录
    for parent in current_file.parents:
        quant_candidate = parent / "quant"
        if quant_candidate.exists() and quant_candidate.is_dir():
            # 验证quant目录包含必要的文件
            if (quant_candidate / "run_crawler.py").exists():
                return quant_candidate
    
    # 如果找不到，使用基于BASE_DIR的默认路径
    return Path(Config.BASE_DIR) / "quant"


# 动态计算quant目录路径
QUANT_DIR = _find_quant_dir()


class LogCapture:
    """
    日志捕获器，用于实时捕获子进程输出
    支持 SSE 流式推送
    """
    
    def __init__(self, job_id: str):
        self.job_id = job_id
        self.log_queue = queue.Queue()
        self.subscribers: list[Callable] = []
        self._stop_event = threading.Event()
        self._capture_thread: Optional[threading.Thread] = None
    
    def start_capture(self, process: subprocess.Popen):
        """开始捕获进程输出"""
        self._capture_thread = threading.Thread(
            target=self._capture_output,
            args=(process,),
            daemon=True
        )
        self._capture_thread.start()
    
    def _capture_output(self, process: subprocess.Popen):
        """在后台线程中捕获输出"""
        try:
            # 捕获 stdout
            if process.stdout:
                for line in iter(process.stdout.readline, ''):
                    if self._stop_event.is_set():
                        break
                    if line:
                        log_entry = {
                            'timestamp': datetime.now().isoformat(),
                            'level': 'info',
                            'message': line.strip()
                        }
                        self.log_queue.put(log_entry)
                        self._notify_subscribers(log_entry)
            
            # 捕获 stderr
            if process.stderr:
                for line in iter(process.stderr.readline, ''):
                    if self._stop_event.is_set():
                        break
                    if line:
                        log_entry = {
                            'timestamp': datetime.now().isoformat(),
                            'level': 'error',
                            'message': line.strip()
                        }
                        self.log_queue.put(log_entry)
                        self._notify_subscribers(log_entry)
                        
        except Exception as e:
            logger.error(f"Log capture error: {e}")
    
    def subscribe(self, callback: Callable[[Dict], None]):
        """订阅日志更新"""
        self.subscribers.append(callback)
    
    def unsubscribe(self, callback: Callable):
        """取消订阅"""
        self.subscribers = [cb for cb in self.subscribers if cb != callback]
    
    def _notify_subscribers(self, log_entry: Dict):
        """通知所有订阅者"""
        for callback in self.subscribers:
            try:
                callback(log_entry)
            except Exception as e:
                logger.error(f"Error notifying log subscriber: {e}")
    
    def stop(self):
        """停止捕获"""
        self._stop_event.set()
        if self._capture_thread:
            self._capture_thread.join(timeout=2)


class DragonEyeService:
    """
    DragonEye 核心服务类
    
    功能：
    1. 启动爬虫并实时捕获日志
    2. 执行数据清洗和持久化
    3. 推送飞书消息
    4. 管理任务状态和 SSE 流
    """
    
    def __init__(self):
        self.manager = DragonEyeManager()
        self.project_root = Path(Config.BASE_DIR)
        # 使用quant原始版本的路径配置
        self.quant_dir = QUANT_DIR
        # 直接调用main_launcher.py，避免run_crawler.py中的硬编码路径问题
        self.spider_path = self.quant_dir / "main_launcher.py"
        self.cleaner_path = self.quant_dir / "combined.py"
        self.data_lake_dir = self.quant_dir / "data" / "data_lake"
        self.cleaned_data_dir = self.quant_dir / "data" / "cleaned_data"
        self._active_captures: Dict[str, LogCapture] = {}
    
    def run_crawler(self, target_date: str, job_id: Optional[str] = None) -> str:
        """
        启动爬虫任务
        
        Args:
            target_date: 目标日期 (YYYY-MM-DD)
            job_id: 可选的任务ID
            
        Returns:
            job_id: 任务ID
        """
        # 创建任务
        job = job_manager.create_job('crawl', job_id)
        job_id = job.job_id
        
        # 在后台线程中执行爬虫
        thread = threading.Thread(
            target=self._run_crawler_task,
            args=(target_date, job_id),
            daemon=True
        )
        thread.start()
        
        return job_id
    
    def _run_crawler_task(self, target_date: str, job_id: str):
        """在后台执行爬虫任务"""
        job_manager.update_job(job_id, status='running')
        job_manager.add_log(job_id, 'info', f'🚀 启动爬虫任务，目标日期: {target_date}', 0)

        try:
            # 直接调用main_launcher.py，使用python解释器
            # main_launcher.py接收日期作为第一个位置参数
            cmd = [sys.executable, str(self.spider_path), target_date]

            job_manager.add_log(job_id, 'info', f'执行命令: {" ".join(cmd)}', 5)
            job_manager.add_log(job_id, 'info', f'工作目录: {self.quant_dir}', 5)

            # 启动子进程，在quant目录下执行
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1,
                universal_newlines=True,
                encoding='utf-8',
                cwd=str(self.quant_dir)
            )
            
            # 创建日志捕获器
            capture = LogCapture(job_id)
            self._active_captures[job_id] = capture
            
            # 同步日志到 job_manager
            def on_log(log_entry):
                job_manager.add_log(
                    job_id, 
                    log_entry['level'], 
                    log_entry['message']
                )
            
            capture.subscribe(on_log)
            capture.start_capture(process)
            
            # 等待进程完成
            return_code = process.wait()
            capture.stop()
            
            if return_code == 0:
                job_manager.add_log(job_id, 'success', '✅ 爬虫任务完成', 100)
                job_manager.complete_job(job_id, success=True)
            else:
                error_msg = f'爬虫进程异常退出，返回码: {return_code}'
                job_manager.add_log(job_id, 'error', f'❌ {error_msg}')
                job_manager.complete_job(job_id, success=False, error=error_msg)
                
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Crawler task failed: {e}")
            job_manager.add_log(job_id, 'error', f'❌ 任务执行异常: {error_msg}')
            job_manager.complete_job(job_id, success=False, error=error_msg)
        finally:
            if job_id in self._active_captures:
                del self._active_captures[job_id]
    
    def process_and_persist(self, target_date: str, job_id: Optional[str] = None) -> str:
        """
        清洗数据并持久化到 LanceDB
        
        Args:
            target_date: 目标日期 (YYYY-MM-DD)
            job_id: 可选的任务ID
            
        Returns:
            job_id: 任务ID
        """
        job = job_manager.create_job('clean', job_id)
        job_id = job.job_id
        
        thread = threading.Thread(
            target=self._process_task,
            args=(target_date, job_id),
            daemon=True
        )
        thread.start()
        
        return job_id
    
    def _process_task(self, target_date: str, job_id: str):
        """在后台执行清洗任务"""
        job_manager.update_job(job_id, status='running')
        job_manager.add_log(job_id, 'info', f'🧹 开始数据清洗，日期: {target_date}', 0)

        try:
            date_dir = self.data_lake_dir / target_date
            if not date_dir.exists():
                error_msg = f'数据目录不存在: {date_dir}'
                job_manager.add_log(job_id, 'error', f'❌ {error_msg}')
                job_manager.complete_job(job_id, success=False, error=error_msg)
                return

            # 动态导入quant目录下的combined模块
            import importlib.util
            spec = importlib.util.spec_from_file_location("combined", str(self.cleaner_path))
            combined_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(combined_module)
            StockDataCleaner = combined_module.StockDataCleaner

            job_manager.add_log(job_id, 'info', '加载数据文件...', 10)
            output_dir = self.cleaned_data_dir / target_date
            output_dir.mkdir(parents=True, exist_ok=True)
            cleaner = StockDataCleaner(str(date_dir), str(output_dir))

            # 1. 生成大盘情绪数据
            job_manager.add_log(job_id, 'info', '生成市场仪表盘...', 30)
            cleaner.generate_market_dashboard()
            job_manager.add_log(job_id, 'success', '✅ market_dashboard.csv 生成完成', 50)

            # 2. 生成龙头个股数据
            job_manager.add_log(job_id, 'info', '生成个股特征矩阵...', 60)
            cleaner.generate_stock_feature_matrix()
            job_manager.add_log(job_id, 'success', '✅ stock_feature_matrix.csv 生成完成', 80)

            # 3. 生成 AI 简报
            job_manager.add_log(job_id, 'info', '生成 AI 每日复盘简报...', 90)
            cleaner.generate_ai_daily_brief()
            job_manager.add_log(job_id, 'success', '✅ AI 简报生成完成', 95)

            # 4. 数据入库到 ArcticDB
            job_manager.add_log(job_id, 'info', '写入数据库...', 96)
            self._persist_to_db(target_date, output_dir)
            job_manager.add_log(job_id, 'success', '✅ 数据入库完成', 99)

            job_manager.add_log(job_id, 'success', '🎉 数据清洗和入库完成', 100)
            job_manager.complete_job(job_id, success=True)

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Process task failed: {e}")
            job_manager.add_log(job_id, 'error', f'❌ 清洗失败: {error_msg}')
            job_manager.complete_job(job_id, success=False, error=error_msg)
    
    def run_full_pipeline(self, target_date: str, push_feishu: bool = True) -> str:
        """
        执行完整工作流：爬虫 -> 清洗 -> (推送)
        
        Args:
            target_date: 目标日期 (YYYY-MM-DD)
            push_feishu: 是否推送到飞书
            
        Returns:
            job_id: 任务ID
        """
        job = job_manager.create_job('full_pipeline')
        job_id = job.job_id
        
        thread = threading.Thread(
            target=self._run_pipeline_task,
            args=(target_date, job_id, push_feishu),
            daemon=True
        )
        thread.start()
        
        return job_id
    
    def _run_pipeline_task(self, target_date: str, job_id: str, push_feishu: bool):
        """在后台执行完整工作流"""
        job_manager.update_job(job_id, status='running')

        try:
            # Step 1: 爬虫
            job_manager.add_log(job_id, 'info', '📡 步骤 1/3: 启动数据爬虫...', 5)

            # 直接调用main_launcher.py，使用python解释器
            cmd = [sys.executable, str(self.spider_path), target_date]
            job_manager.add_log(job_id, 'info', f'执行命令: {" ".join(cmd)}', 5)
            job_manager.add_log(job_id, 'info', f'工作目录: {self.quant_dir}', 5)

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1,
                universal_newlines=True,
                encoding='utf-8',
                cwd=str(self.quant_dir)
            )

            capture = LogCapture(job_id)
            self._active_captures[job_id] = capture

            def on_log(log_entry):
                job_manager.add_log(job_id, log_entry['level'], log_entry['message'])

            capture.subscribe(on_log)
            capture.start_capture(process)

            return_code = process.wait()
            capture.stop()

            if return_code != 0:
                raise Exception(f'爬虫进程异常退出，返回码: {return_code}')

            job_manager.add_log(job_id, 'success', '✅ 爬虫完成', 35)

            # Step 2: 清洗
            job_manager.add_log(job_id, 'info', '🧹 步骤 2/3: 开始数据清洗...', 40)

            # 动态导入quant目录下的combined模块
            import importlib.util
            spec = importlib.util.spec_from_file_location("combined", str(self.cleaner_path))
            combined_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(combined_module)
            StockDataCleaner = combined_module.StockDataCleaner

            date_dir = self.data_lake_dir / target_date
            output_dir = self.cleaned_data_dir / target_date
            output_dir.mkdir(parents=True, exist_ok=True)
            cleaner = StockDataCleaner(str(date_dir), str(output_dir))

            cleaner.generate_market_dashboard()
            cleaner.generate_stock_feature_matrix()
            cleaner.generate_ai_daily_brief()
            job_manager.add_log(job_id, 'success', '✅ 数据清洗完成', 70)

            # Step 3: 数据入库
            job_manager.add_log(job_id, 'info', '📦 步骤 3/4: 写入数据库...', 75)
            self._persist_to_db(target_date, output_dir)
            job_manager.add_log(job_id, 'success', '✅ 数据入库完成', 85)

            # Step 4: 推送 (可选)
            if push_feishu:
                job_manager.add_log(job_id, 'info', '📤 步骤 4/4: 推送到飞书...', 90)
                success, msg = self.send_to_feishu(target_date)
                if success:
                    job_manager.add_log(job_id, 'success', f'✅ 飞书推送成功', 100)
                else:
                    job_manager.add_log(job_id, 'warning', f'⚠️ 飞书推送失败: {msg}', 100)
            else:
                job_manager.add_log(job_id, 'info', '⏭️ 跳过飞书推送', 100)

            job_manager.complete_job(job_id, success=True)

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Pipeline task failed: {e}")
            job_manager.add_log(job_id, 'error', f'❌ 工作流失败: {error_msg}')
            job_manager.complete_job(job_id, success=False, error=error_msg)
        finally:
            if job_id in self._active_captures:
                del self._active_captures[job_id]
    
    def _persist_to_db(self, target_date: str, output_dir: Path):
        """
        将清洗后的数据持久化到 ArcticDB

        从原始 JSON 数据文件中读取完整信息，包括涨停数量等

        Args:
            target_date: 目标日期 (YYYY-MM-DD)
            output_dir: 清洗后数据目录
        """
        data_lake_dir = self.data_lake_dir / target_date

        # 1. 写入市场情绪数据（从原始 JSON 获取完整信息）
        sentiment_path = data_lake_dir / 'market_sentiment_cycle.json'
        if sentiment_path.exists():
            with open(sentiment_path, 'r', encoding='utf-8') as f:
                sentiment_json = json.load(f)

            data_list = sentiment_json.get('data', [])
            current_data = None
            for item in data_list:
                if item.get('date') == target_date:
                    current_data = item
                    break

            if current_data:
                emotion = current_data.get('emotionMetrics', {})
                ladder = current_data.get('ladder', {})
                themes = current_data.get('themes', [])[:2]

                # 计算涨停总数（所有连板层级的股票数量之和）
                limit_up_count = sum(len(stocks) for stocks in ladder.values()) if ladder else 0

                # 最高连板高度
                max_height = max(map(int, ladder.keys())) if ladder else 0

                # 找前一交易日数据（用于周期阶段判断）
                previous_data = None
                prev2_data = None
                try:
                    target_ts = datetime.strptime(target_date, '%Y-%m-%d')
                    sorted_data = sorted(
                        [d for d in data_list if d.get('date')],
                        key=lambda x: x['date'],
                        reverse=True,
                    )
                    for d in sorted_data:
                        if d.get('date') == target_date:
                            continue
                        try:
                            d_ts = datetime.strptime(d['date'], '%Y-%m-%d')
                            if d_ts < target_ts:
                                if previous_data is None:
                                    previous_data = d
                                elif prev2_data is None:
                                    prev2_data = d
                                    break
                        except (TypeError, ValueError):
                            continue
                except Exception:
                    previous_data = None
                    prev2_data = None

                # 调用情绪分析器，补全周期/风险/接力/综合分
                sentiment_score = analyze_sentiment(current_data, previous_data, prev2_data)
                rates = emotion.get('promotionRates', {}) or {}

                df_sentiment = pl.DataFrame([{
                    'trade_date': target_date,
                    'broken_ratio': emotion.get('brokenRatio', 0),
                    'broken_count': emotion.get('brokenCount', 0),
                    'limit_down_count': emotion.get('limitDownCount', 0),
                    'limit_up_count': limit_up_count,
                    'max_height': max_height,
                    'main_themes': ','.join([t.get('name', '') for t in themes]),
                    'rise_count': current_data.get('marketSentiment', {}).get('rise', 0),
                    'fall_count': current_data.get('marketSentiment', {}).get('fall', 0),
                    # 以下为情绪分析器扩展字段
                    'cycle_phase': sentiment_score.cycle_phase,
                    'cycle_reasons': '|'.join(sentiment_score.cycle_reasons),
                    'risk_level': sentiment_score.risk_level,
                    'promotion_1to2': sentiment_score.promotion_1to2,
                    'promotion_2to3': sentiment_score.promotion_2to3,
                    'promotion_high': sentiment_score.promotion_high,
                    'theme_continuity': sentiment_score.theme_continuity,
                    'theme_flow': sentiment_score.theme_flow,
                    'sentiment_score': sentiment_score.total_score,
                    'summary': sentiment_score.summary,
                }])

                self.manager.upsert_sentiment(df_sentiment)
                logger.info(
                    f"Persisted market sentiment for {target_date}: "
                    f"phase={sentiment_score.cycle_phase}, "
                    f"risk={sentiment_score.risk_level}, "
                    f"score={sentiment_score.total_score}"
                )
        
        # 2. 写入龙头个股数据（从原始 JSON 获取完整信息）
        limit_up_path = data_lake_dir / 'limit_up_filter.json'
        dragon_tiger_path = data_lake_dir / 'dragon_tiger_list.json'
        risk_monitor_path = data_lake_dir / 'risk_monitor_list.json'
        
        if limit_up_path.exists():
            with open(limit_up_path, 'r', encoding='utf-8') as f:
                limit_up_json = json.load(f)
            
            stocks = limit_up_json.get('data', {}).get('stocks', [])
            if stocks:
                # 构建监管股票集合
                stock_regulation = set()
                if risk_monitor_path.exists():
                    with open(risk_monitor_path, 'r', encoding='utf-8') as f:
                        risk_data = json.load(f)
                    for stock in risk_data.get('data', []):
                        code = stock.get('code', '')
                        if code:
                            stock_regulation.add(code)
                
                # 构建机构买入股票集合
                stock_institution_buy = {}
                if dragon_tiger_path.exists():
                    with open(dragon_tiger_path, 'r', encoding='utf-8') as f:
                        dragon_data = json.load(f)
                    for record in dragon_data.get('data', []):
                        code = record.get('stockCode', '')
                        lhb_branch = record.get('lhbBranch', {})
                        buy_branches = lhb_branch.get('buyBranches', [])
                        has_institution = any(b.get('branchName') == '机构专用' for b in buy_branches)
                        if has_institution and code:
                            stock_institution_buy[code] = True
                
                stock_records = []
                for stock in stocks:
                    stock_code = stock.get('code', '')
                    tags = stock.get('tags', [])
                    
                    stock_records.append({
                        'trade_date': target_date,
                        'stock_code': stock_code,
                        'stock_name': stock.get('name', ''),
                        'continue_num': stock.get('continue_num', 0),
                        'order_amount': stock.get('order_amount', 0),
                        'turnover_rate': stock.get('turnover_rate', 0),
                        'total_market_cap': stock.get('total_market_cap', 0),
                        'is_regulation': stock_code in stock_regulation,
                        'is_institution_buy': stock_code in stock_institution_buy,
                        'leader_tag': ','.join(tags) if tags else '',
                        'reason_type': stock.get('reason_type', ''),
                        'limit_up_type': stock.get('limit_up_type', ''),
                    })
                
                df_stocks = pl.DataFrame(stock_records)
                self.manager.upsert_stocks(df_stocks)
                logger.info(f"Persisted {len(df_stocks)} dragon stocks for {target_date}")
    
    def get_latest_brief(self, target_date: str) -> str:
        """获取最新的 AI 简报内容"""
        # 简报文件在 cleaned_data 目录下
        brief_path = self.cleaned_data_dir / target_date / "ai_daily_brief.txt"
        if brief_path.exists():
            return brief_path.read_text(encoding='utf-8')
        return ""

    def get_sentiment_score(self, target_date: str) -> Dict[str, Any]:
        """
        获取指定日期的综合情绪评分（结构化数据）

        从 DB 读取已入库的市场情绪数据，并补全 cycle_phase / risk_level / summary 等扩展字段。
        若该日尚未入库，则尝试从原始 JSON 实时分析，避免前端空白。

        Args:
            target_date: 目标日期 (YYYY-MM-DD)

        Returns:
            Dict: 综合情绪数据
        """
        default_empty: Dict[str, Any] = {
            "trade_date": target_date,
            "has_data": False,
            "summary": "暂无数据",
            "cycle_phase": "震荡期",
            "cycle_reasons": [],
            "risk_level": "风险可控",
            "sentiment_score": 50.0,
            "promotion_1to2": 0.0,
            "promotion_2to3": 0.0,
            "promotion_high": 0.0,
            "theme_continuity": 0.0,
            "theme_flow": "无主线",
            "limit_up_count": 0,
            "limit_down_count": 0,
            "broken_ratio": 0.0,
            "max_height": 0,
            "rise_count": 0,
            "fall_count": 0,
            "main_themes": "",
        }

        try:
            df = self.manager.get_market_sentiment(target_date, target_date)
        except Exception as e:
            logger.error(f"get_market_sentiment failed for {target_date}: {e}")
            df = None

        if df is None or df.is_empty():
            return default_empty

        # polars DataFrame → dict
        try:
            row = df.to_dicts()[0]
        except Exception:
            return default_empty

        # 拆分 cycle_reasons（用 | 分隔存储）
        reasons_raw = row.get("cycle_reasons") or ""
        reasons = [r for r in str(reasons_raw).split("|") if r] if reasons_raw else []

        return {
            "trade_date": target_date,
            "has_data": True,
            "summary": row.get("summary") or "震荡",
            "cycle_phase": row.get("cycle_phase") or "震荡期",
            "cycle_reasons": reasons,
            "risk_level": row.get("risk_level") or "风险可控",
            "sentiment_score": float(row.get("sentiment_score") or 50.0),
            "promotion_1to2": float(row.get("promotion_1to2") or 0.0),
            "promotion_2to3": float(row.get("promotion_2to3") or 0.0),
            "promotion_high": float(row.get("promotion_high") or 0.0),
            "theme_continuity": float(row.get("theme_continuity") or 0.0),
            "theme_flow": row.get("theme_flow") or "无主线",
            "limit_up_count": int(row.get("limit_up_count") or 0),
            "limit_down_count": int(row.get("limit_down_count") or 0),
            "broken_ratio": float(row.get("broken_ratio") or 0.0),
            "max_height": int(row.get("max_height") or 0),
            "rise_count": int(row.get("rise_count") or 0),
            "fall_count": int(row.get("fall_count") or 0),
            "main_themes": row.get("main_themes") or "",
        }

    def get_sentiment_score_history(self, start_date: str, end_date: str) -> List[Dict[str, Any]]:
        """
        获取一段时间内的综合情绪评分历史

        Args:
            start_date: 开始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)

        Returns:
            List[Dict]: 每日情绪数据列表
        """
        try:
            df = self.manager.get_market_sentiment(start_date, end_date)
        except Exception as e:
            logger.error(f"get_market_sentiment history failed: {e}")
            return []

        if df is None or df.is_empty():
            return []

        try:
            rows = df.to_dicts()
        except Exception:
            return []

        results: List[Dict[str, Any]] = []
        for row in rows:
            reasons_raw = row.get("cycle_reasons") or ""
            reasons = [r for r in str(reasons_raw).split("|") if r] if reasons_raw else []
            results.append({
                "trade_date": str(row.get("trade_date")),
                "sentiment_score": float(row.get("sentiment_score") or 50.0),
                "cycle_phase": row.get("cycle_phase") or "震荡期",
                "risk_level": row.get("risk_level") or "风险可控",
                "summary": row.get("summary") or "震荡",
                "theme_flow": row.get("theme_flow") or "无主线",
                "limit_up_count": int(row.get("limit_up_count") or 0),
                "limit_down_count": int(row.get("limit_down_count") or 0),
                "broken_ratio": float(row.get("broken_ratio") or 0.0),
                "max_height": int(row.get("max_height") or 0),
                "promotion_1to2": float(row.get("promotion_1to2") or 0.0),
                "cycle_reasons": reasons,
            })
        return results

    def send_to_feishu(self, target_date: str) -> tuple[bool, str]:
        """
        触发飞书推送

        Returns:
            (success, message)
        """
        # 动态导入quant目录下的combined模块获取FeishuPush类
        import importlib.util
        spec = importlib.util.spec_from_file_location("combined", str(self.cleaner_path))
        combined_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(combined_module)
        FeishuPush = combined_module.FeishuPush

        webhook_url = getattr(Config, 'FEISHU_WEBHOOK', "")
        if not webhook_url:
            return False, "Feishu Webhook not configured"

        brief_content = self.get_latest_brief(target_date)
        if not brief_content:
            return False, "Brief content is empty"

        try:
            pusher = FeishuPush(webhook_url)
            markdown = pusher.txt_to_markdown(brief_content)
            success = pusher.push_markdown(markdown)
            return success, "Pushed to Feishu" if success else "Failed to push"
        except Exception as e:
            return False, str(e)
    
    def subscribe_job_logs(self, job_id: str, callback: Callable[[Dict], None]):
        """订阅任务日志流"""
        if job_id in self._active_captures:
            self._active_captures[job_id].subscribe(callback)
            return True
        
        # 如果任务已完成，直接发送历史日志
        job = job_manager.get_job(job_id)
        if job:
            for log in job.logs:
                callback(log)
        return False
    
    def get_job_status(self, job_id: str) -> Optional[Dict]:
        """获取任务状态"""
        job = job_manager.get_job(job_id)
        return job.to_dict() if job else None
    
    def get_bubble_matrix_data(self, target_date: str) -> Dict:
        """
        获取涨停强度气泡图数据
        
        从原始JSON数据文件中读取涨停股票的详细信息，
        包括封板时间、市值等用于气泡图展示的数据
        
        Args:
            target_date: 目标日期 (YYYY-MM-DD)
            
        Returns:
            Dict: 包含气泡数据列表
        """
        import json
        from datetime import datetime
        
        # 读取原始数据文件
        ladder_detail_path = self.data_lake_dir / target_date / "ladder_hierarchy_detail.json"
        limit_up_path = self.data_lake_dir / target_date / "limit_up_filter.json"
        
        bubbles = []
        
        # 从 limit_up_filter.json 获取基础数据
        if limit_up_path.exists():
            with open(limit_up_path, 'r', encoding='utf-8') as f:
                limit_up_data = json.load(f)
            
            stocks = limit_up_data.get('data', {}).get('stocks', [])
            
            for stock in stocks:
                # 解析封板时间（Unix timestamp）
                first_limit_time = stock.get('first_limit_up_time', 0)
                if first_limit_time:
                    try:
                        dt = datetime.fromtimestamp(int(first_limit_time))
                        # 转换为分钟数（从开盘时间 9:30 开始计算）
                        minutes_from_open = (dt.hour * 60 + dt.minute) - (9 * 60 + 30)
                        limit_up_minutes = max(0, minutes_from_open)
                    except:
                        limit_up_minutes = 240  # 默认尾盘封板
                else:
                    limit_up_minutes = 240
                
                # 市值（转换为亿元）
                market_cap = stock.get('total_market_cap', 0) / 1e8
                
                # 判断象限
                # X轴：封板时间（早封板 = 低值，晚封板 = 高值）
                # Y轴：市值（小市值 = 低值，大市值 = 高值）
                is_early = limit_up_minutes < 60  # 10:30前封板
                is_large_cap = market_cap > 50  # 50亿以上为大市值
                
                if is_early and is_large_cap:
                    quadrant = 1  # 权重股（早封板+大市值）
                    quadrant_name = "权重股"
                elif not is_early and is_large_cap:
                    quadrant = 2  # 跟风股（晚封板+大市值）
                    quadrant_name = "跟风股"
                elif not is_early and not is_large_cap:
                    quadrant = 3  # 题材股（晚封板+小市值）
                    quadrant_name = "题材股"
                else:
                    quadrant = 4  # 强势股（早封板+小市值）
                    quadrant_name = "强势股"
                
                bubbles.append({
                    'stock_code': stock.get('code', ''),
                    'stock_name': stock.get('name', ''),
                    'continue_num': stock.get('continue_num', 0),
                    'market_cap': round(market_cap, 2),
                    'limit_up_time': limit_up_minutes,
                    'limit_up_time_str': f"{9 + (30 + limit_up_minutes) // 60}:{(30 + limit_up_minutes) % 60:02d}" if limit_up_minutes < 240 else "尾盘",
                    'order_amount': stock.get('order_amount', 0),
                    'turnover_rate': stock.get('turnover_rate', 0),
                    'quadrant': quadrant,
                    'quadrant_name': quadrant_name,
                    'theme': stock.get('jiuyangongshe_category_name', ''),
                    'tags': stock.get('tags', [])
                })
        
        return {
            'date': target_date,
            'bubbles': bubbles,
            'quadrant_labels': {
                1: '权重股（早封板+大市值）',
                2: '跟风股（晚封板+大市值）',
                3: '题材股（晚封板+小市值）',
                4: '强势股（早封板+小市值）'
            }
        }
    
    def get_theme_flow_data(self, start_date: str, end_date: str) -> Dict:
        """
        获取题材流向数据
        
        分析不同交易日之间题材板块的资金流向和强度变化，
        生成用于桑基图展示的节点和连接数据
        
        Args:
            start_date: 开始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)
            
        Returns:
            Dict: 包含 nodes 和 links 的数据
        """
        import json
        from datetime import datetime, timedelta
        
        nodes = []
        links = []
        node_index_map = {}  # 用于快速查找节点索引
        
        # 遍历日期范围
        current_date = datetime.strptime(start_date, '%Y-%m-%d')
        end_dt = datetime.strptime(end_date, '%Y-%m-%d')
        
        prev_themes = {}  # 前一天的题材数据
        
        while current_date <= end_dt:
            date_str = current_date.strftime('%Y-%m-%d')
            
            # 读取当天的 sector_heat_stats.json
            sector_heat_path = self.data_lake_dir / date_str / "sector_heat_stats.json"
            
            if sector_heat_path.exists():
                with open(sector_heat_path, 'r', encoding='utf-8') as f:
                    sector_data = json.load(f)
                
                themes = sector_data.get('data', [])
                
                # 为每个题材创建节点
                for theme in themes:
                    theme_name = theme.get('name', '')
                    count = theme.get('count', 0)
                    
                    if count < 2:  # 忽略涨停数少于2的题材
                        continue
                    
                    node_key = f"{date_str}_{theme_name}"
                    
                    # 创建节点
                    node = {
                        'name': theme_name,
                        'date': date_str,
                        'count': count,
                        'is_main': count >= 5  # 涨停数>=5为主流题材
                    }
                    
                    node_index = len(nodes)
                    nodes.append(node)
                    node_index_map[node_key] = node_index
                    
                    # 如果前一天有相同题材，创建连接
                    if theme_name in prev_themes:
                        prev_key = f"{prev_themes[theme_name]['date']}_{theme_name}"
                        if prev_key in node_index_map:
                            links.append({
                                'source': node_index_map[prev_key],
                                'target': node_index,
                                'value': min(prev_themes[theme_name]['count'], count)
                            })
                
                # 更新 prev_themes
                prev_themes = {}
                for theme in themes:
                    theme_name = theme.get('name', '')
                    count = theme.get('count', 0)
                    if count >= 2:
                        prev_themes[theme_name] = {
                            'date': date_str,
                            'count': count
                        }
            
            current_date += timedelta(days=1)
        
        return {
            'nodes': nodes,
            'links': links
        }
