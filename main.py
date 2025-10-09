import asyncio
import time
import pickle
import traceback
from pathlib import Path
from astrbot.api import logger
from astrbot.api import AstrBotConfig
from astrbot.api.star import StarTools
from astrbot.api import message_components as Comp
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.core.star.filter.platform_adapter_type import PlatformAdapterType


def get_private_unified_msg_origin(user_id: str, platform: str = "aiocqhttp") -> str:
    """获取私聊的统一消息来源格式，用于 context.send_message API"""
    return f"{platform}:FriendMessage:{user_id}"

async def delayed_delete(delay: int, path: Path):
    """延迟删除文件"""
    await asyncio.sleep(delay)
    try:
        # 尝试删除文件
        path.unlink(missing_ok=True)
        logger.debug(f"[AntiRevoke] 缓存清理：已删除过期文件 {path.name}")
    except Exception:
        logger.error(f"[AntiRevoke] 删除文件失败 ({path}): {traceback.format_exc()}")

def get_value(obj, key, default=None):
    """安全地获取属性/字典值"""
    try:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)
    except Exception:
        return default

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
        
        # 统一配置，简化处理逻辑
        self.monitor_groups = [str(g) for g in config.get("monitor_groups", []) or []]
        self.target_receivers = [str(r) for r in config.get("target_receivers", []) or []]
        
        self.instance_id = "AntiRevoke"
        # 缓存过期时间，秒，默认5分钟
        self.cache_expiration_time = int(config.get("cache_expiration_time", 300)) 
        
        # 初始化缓存目录
        self.temp_path = Path(StarTools.get_data_dir()) / "anti_revoke_data" # 使用一个通用的数据目录名
        self.temp_path.mkdir(exist_ok=True)

        # 启动时清理过期的缓存文件
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
                # 文件名格式: {timestamp_ms}_{group_id}_{message_id}.pkl
                file_create_time_ms = int(file.name.split('_')[0])
                if now_ms - file_create_time_ms > self.cache_expiration_time * 1000:
                    file.unlink(missing_ok=True)
                    expired_count += 1
            except Exception:
                # 忽略格式不正确的文件
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
        raw_message = event.message_obj.raw_message
        group_id = str(event.get_group_id())
        message_id = event.message_obj.message_id
        
        # 仅处理群聊消息
        if event.message_obj.message_type != 'group':
             return None
             
        # 仅处理在监控列表中的群
        if group_id not in self.monitor_groups:
            return None

        try:
            # 获取完整的 MessageChain
            message: MessageChain = event.get_messages()
            
            # 文件名格式: {timestamp_ms}_{group_id}_{message_id}.pkl
            file_name = '{}_{}_{}.pkl'.format(
                int(time.time() * 1000), group_id, message_id
            )
            file_path = self.temp_path / file_name
            
            # 使用 pickle 序列化 MessageChain 和发送者ID
            with open(file_path, 'wb') as f:
                pickle.dump({"message": message, "sender_id": event.get_sender_id()}, f)
            
            # 设定定时任务，在缓存时间后自动删除文件
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
        处理撤回通知事件的核心逻辑。
        """
        raw_message = event.message_obj.raw_message
        
        post_type = get_value(raw_message, "post_type")
        
        if post_type == "notice":
            notice_type = get_value(raw_message, "notice_type")
            
            if notice_type == "group_recall":
                group_id = str(get_value(raw_message, "group_id"))
                message_id = get_value(raw_message, "message_id")
                
                # 检查是否为监控群
                if group_id not in self.monitor_groups or not message_id:
                    return None
                    
                # 查找缓存文件: *_{group_id}_{message_id}.pkl
                file_name_pattern = f"*_{group_id}_{message_id}.pkl"
                file_path: Path = next(self.temp_path.glob(file_name_pattern), None)
                
                if file_path and file_path.exists():
                    try:
                        with open(file_path, 'rb') as f:
                            cached_data = pickle.load(f)
                        
                        original_message: MessageChain = cached_data["message"]
                        sender_id = cached_data["sender_id"]
                        # 撤回操作者，如果是自己撤回，operator_id就是自己的QQ号；如果是管理员，就是管理员的QQ号
                        operator_id = get_value(raw_message, "operator_id") 

                        logger.info(
                            f"[{self.instance_id}] 发现撤回。群: {group_id}, 发送者: {sender_id}, 操作者: {operator_id}"
                        )
                        
                        # 构建提醒消息 (MessageChain)
                        alert_chain = MessageChain(
                            [
                                Comp.Plain(f"【防撤回提醒】\n"),
                                Comp.Plain(f"群聊：{group_id}\n"),
                                Comp.Plain(f"发送者：{sender_id}\n"),
                                Comp.Plain(f"操作者：{operator_id} (撤回人)\n"),
                                Comp.Plain("被撤回消息内容：\n"),
                                Comp.Plain("--------------------\n")
                            ] + original_message.components 
                              + [Comp.Plain("\n--------------------")]
                        )

                        # 转发给目标接收者
                        await self._send_to_targets(event, alert_chain)

                        # 撤回消息已处理，立即删除缓存文件
                        asyncio.create_task(delayed_delete(0, file_path))
                        
                    except Exception as e:
                        logger.error(f"[{self.instance_id}] 处理撤回事件失败 (ID: {message_id})：{e}")
                else:
                    logger.warning(
                        f"[{self.instance_id}] 找不到消息记录 (ID: {message_id})，可能已过期或未缓存。"
                    )
                    
        return None

    async def _send_to_targets(self, event: AstrMessageEvent, message_chain: MessageChain):
        """
        发送提醒消息给所有配置的目标接收者 (用户或群)。
        """
        for target_id in self.target_receivers:
            try:
                target_id_str = str(target_id)
                # 简单的长度判断区分私聊和群聊
                if len(target_id_str) > 8 and target_id_str.isdigit(): 
                    # 假设是群号
                    target_origin = f"{event.adapter_type}:GroupMessage:{target_id_str}"
                    target_type = "群聊"
                else: 
                    # 假设是用户ID (私聊)
                    target_origin = get_private_unified_msg_origin(target_id_str, event.adapter_type)
                    target_type = "私聊"
                
                await self.context.send_message(target_origin, message_chain)
                
                logger.debug(f"[{self.instance_id}] 成功发送防撤回提醒到 {target_type}：{target_id_str}")

            except Exception as e:
                logger.error(f"[{self.instance_id}] 发送提醒消息到 {target_id_str} 失败：{e}")