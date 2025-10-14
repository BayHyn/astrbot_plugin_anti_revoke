import asyncio
import time
import pickle
import traceback
import json 
from pathlib import Path
from typing import List, Dict
import aiohttp
import os 
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


async def _download_and_cache_image(session: aiohttp.ClientSession, component: Comp.Image, temp_path: Path) -> str:
    """
    下载图片到本地，并返回Go-CQHTTP可用的绝对文件路径。
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
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Referer': 'https://qzone.qq.com/' 
        }
        
        async with session.get(image_url, headers=headers, timeout=15) as response:
            response.raise_for_status() 
            
            content_type = response.headers.get('Content-Type', '').lower()
            if 'image' not in content_type and 'octet-stream' not in content_type:
                logger.warning(f"[AntiRevoke] 下载 URL 返回类型非图片: {content_type}")
                return None
                
            image_bytes = await response.read()
            temp_file_path.write_bytes(image_bytes)

        logger.info(f"[AntiRevoke] 图片成功缓存到本地: {temp_file_path.name}")
        
        return str(temp_file_path.absolute())

    except Exception as e:
        logger.error(f"[AntiRevoke] ❌ 图片下载或保存失败 ({image_url}): {e}")
        if temp_file_path.exists():
            os.remove(temp_file_path)
        return None

async def _process_component_and_get_gocq_part(
    comp, session: aiohttp.ClientSession, temp_path: Path, local_files_to_cleanup: List[str]
) -> List[Dict]:
    """处理单个组件，返回其 Go-CQHTTP 消息段列表，并处理图片下载和清理。"""
    
    gocq_parts = []
    comp_type_name = getattr(comp.type, 'name', 'unknown')

    if comp_type_name in ['Plain', 'Text']:
        text = getattr(comp, 'text', '')
        if text:
            # 文本
            gocq_parts.append({"type": "text", "data": {"text": text}})
    
    elif comp_type_name == 'Face':
        face_id = getattr(comp, 'id', None)
        if face_id is not None:
            # 表情：使用 Go-CQHTTP 要求的 Face 格式
            gocq_parts.append({"type": "face", "data": {"id": int(face_id)}})
    
    elif comp_type_name == 'Image':
        local_path = await _download_and_cache_image(session, comp, temp_path)
        
        if local_path:
            # 图片
            local_files_to_cleanup.append(local_path)
            gocq_parts.append({"type": "image", "data": {"file": local_path}})
        else:
            gocq_parts.append({"type": "text", "data": {"text": "[图片转发失败]"}})

    # 如果是其他类型 (At, Video, Record)，则返回空列表，由主循环记录到 unsupported_types
    
    return gocq_parts

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
        self.ignore_senders = [str(s) for s in config.get("ignore_senders", []) or []]
        
        self.instance_id = "AntiRevoke"
        self.cache_expiration_time = int(config.get("cache_expiration_time", 300))
        
        self.context = context 
        
        self.temp_path = Path(StarTools.get_data_dir()) / "anti_revoke_data"
        self.temp_path.mkdir(exist_ok=True)

        self._cleanup_cache_on_startup()

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
                pickle.dump({
                    "message": message, 
                    "sender_id": event.get_sender_id(),
                    "timestamp": event.message_obj.timestamp
                }, f)
            
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
        处理撤回通知事件的核心逻辑 (单条消息模式，包含富媒体)。
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
                    
                    local_files_to_cleanup = [] 
                    
                    try:
                        with open(file_path, 'rb') as f:
                            cached_data = pickle.load(f)
                        
                        original_message = cached_data["message"]
                        sender_id = cached_data["sender_id"]

                        if str(sender_id) in self.ignore_senders:
                            logger.debug(f"[{self.instance_id}] 撤回消息发送者 {sender_id} 在白名单中，跳过转发。")
                            return None
                        
                        timestamp = cached_data.get("timestamp")
                        message_time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp)) if timestamp else "未知时间"


                        # 1. 提取组件列表
                        if isinstance(original_message, MessageChain):
                            components = original_message.components
                        elif isinstance(original_message, list):
                            components = original_message
                        else:
                            components = []

                        client = event.bot
                        
                        # 获取群名和昵称
                        group_name = str(group_id)
                        member_nickname = str(sender_id)

                        try:
                            group_info = await client.api.call_action('get_group_info', group_id=int(group_id))
                            group_name = group_info.get('group_name', group_name)
                        except Exception:
                            pass

                        try:
                            member_info = await client.api.call_action('get_group_member_info', group_id=int(group_id), user_id=int(sender_id))
                            card = member_info.get('card', '')
                            nickname = member_info.get('nickname', '')

                            if card and card.strip():
                                member_nickname = card
                            elif nickname and nickname.strip():
                                member_nickname = nickname
                        except Exception:
                            pass
                        
                        logger.info(f"[{self.instance_id}] 发现撤回。群: {group_name} ({group_id}), 发送者: {member_nickname} ({sender_id})")
                        logger.info(f"[{self.instance_id}] 缓存消息组件 ({len(components)}个):\n{_serialize_components(components)}") # <--- 输出组件结构

                        
                        # 2. 循环发送 (单条消息)
                        async with aiohttp.ClientSession() as session:
                            for target_id in self.target_receivers:
                                target_id_str = str(target_id)
                                
                                # A. 构造最终的 Go-CQHTTP 消息段数组 (包含头部通知)
                                gocq_content_array = []
                                unsupported_types = set()
                                
                                # 用于缓存消息体中的文本和图片等消息段
                                message_parts = []

                                # 遍历组件并转换
                                for comp in components:
                                    comp_type_name = getattr(comp.type, 'name', 'unknown')
                                    
                                    if comp_type_name in ['Plain', 'Text', 'Image', 'Face']: # 包含 Face
                                        # 使用辅助函数处理所有已支持的组件 (Plain, Face, Image)
                                        converted_parts = await _process_component_and_get_gocq_part(
                                            comp, session, self.temp_path, local_files_to_cleanup
                                        )
                                        message_parts.extend(converted_parts)
                                    
                                    else:
                                        unsupported_types.add(comp_type_name)

                                # 3. 在所有消息段前面插入昵称和冒号，只对第一个文本/图片消息段生效
                                has_inserted_prefix = False
                                final_message_parts = []
                                for part in message_parts:
                                    if not has_inserted_prefix and (part['type'] == 'text' or part['type'] == 'image' or part['type'] == 'face'):
                                        # 对第一个非表情/文本/图片的消息段，插入昵称前缀
                                        final_message_parts.append({"type": "text", "data": {"text": f"{member_nickname}："}})
                                        has_inserted_prefix = True
                                        
                                        # 如果原本就是文本，需要确保昵称和文本内容在同一个消息段
                                        if part['type'] == 'text':
                                            final_message_parts[-1]['data']['text'] += part['data']['text']
                                            continue # 跳过原始文本部分
                                        
                                    final_message_parts.append(part)
                                
                                
                                # 4. 构造通知头部 (纯文本)
                                notification_prefix = (
                                    f"【防撤回提醒】\n"
                                    f"群聊：{group_name} ({group_id})\n"
                                    f"发送者：{member_nickname} ({sender_id})\n"
                                    f"时间：{message_time_str}"
                                )
                                
                                # 5. 在头部文本中添加不支持的组件警告
                                warning_text = ""
                                if unsupported_types:
                                    warning_text = f"\n⚠️ 注意：包含无法转发组件：{', '.join(unsupported_types)}"
                                    final_prefix_text = f"{notification_prefix}{warning_text}"
                                else:
                                    final_prefix_text = f"{notification_prefix}\n--------------------\n"
                                gocq_content_array.append({"type": "text", "data": {"text": final_prefix_text}})
                                
                                # 将处理后的消息段附加到头部通知之后
                                gocq_content_array.extend(final_message_parts)


                                # B. 执行发送 (单次调用)
                                if gocq_content_array:
                                    try:
                                        logger.info(f"[{self.instance_id}] ➡️ 正在发送【合并消息】到：{target_id_str}")
                                        
                                        logger.info(f"[{self.instance_id}] Go-CQHTTP REQUEST BODY:\n{json.dumps(gocq_content_array, indent=2, ensure_ascii=False)}")
                                        
                                        await client.send_private_msg(
                                            user_id=int(target_id_str),
                                            message=gocq_content_array # 传递合并后的列表
                                        )
                                        logger.info(f"[{self.instance_id}] ✅ 合并消息转发请求完成到：{target_id_str}")

                                    except Exception as e:
                                        logger.error(f"[{self.instance_id}] ❌ 合并消息转发失败到 {target_id_str}：{e}")
                                        logger.error(traceback.format_exc())

                        if local_files_to_cleanup:
                            asyncio.create_task(_cleanup_local_files(local_files_to_cleanup))
                            logger.info(f"已调度本地图片清理任务，共 {len(local_files_to_cleanup)} 个文件。")

                        # 撤回消息缓存清理
                        asyncio.create_task(delayed_delete(0, file_path))
                        logger.info(f"已调度撤回消息缓存清理任务，文件路径：{file_path}")
                        
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