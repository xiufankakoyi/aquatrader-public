"""
飞书机器人核心模块 (core/feishu_bot/bot.py)
基于飞书 SDK 实现 WebSocket 长连接，接收事件并响应
支持图片消息接收、下载、OCR 识别
"""
import io
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional, Callable, Dict, Any

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
    GetMessageResourceRequest,
)

from config.config import Config

logger = logging.getLogger(__name__)


class FeishuBot:
    """
    飞书机器人
    
    通过 WebSocket 长连接接收飞书事件，支持消息收发、图片下载、OCR 识别
    
    Usage:
        bot = FeishuBot()
        bot.on_message(my_handler)
        bot.start()
    """
    
    def __init__(
        self,
        app_id: Optional[str] = None,
        app_secret: Optional[str] = None,
        log_level: lark.LogLevel = lark.LogLevel.INFO
    ):
        """
        初始化飞书机器人
        
        Args:
            app_id: 飞书应用 App ID，不传则从配置读取
            app_secret: 飞书应用 App Secret，不传则从配置读取
            log_level: 日志级别
        """
        self.app_id = app_id or Config.FEISHU_APP_ID
        self.app_secret = app_secret or Config.FEISHU_APP_SECRET
        
        if not self.app_id or not self.app_secret:
            raise ValueError("飞书 App ID 和 App Secret 未配置")
        
        self.log_level = log_level
        self._client: Optional[lark.ws.Client] = None
        self._api_client: Optional[lark.Client] = None
        self._event_handler: Optional[lark.EventDispatcherHandler] = None
        self._message_handlers: list[Callable] = []
        self._event_handlers: Dict[str, Callable] = {}
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_ocr_time: float = 0
        self._ocr_lock = threading.Lock()
        
    def _build_event_handler(self) -> lark.EventDispatcherHandler:
        """
        构建事件处理器
        
        Returns:
            事件处理器实例
        """
        builder = lark.EventDispatcherHandler.builder("", "")
        
        builder.register_p2_im_message_receive_v1(self._handle_message_receive)
        
        for event_type, handler in self._event_handlers.items():
            builder.register_p1_customized_event(event_type, handler)
        
        return builder.build()
    
    def _handle_message_receive(self, data: lark.im.v1.P2ImMessageReceiveV1) -> None:
        """
        处理接收消息事件
        
        Args:
            data: 消息事件数据
        """
        try:
            event = data.event
            message = event.message
            
            msg_info = {
                'message_id': message.message_id,
                'chat_id': message.chat_id,
                'chat_type': message.chat_type,
                'content': message.content,
                'content_type': message.message_type,
                'sender': event.sender,
                'create_time': message.create_time,
            }
            
            logger.info(f"收到消息: chat_id={msg_info['chat_id']}, type={msg_info['content_type']}")
            
            for handler in self._message_handlers:
                try:
                    handler(msg_info, self)
                except Exception as e:
                    logger.error(f"消息处理器执行失败: {e}", exc_info=True)
                    
        except Exception as e:
            logger.error(f"处理消息事件失败: {e}", exc_info=True)
    
    def on_message(self, handler: Callable[[Dict[str, Any], 'FeishuBot'], None]) -> None:
        """
        注册消息处理器
        
        Args:
            handler: 消息处理函数，接收 (message_info, bot) 参数
        """
        self._message_handlers.append(handler)
        
    def on_event(self, event_type: str, handler: Callable) -> None:
        """
        注册自定义事件处理器
        
        Args:
            event_type: 事件类型，如 'out_approval'
            handler: 事件处理函数
        """
        self._event_handlers[event_type] = handler
    
    def _ensure_api_client(self) -> lark.Client:
        """
        确保 API 客户端已初始化
        
        Returns:
            lark.Client 实例
        """
        if not self._api_client:
            self._api_client = lark.Client.builder() \
                .app_id(self.app_id) \
                .app_secret(self.app_secret) \
                .build()
        return self._api_client
    
    def download_image(self, message_id: str, file_key: str) -> Optional[bytes]:
        """
        下载消息中的图片
        
        Args:
            message_id: 消息 ID
            file_key: 图片文件 key（通常是 image_key）
            
        Returns:
            图片二进制数据，失败返回 None
        """
        client = self._ensure_api_client()
        
        try:
            req = GetMessageResourceRequest.builder() \
                .message_id(message_id) \
                .file_key(file_key) \
                .type("file") \
                .build()
            
            resp = client.im.v1.message_resource.get(req)
            
            if resp.success():
                if resp.file:
                    return resp.file.read()
                logger.error(f"下载图片失败: 响应文件为空")
                return None
            else:
                logger.error(f"下载图片失败: code={resp.code}, msg={resp.msg}")
                return None
                
        except Exception as e:
            logger.error(f"下载图片异常: {e}", exc_info=True)
            return None
    
    def download_image_to_file(self, message_id: str, file_key: str, save_path: Optional[str] = None) -> Optional[str]:
        """
        下载图片到文件
        
        Args:
            message_id: 消息 ID
            file_key: 图片文件 key
            save_path: 保存路径，不传则使用临时文件
            
        Returns:
            文件路径，失败返回 None
        """
        image_data = self.download_image(message_id, file_key)
        if not image_data:
            return None
        
        try:
            if save_path:
                path = Path(save_path)
                path.parent.mkdir(parents=True, exist_ok=True)
            else:
                temp_dir = Path(Config.DATA_DIR) / 'temp' / 'feishu_images'
                temp_dir.mkdir(parents=True, exist_ok=True)
                path = temp_dir / f"{file_key}.jpg"
            
            with open(path, 'wb') as f:
                f.write(image_data)
            
            logger.info(f"图片已保存: {path}")
            return str(path)
            
        except Exception as e:
            logger.error(f"保存图片失败: {e}", exc_info=True)
            return None
    
    def ocr_recognize_local(self, image_data: bytes) -> Optional[Dict[str, Any]]:
        """
        本地 OCR 文字识别（使用 EasyOCR，准确率更高）
        
        Args:
            image_data: 图片二进制数据
            
        Returns:
            OCR 识别结果字典，失败返回 None
        """
        try:
            import cv2
            import numpy as np
            import easyocr
            
            np_array = np.frombuffer(image_data, np.uint8)
            img = cv2.imdecode(np_array, cv2.IMREAD_COLOR)
            
            if img is None:
                logger.error("无法解码图片数据")
                return None
            
            logger.info("初始化 EasyOCR 引擎...")
            reader = easyocr.Reader(['ch_sim', 'en'], gpu=False)
            
            logger.info("开始 OCR 识别...")
            result = reader.readtext(img)
            
            if result is None or len(result) == 0:
                logger.info("OCR 未识别到文字")
                return {'text_lines': [], 'full_text': ''}
            
            ocr_result = {
                'text_lines': [],
                'full_text': '',
            }
            
            for item in result:
                bbox = item[0]
                text = item[1]
                confidence = float(item[2])
                
                ocr_result['text_lines'].append({
                    'text': text,
                    'confidence': confidence,
                    'position': bbox,
                })
            
            ocr_result['full_text'] = '\n'.join(
                line['text'] for line in ocr_result['text_lines']
            )
            
            logger.info(f"EasyOCR 识别成功，共 {len(ocr_result['text_lines'])} 行文字")
            return ocr_result
            
        except ImportError:
            logger.error("EasyOCR 未安装，请运行: pip install easyocr")
            return None
        except Exception as e:
            logger.error(f"本地 OCR 识别异常: {e}", exc_info=True)
            return None
    
    def ocr_recognize_from_message(self, message_id: str, image_key: str) -> Optional[Dict[str, Any]]:
        """
        从消息图片进行 OCR 识别（使用本地 OCR）
        
        Args:
            message_id: 消息 ID
            image_key: 图片 key
            
        Returns:
            OCR 识别结果
        """
        image_data = self.download_image(message_id, image_key)
        if not image_data:
            logger.error("下载图片失败")
            return None
        
        return self.ocr_recognize_local(image_data)
    
    def send_text(
        self,
        receive_id: str,
        content: str,
        receive_id_type: str = "chat_id"
    ) -> bool:
        """
        发送文本消息
        
        Args:
            receive_id: 接收者 ID
            content: 文本内容
            receive_id_type: 接收者类型 (chat_id, open_id, user_id)
            
        Returns:
            是否发送成功
        """
        client = self._ensure_api_client()
            
        try:
            req = CreateMessageRequest.builder() \
                .receive_id_type(receive_id_type) \
                .request_body(CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type("text")
                    .content(json.dumps({"text": content}, ensure_ascii=False))
                    .build()) \
                .build()
            
            resp = client.im.v1.message.create(req)
            
            if resp.success():
                logger.info(f"消息发送成功: {receive_id}")
                return True
            else:
                logger.error(f"消息发送失败: code={resp.code}, msg={resp.msg}")
                return False
                
        except Exception as e:
            logger.error(f"发送消息异常: {e}", exc_info=True)
            return False
    
    def send_markdown(
        self,
        receive_id: str,
        title: str,
        content: str,
        receive_id_type: str = "chat_id"
    ) -> bool:
        """
        发送 Markdown 消息（卡片形式）
        
        Args:
            receive_id: 接收者 ID
            title: 卡片标题
            content: Markdown 内容
            receive_id_type: 接收者类型
            
        Returns:
            是否发送成功
        """
        client = self._ensure_api_client()
            
        try:
            card_content = lark.messageCard.defaultCard(
                title=title,
                content=content
            )
            
            req = CreateMessageRequest.builder() \
                .receive_id_type(receive_id_type) \
                .request_body(CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type("interactive")
                    .content(card_content)
                    .build()) \
                .build()
            
            resp = client.im.v1.message.create(req)
            
            if resp.success():
                logger.info(f"Markdown 消息发送成功: {receive_id}")
                return True
            else:
                logger.error(f"Markdown 消息发送失败: code={resp.code}, msg={resp.msg}")
                return False
                
        except Exception as e:
            logger.error(f"发送 Markdown 消息异常: {e}", exc_info=True)
            return False
    
    def send_json_card(
        self,
        receive_id: str,
        title: str,
        json_content: Dict[str, Any],
        receive_id_type: str = "chat_id"
    ) -> bool:
        """
        发送 JSON 格式的卡片消息
        
        Args:
            receive_id: 接收者 ID
            title: 卡片标题
            json_content: JSON 内容字典
            receive_id_type: 接收者类型
            
        Returns:
            是否发送成功
        """
        formatted_json = json.dumps(json_content, ensure_ascii=False, indent=2)
        
        content = f"```json\n{formatted_json}\n```"
        
        return self.send_markdown(receive_id, title, content, receive_id_type)
    
    def reply_text(self, message_id: str, content: str) -> bool:
        """
        回复文本消息
        
        Args:
            message_id: 原消息 ID
            content: 回复内容
            
        Returns:
            是否发送成功
        """
        client = self._ensure_api_client()
            
        try:
            req = ReplyMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(ReplyMessageRequestBody.builder()
                    .msg_type("text")
                    .content(json.dumps({"text": content}, ensure_ascii=False))
                    .build()) \
                .build()
            
            resp = client.im.v1.message.reply(req)
            
            if resp.success():
                logger.info(f"消息回复成功: {message_id}")
                return True
            else:
                logger.error(f"消息回复失败: code={resp.code}, msg={resp.msg}")
                return False
                
        except Exception as e:
            logger.error(f"回复消息异常: {e}", exc_info=True)
            return False
    
    def reply_markdown(self, message_id: str, title: str, content: str) -> bool:
        """
        回复 Markdown 卡片消息
        
        Args:
            message_id: 原消息 ID
            title: 卡片标题
            content: Markdown 内容
            
        Returns:
            是否发送成功
        """
        client = self._ensure_api_client()
        
        try:
            card_content = lark.messageCard.defaultCard(
                title=title,
                content=content
            )
            
            req = ReplyMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(ReplyMessageRequestBody.builder()
                    .msg_type("interactive")
                    .content(card_content)
                    .build()) \
                .build()
            
            resp = client.im.v1.message.reply(req)
            
            if resp.success():
                logger.info(f"Markdown 回复成功: {message_id}")
                return True
            else:
                logger.error(f"Markdown 回复失败: code={resp.code}, msg={resp.msg}")
                return False
                
        except Exception as e:
            logger.error(f"回复 Markdown 消息异常: {e}", exc_info=True)
            return False
    
    def _run_client(self) -> None:
        """
        在线程中运行 WebSocket 客户端
        """
        try:
            self._event_handler = self._build_event_handler()
            
            self._api_client = lark.Client.builder() \
                .app_id(self.app_id) \
                .app_secret(self.app_secret) \
                .build()
            
            self._client = lark.ws.Client(
                self.app_id,
                self.app_secret,
                event_handler=self._event_handler,
                log_level=self.log_level
            )
            
            logger.info("飞书机器人 WebSocket 连接启动...")
            self._client.start()
            
        except Exception as e:
            logger.error(f"WebSocket 客户端运行异常: {e}", exc_info=True)
            self._running = False
    
    def start(self, blocking: bool = True) -> None:
        """
        启动机器人
        
        Args:
            blocking: 是否阻塞主线程
        """
        if self._running:
            logger.warning("机器人已在运行中")
            return
            
        self._running = True
        
        if blocking:
            self._run_client()
        else:
            self._thread = threading.Thread(target=self._run_client, daemon=True)
            self._thread.start()
    
    def stop(self) -> None:
        """
        停止机器人
        """
        self._running = False
        if self._client:
            logger.info("飞书机器人已停止")
    
    @property
    def is_running(self) -> bool:
        """
        检查机器人是否在运行
        """
        return self._running
