# server/asgi_socketio_handlers.py
"""
ASGI SocketIO 事件处理器

将原有的 Flask-SocketIO 事件处理器转换为异步版本
"""
import os
import sys
from pathlib import Path

_project_root = Path(__file__).parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from typing import Dict, Any
import asyncio
import base64

from server.performance_utils import pack_backtest_data
from server.utils.binary_packer import pack_backtest_result, estimate_size

try:
    import msgpack
    MSGPACK_AVAILABLE = True
except ImportError:
    MSGPACK_AVAILABLE = False
    msgpack = None


def register_handlers(sio):
    """
    注册所有 SocketIO 事件处理器（异步版本）
    
    Args:
        sio: socketio.AsyncServer 实例
    """
    print(f"\n{'='*80}\n[SOCKET.IO] Registering event handlers...\n{'='*80}\n")
    
    import asyncio
    from config.logger import get_logger
    logger = get_logger(__name__)
    
    # 设置全局 Socket.IO 实例和事件循环
    try:
        from server.socketio_manager import set_global_socketio
        set_global_socketio(sio)
        logger.info("[SocketIO Manager] 全局 Socket.IO 实例已设置")
    except Exception as e:
        logger.warning(f"[SocketIO Manager] 无法设置全局实例: {e}")
    
    active_backtests: Dict[str, Any] = {}
    
    @sio.event
    async def connect(sid, environ):
        """客户端连接事件"""
        try:
            from server.socketio_manager import set_global_loop
            set_global_loop(asyncio.get_running_loop())
            print(f"\n{'='*80}\n[SOCKET.IO] Client connected! sid={sid}\n{'='*80}\n")
            logger.debug(f"Socket.IO 客户端已连接: {sid}")
        except Exception as e:
            logger.error(f"Socket.IO 连接处理失败: {e}")
    
    @sio.event
    async def disconnect(sid):
        """客户端断开事件"""
        try:
            logger.debug(f"Socket.IO 客户端已断开: {sid}")
            if sid in active_backtests:
                stop_event = active_backtests[sid]
                stop_event.set()
                del active_backtests[sid]
        except Exception as e:
            logger.error(f"Socket.IO 断开处理失败: {e}")
    
    @sio.event
    async def run_streaming_backtest(sid, data):
        """
        运行流式回测（异步版本）
        
        【核心修复】实现真正的流式传输：
        - 使用生产者-消费者同步机制
        - 回测线程每次放入事件后等待主事件循环确认发送
        - 确保前端实时看到进度更新
        """
        print(f"\n\n{'='*80}\n[BACKTEST] Event handler called! sid={sid}\n{'='*80}\n\n")
        
        from threading import Event, Semaphore
        from server.visualization_api import BacktestVisualizationAPI
        from config.logger import get_logger
        
        logger = get_logger(__name__)
        logger.debug(f"收到 run_streaming_backtest 事件: sid={sid}, data_keys={list(data.keys()) if isinstance(data, dict) else str(type(data))}")
        
        try:
            strategy_name = data.get('strategy_name')
            start_date = data.get('start_date')
            end_date = data.get('end_date')
            benchmark_code = data.get('benchmark_code', '000300')
            profile_id = data.get('profile_id')
            override_params = data.get('override_params') or {}
            
            if not all([strategy_name, start_date, end_date]):
                await sio.emit('backtest_error', {
                    "message": "缺少必需参数: strategy_name, start_date, end_date"
                }, room=sid)
                return
            
            effective_params = None
            if profile_id is not None:
                try:
                    from core.profiles.profile_repository import get_profile as load_profile
                    profile = load_profile(int(profile_id))
                    if profile is None:
                        await sio.emit('backtest_error', {
                            "message": f"Profile {profile_id} 不存在"
                        }, room=sid)
                        return
                    params_from_profile = profile.get("params") or {}
                    if not isinstance(params_from_profile, dict):
                        params_from_profile = {}
                    if not isinstance(override_params, dict):
                        override_params = {}
                    effective_params = {**params_from_profile, **override_params}
                except Exception as e:
                    await sio.emit('backtest_error', {
                        "message": f"加载 Profile 失败: {e}"
                    }, room=sid)
                    return
            else:
                effective_params = override_params if override_params else None
            
            stop_event = Event()
            active_backtests[sid] = stop_event
            
            await sio.emit('request_received', {"message": "回测请求已收到"}, room=sid)
            
            main_loop = asyncio.get_event_loop()
            
            from queue import Queue, Empty
            event_queue = Queue()
            
            emit_semaphore = Semaphore(0)
            
            batch_buffer = []
            BATCH_SIZE = 5
            FLUSH_INTERVAL = 0.02
            
            def run_backtest():
                import time
                import sys
                try:
                    logger.info(f"开始流式回测: {strategy_name} | {start_date}~{end_date} | 基准: {benchmark_code}")
                    sys.stderr.write(f"[DEBUG asgi_socketio_handlers] 创建 BacktestVisualizationAPI\n")
                    sys.stderr.flush()
                    
                    api = BacktestVisualizationAPI()
                    api._ensure_initialized()
                    
                    sys.stderr.write(f"[DEBUG asgi_socketio_handlers] 调用 api.stream_backtest\n")
                    sys.stderr.flush()
                    
                    update_count = 0
                    last_flush_time = time.time()
                    generator = api.stream_backtest(
                        strategy_name=strategy_name,
                        start_date=start_date,
                        end_date=end_date,
                        benchmark_code=benchmark_code,
                        stop_event=stop_event,
                        params=effective_params
                    )
                    
                    immediate_events = {
                        'error', 'cancelled', 'backtest_start', 'initializing', 
                        'initialized', 'final_metrics', 'risk_data', 'stream_complete',
                        'progress', 'backtest_end'
                    }
                    
                    def emit_and_wait(event_name, payload, wait=True):
                        """
                        放入事件并等待主事件循环发送完成
                        
                        【关键】wait=True 时会阻塞直到事件被发送
                        这确保了真正的流式传输
                        """
                        nonlocal batch_buffer, last_flush_time
                        batch_buffer.append((event_name, payload))
                        
                        current_time = time.time()
                        should_flush = (
                            len(batch_buffer) >= BATCH_SIZE or
                            current_time - last_flush_time >= FLUSH_INTERVAL or
                            event_name in ['backtest_start', 'stream_complete', 'backtest_error', 'progress']
                        )
                        
                        if should_flush:
                            event_queue.put(batch_buffer.copy())
                            batch_buffer.clear()
                            last_flush_time = current_time
                            
                            if wait:
                                emit_semaphore.acquire()
                    
                    for update in generator:
                        if stop_event.is_set():
                            if batch_buffer:
                                event_queue.put(batch_buffer.copy())
                                batch_buffer.clear()
                                emit_semaphore.acquire()
                            break
                        
                        update_count += 1
                        t = update.get('type')
                        update_data = update.get('data', {})
                        
                        if t == 'error':
                            emit_and_wait('backtest_error', update_data)
                            return
                        elif t == 'cancelled':
                            emit_and_wait('backtest_cancelled', update_data or {"message": "回测已取消"})
                            return
                        elif t == 'backtest_start':
                            logger.info(f"[BACKTEST] 发送 backtest_start 事件")
                            emit_and_wait('backtest_start', update_data)
                        elif t == 'progress':
                            emit_and_wait('progress', update_data, wait=False)
                        elif t == 'initializing':
                            emit_and_wait('initializing', update_data)
                        elif t == 'initialized':
                            emit_and_wait('initialized', update_data)
                        elif t == 'final_metrics':
                            emit_and_wait('metrics_update', update_data)
                        elif t == 'risk_data':
                            emit_and_wait('risk_update', update_data)
                        elif t == 'stream_complete':
                            logger.info("[STREAM] 发送 stream_complete 事件")
                            emit_and_wait('stream_complete', update_data, wait=True)
                        elif t == 'daily_equity':
                            # 【调试】记录发送的 daily_equity 事件
                            if update_count % 50 == 0 or update_count <= 3:
                                logger.info(f"[STREAM] 发送 daily_equity: date={update_data.get('date')}, equity={update_data.get('strategyReturn')}")
                            emit_and_wait('daily_equity', update_data, wait=True)
                        elif t == 'daily_equity_engine':
                            emit_and_wait('daily_equity', update_data, wait=True)
                        elif t == 'new_trade':
                            emit_and_wait('new_trade', update_data, wait=True)
                        elif t == 'new_trade_engine':
                            emit_and_wait('new_trade', update_data, wait=True)
                        else:
                            pass
                    
                    if batch_buffer:
                        event_queue.put(batch_buffer.copy())
                        batch_buffer.clear()
                        emit_semaphore.acquire()
                        
                except Exception as e:
                    import traceback
                    error_msg = f"{e}"
                    error_trace = traceback.format_exc()
                    logger.error(f"回测失败: {error_msg}")
                    logger.error(f"错误堆栈: {error_trace}")
                    try:
                        if batch_buffer:
                            event_queue.put(batch_buffer.copy())
                            batch_buffer.clear()
                        event_queue.put([('backtest_error', {"message": error_msg, "trace": error_trace[:500]})])
                        emit_semaphore.acquire()
                    except Exception as emit_e:
                        logger.error(f"发送错误事件失败: {emit_e}")
                finally:
                    try:
                        if 'api' in locals():
                            if hasattr(api, 'data_query') and hasattr(api.data_query, '_preloaded_data_pl'):
                                api.data_query._preloaded_data_pl = None
                            if hasattr(api, 'data_query') and hasattr(api.data_query, '_cache'):
                                api.data_query._cache.clear()
                            del api
                    except Exception as e:
                        logger.warning(f"清理 API 资源时出错: {e}")
                    
                    if sid in active_backtests:
                        del active_backtests[sid]
                    
                    logger.info(f"回测资源已清理")
            
            import concurrent.futures
            import threading
            
            backtest_done = threading.Event()
            
            def run_backtest_in_thread():
                try:
                    run_backtest()
                finally:
                    backtest_done.set()
            
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(run_backtest_in_thread)
                
                while not backtest_done.is_set() or not event_queue.empty():
                    try:
                        batch = event_queue.get(timeout=0.01)
                        for event_name, payload in batch:
                            await sio.emit(event_name, payload, room=sid)
                            logger.debug(f"[STREAM] 已发送 {event_name}")
                        emit_semaphore.release()
                    except Empty:
                        await asyncio.sleep(0.001)
                    
                    if future.done():
                        backtest_done.set()
            
        except Exception as e:
            logger.error(f"启动回测失败: {e}", exc_info=True)
            await sio.emit('backtest_error', {"message": str(e)}, room=sid)
            if sid in active_backtests:
                del active_backtests[sid]
    
    @sio.event
    async def cancel_streaming_backtest(sid, data=None):
        """取消回测（异步版本）"""
        if sid in active_backtests:
            stop_event = active_backtests[sid]
            stop_event.set()
            await sio.emit('backtest_cancel_ack', {"message": "正在停止回测..."}, room=sid)
            del active_backtests[sid]
        else:
            await sio.emit('backtest_cancel_ack', {"message": "当前没有正在运行的回测"}, room=sid)

    @sio.event
    async def request_kline(sid, data):
        """
        请求 K 线数据（异步版本）- 带性能监控
        """
        import time
        
        symbol_code = data.get('symbol_code')
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        request_id = data.get('request_id')
        
        total_start = time.perf_counter()

        print(f"\n{'='*80}\n[SOCKET] 收到 request_kline 请求\n{'='*80}")
        print(f"[SOCKET] symbol_code: {symbol_code}, start_date: {start_date}, end_date: {end_date}")
        print(f"[SOCKET] request_id: {request_id}")

        if not symbol_code:
            print(f"[SOCKET] 错误: 缺少 symbol_code")
            await sio.emit('kline_data', {"error": "缺少 symbol_code", "request_id": request_id}, room=sid)
            return

        logger.debug(f"[SOCKET] 请求 K 线 {symbol_code} | {start_date} ~ {end_date}")
        
        try:
            from server.visualization_api import BacktestVisualizationAPI
            
            def fetch_kline():
                print(f"[SOCKET] 开始获取 K 线数据...")
                t0 = time.perf_counter()
                api_instance = BacktestVisualizationAPI()
                t1 = time.perf_counter()
                history = api_instance.get_symbol_kline(symbol_code, start_date, end_date)
                t2 = time.perf_counter()
                symbol_name = api_instance.stock_info_map.get(api_instance._normalize_symbol_code(symbol_code), symbol_code)
                
                # 性能日志
                init_time = (t1 - t0) * 1000
                query_time = (t2 - t1) * 1000
                total_time = (t2 - t0) * 1000
                
                print(f"[PERF] API初始化: {init_time:.2f}ms")
                print(f"[PERF] 数据查询: {query_time:.2f}ms")
                print(f"[PERF] 总耗时: {total_time:.2f}ms")
                print(f"[SOCKET] 获取到 {len(history)} 条 K 线数据")
                
                return history, symbol_name, query_time

            loop = asyncio.get_event_loop()
            history, symbol_name, query_ms = await loop.run_in_executor(None, fetch_kline)

            total_ms = (time.perf_counter() - total_start) * 1000
            print(f"[PERF] 请求总耗时: {total_ms:.2f}ms (查询: {query_ms:.2f}ms)")
            print(f"[SOCKET] 发送 kline_data 响应, 数据条数: {len(history)}")
            
            await sio.emit('kline_data', {
                "request_id": request_id,
                "symbol_code": symbol_code,
                "symbolCode": symbol_code,
                "symbol_name": symbol_name,
                "data": history,
                "perf": {
                    "total_ms": round(total_ms, 2),
                    "query_ms": round(query_ms, 2)
                }
            }, room=sid)
            print(f"[SOCKET] 响应已发送\n{'='*80}\n")
        except Exception as e:
            print(f"[SOCKET] 处理 request_kline 失败: {e}")
            import traceback
            traceback.print_exc()
            logger.error(f"处理 request_kline 失败: {e}", exc_info=True)
            await sio.emit('kline_data', {
                "error": str(e),
                "request_id": request_id
            }, room=sid)

    logger.info("ASGI SocketIO 事件处理器已注册 (包含 request_kline)")
