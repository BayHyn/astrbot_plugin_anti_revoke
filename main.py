import asyncio
import time
import pickle
import traceback
import json 
from pathlib import Path
from typing import List, Dict
import aiohttp
import os # 用于文件删除
from astrbot.api import logger
from astrbot.api import AstrBotConfig
from astrbot.api.star import StarTools
from astrbot.api import message_components as Comp
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.platform import MessageType
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.core.star.filter.platform_adapter_type import PlatformAdapterType
from aiocqhttp.exceptions import ActionFailed 


# ====================================
# 工具函数 (必须在类外部)
# ====================================

def get_private_unified_msg_origin(user_id: str, platform: str = "aiocqhttp") -> str:
    """获取私聊的统一消息来源格式，用于 context.send_message API"""
    return f"{platform}:FriendMessage:{user_id}"

async def delayed_delete(delay: int, path: Path):
    """延迟删除文件"""
    await asyncio.sleep(delay)
    try:
        path.unlink(missing_ok=True)
        logger.debug(f"[AntiRevoke] 缓存清理：已删除过期文件 {path.name}")
    except Exception:
        logger.error(f"[AntiRevoke] 删除文件失败 ({path}): {traceback.format_exc()}")

async def _cleanup_local_files(file_paths: List[str]):
    """
    异步清理本地临时文件。
    """
    if not file_paths:
        return
    
    # 等待短暂时间，确保文件上传完成 (尽管发送请求已完成，但多等待一秒更安全)
    await asyncio.sleep(1) 
    
    for abs_path in file_paths:
        try:
            os.remove(abs_path)
            logger.debug(f"[AntiRevoke] 🗑️ 已清理本地图片: {os.path.basename(abs_path)}")
        except Exception as e:
            logger.error(f"[AntiRevoke] ❌ 清理本地图片失败 ({abs_path}): {e}")


def get_value(obj, key, default=None):
    """安全地获取属性/字典值"""
    try:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)
    except Exception:
        return default

def _serialize_components(components: list) -> str:
    """将 Nakuru 组件列表序列化为 JSON 字符串，便于日志输出。"""
    serialized_list = []
    for comp in components:
        try:
            comp_dict = {k: v for k, v in comp.__dict__.items() if not k.startswith('_')}
            comp_dict['type'] = getattr(comp.type, 'name', 'unknown')
            serialized_list.append(comp_dict)
        except:
            serialized_list.append(f"<{str(comp)}>")

    try:
        return json.dumps(serialized_list, indent=2, ensure_ascii=False)
    except:
        return f"无法序列化: {str(serialized_list)}"


def _convert_to_gocq_message(components: list) -> List[Dict]:
    """
    将 Nakuru 格式的组件列表手动转换为 Go-CQHTTP 要求的 Message Segment List (List[Dict]).
    """
    gocq_list = []
    for comp in components:
        comp_type = getattr(comp.type, 'name', 'unknown').lower()
        
        if comp_type in ['plain', 'text']:
            text = getattr(comp, 'text', '')
            if text:
                gocq_list.append({"type": "text", "data": {"text": text}})
        
        elif comp_type == 'image':
            # 优先使用 file (本地 ID/路径)，其次 URL
            image_source = getattr(comp, 'file', None)
            if not image_source:
                image_source = getattr(comp, 'url', None)

            if image_source:
                gocq_list.append({"type": "image", "data": {"file": image_source}})
            else:
                logger.warning("[AntiRevoke] 转换图片时，未找到可用的 file 或 url 源。")
                
        elif comp_type == 'at':
            qq_id = str(getattr(comp, 'qq', 'all'))
            gocq_list.append({"type": "at", "data": {"qq": qq_id}})
            
        else:
             gocq_list.append({"type": "text", "data": {"text": f"[{comp.type.name}]"}})
        
    return gocq_list


async def _download_and_cache_image(session: aiohttp.ClientSession, component: Comp.Image, temp_path: Path) -> str:
    """
    下载图片到本地，并返回Go-CQHTTP可用的绝对文件路径。
    返回 None 表示下载或保存失败。
    """
    image_url = getattr(component, 'url', None)
    if not image_url:
        return None
        
    file_extension = '.jpg'
    if image_url.lower().endswith('.png'):
        file_extension = '.png'
    
    file_name = f"forward_{int(time.time() * 1000)}{file_extension}"
    temp_file_path = temp_path / file_name

    try:
        logger.info(f"[AntiRevoke] 尝试下载图片到本地: {image_url}")
        
        async with session.get(image_url, timeout=15) as response:
            response.raise_for_status() 
            
            content_type = response.headers.get('Content-Type', '').lower()
            if 'image' not in content_type:
                logger.warning(f"[AntiRevoke] 下载 URL 返回类型非图片: {content_type}")
                return None
                
            image_bytes = await response.read()
            temp_file_path.write_bytes(image_bytes)

        logger.info(f"[AntiRevoke] 图片成功缓存到本地: {temp_file_path.name}")
        
        # 返回绝对路径，确保 Go-CQHTTP 可以在服务器上找到文件
        return str(temp_file_path.absolute())

    except Exception as e:
        logger.error(f"[AntiRevoke] ❌ 图片下载或保存失败 ({image_url}): {e}")
        # 清理可能残留的临时文件
        if temp_file_path.exists():
            os.remove(temp_file_path)
        return None


# ====================================
# 插件主体
# ====================================

@register(
    "astrbot_plugin_anti_revoke",  # 插件ID
    "Foolllll",                    # 作者名
    "监控撤回插件",                  # 插件显示名称
    "0.1",                         # 版本号
    "https://github.com/Foolllll-J/astrbot_plugin_anti_revoke", # 插件仓库地址
)
class AntiRevoke(Star):
    
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        
        self.monitor_groups = [str(g) for g in config.get("monitor_groups", []) or []]
        self.target_receivers = [str(r) for r in config.get("target_receivers", []) or []]
        
        self.instance_id = "AntiRevoke"
        self.cache_expiration_time = int(config.get("cache_expiration_time", 300))
        
        self.context = context 
        
        self.temp_path = Path(StarTools.get_data_dir()) / "anti_revoke_data"
        self.temp_path.mkdir(exist_ok=True)

        self._cleanup_cache_on_startup()
        
        logger.info("=" * 60)
        logger.info(f"[{self.instance_id}] 插件版本 0.1 已载入。")
        logger.info(f"[{self.instance_id}] 监控群列表 (monitor_groups): {self.monitor_groups}")
        logger.info(f"[{self.instance_id}] 通知目标列表 (target_receivers): {self.target_receivers}")
        logger.info(f"[{self.instance_id}] 缓存时间 (cache_expiration_time): {self.cache_expiration_time} 秒")
        logger.info("=" * 60)

    def _cleanup_cache_on_startup(self):
        """清理启动时超过有效期的缓存文件"""
        now_ms = int(time.time() * 1000)
        expired_count = 0
        for file in self.temp_path.glob("*.pkl"):
            try:
                file_create_time_ms = int(file.name.split('_')[0])
                if now_ms - file_create_time_ms > self.cache_expiration_time * 1000:
                    file.unlink(missing_ok=True)
                    expired_count += 1
            except Exception:
                pass
        logger.info(f"[{self.instance_id}] 缓存清理完成，移除了 {expired_count} 个过期文件。")
        
    async def terminate(self):
        logger.info(f"[{self.instance_id}] 插件已卸载/重载。")
        
    # --- 1. 消息缓存逻辑 ---
    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=20)
    async def handle_message_cache(self, event: AstrMessageEvent):
        """
        接收群消息，并将 MessageChain 存入本地缓存文件。
        """
        group_id = str(event.get_group_id())
        message_id = event.message_obj.message_id
        
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
             return None
             
        if group_id not in self.monitor_groups:
            return None

        try:
            message: MessageChain = event.get_messages()
            
            file_name = '{}_{}_{}.pkl'.format(
                int(time.time() * 1000), group_id, message_id
            )
            file_path = self.temp_path / file_name
            
            with open(file_path, 'wb') as f:
                pickle.dump({"message": message, "sender_id": event.get_sender_id()}, f)
            
            asyncio.create_task(delayed_delete(self.cache_expiration_time, file_path))
            
            logger.debug(f"[{self.instance_id}] 缓存消息 (ID: {message_id})，群: {group_id}")
            
        except Exception as e:
            logger.error(f"[{self.instance_id}] 缓存消息失败 (ID: {message_id})：{e}")

        return None

    # --- 2. 撤回事件处理逻辑 ---
    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def handle_recall_event(self, event: AstrMessageEvent):
        """
        处理撤回通知事件的核心逻辑 (手动 Go-CQHTTP 消息段)。
        """
        raw_message = event.message_obj.raw_message
        
        post_type = get_value(raw_message, "post_type")
        
        if post_type == "notice":
            notice_type = get_value(raw_message, "notice_type")
            
            if notice_type == "group_recall":
                group_id = str(get_value(raw_message, "group_id"))
                message_id = get_value(raw_message, "message_id")
                
                if group_id not in self.monitor_groups or not message_id:
                    return None
                    
                file_name_pattern = f"*_{group_id}_{message_id}.pkl"
                file_path: Path = next(self.temp_path.glob(file_name_pattern), None)
                
                if file_path and file_path.exists():
                    
                    local_files_to_cleanup = [] # 用于存储需要删除的本地图片路径
                    
                    try:
                        with open(file_path, 'rb') as f:
                            cached_data = pickle.load(f)
                        
                        original_message = cached_data["message"]
                        sender_id = cached_data["sender_id"]

                        # 1. 提取组件列表
                        if isinstance(original_message, MessageChain):
                            components = original_message.components
                        elif isinstance(original_message, list):
                            components = original_message
                        else:
                            components = []

                        # --- 调试输出 ---
                        logger.info(f"[{self.instance_id}] 缓存消息组件 ({len(components)}个):\n{_serialize_components(components)}")
                        # --- 结束调试输出 ---

                        client = event.bot
                        
                        # 2. 循环发送两部分消息
                        async with aiohttp.ClientSession() as session:
                            for target_id in self.target_receivers:
                                target_id_str = str(target_id)
                                
                                # A. 发送纯文本通知 (文本通知代码不变)
                                text_notification = (
                                    f"【防撤回提醒】\n"
                                    f"群聊：{group_id}\n"
                                    f"发送者：{sender_id}\n"
                                    f"原消息内容已在下一条消息中转发。"
                                )
                                try:
                                    logger.info(f"[{self.instance_id}] ➡️ 正在发送文本通知到：{target_id_str}")
                                    await client.send_private_msg(
                                        user_id=int(target_id_str),
                                        message=text_notification 
                                    )
                                    logger.info(f"[{self.instance_id}] ✅ 文本通知发送成功到：{target_id_str}")
                                except ActionFailed as e:
                                    logger.error(f"[{self.instance_id}] ❌ 文本通知API失败：{e}. 目标：{target_id_str}")
                                    continue
                                
                                # B. 复杂消息处理
                                
                                # 核心：先尝试下载图片，更新组件源
                                temp_components = list(components) # 复制一份组件列表
                                gocq_content_array = []
                                
                                for i, comp in enumerate(temp_components):
                                    if comp.type.name == 'Image':
                                        local_path = await _download_and_cache_image(session, comp, self.temp_path)
                                        if local_path:
                                            # 如果下载成功，记录路径，并使用这个路径构造 Go-CQHTTP 消息段
                                            local_files_to_cleanup.append(local_path)
                                            gocq_content_array.append({"type": "image", "data": {"file": local_path}})
                                        else:
                                            # 下载失败，发送文本占位符
                                            gocq_content_array.append({"type": "text", "data": {"text": "[图片转发失败]"}})
                                    else:
                                        # 非图片组件，使用标准转换
                                        gocq_content_array.extend(_convert_to_gocq_message([comp]))

                                if gocq_content_array:
                                    try:
                                        logger.info(f"[{self.instance_id}] ➡️ 正在发送 Go-CQHTTP 列表内容到：{target_id_str}")
                                        
                                        await client.send_private_msg(
                                            user_id=int(target_id_str),
                                            message=gocq_content_array # 传递手动构造的列表
                                        )
                                        logger.info(f"[{self.instance_id}] ✅ 复杂消息转发请求完成到：{target_id_str}")

                                    except Exception as e:
                                        logger.error(f"[{self.instance_id}] ❌ 复杂消息转发失败到 {target_id_str}：{e}")
                                        logger.error(traceback.format_exc())

                        # **核心：在所有发送完成后，清理本地文件**
                        if local_files_to_cleanup:
                            asyncio.create_task(_cleanup_local_files(local_files_to_cleanup))

                        # 撤回消息缓存清理
                        asyncio.create_task(delayed_delete(0, file_path))
                        
                    except Exception as e:
                        logger.error(f"[{self.instance_id}] ❌ 处理撤回事件失败：{e}")
                        logger.error(traceback.format_exc()) 
                    except aiohttp.ClientConnectorError as e:
                        logger.error(f"[{self.instance_id}] ❌ 网络连接失败，跳过本次转发: {e}")
                else:
                    logger.warning(
                        f"[{self.instance_id}] 找不到消息记录 (ID: {message_id})，可能已过期或未缓存。"
                    )
                    
        return None