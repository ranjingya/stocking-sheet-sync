"""组装应用资源并统一管理生命周期。"""

from contextlib import ExitStack

from stocking_sheet_sync.infrastructure.feishu.client import FeishuClient
from stocking_sheet_sync.infrastructure.redis import RedisStateStore
from stocking_sheet_sync.services.sync import SyncService
from stocking_sheet_sync.settings import AppConfig


def build_service(config: AppConfig, resources: ExitStack, *, migrate: bool = False) -> SyncService:
    """
    功能说明：创建数据应用、消息应用及状态存储，组装搬运服务。

    参数：
        config：经过校验的应用配置。
        resources：调用方持有的资源栈，初始化失败或退出时按逆序关闭资源。
        migrate：是否执行已有状态格式迁移，后台消费启动时开启。
    返回值：已组装的搬运服务。
    """
    store = RedisStateStore(
        config.redis_url,
        config.redis_key_prefix,
        socket_timeout_seconds=config.request_timeout_seconds,
    )
    resources.callback(store.close)
    if migrate:
        store.migrate_legacy_records()
    data = FeishuClient(config, config.feishu_data_app_id, config.feishu_data_app_secret, "data")
    resources.callback(data.close)
    message = FeishuClient(
        config, config.feishu_message_app_id, config.feishu_message_app_secret, "message"
    )
    resources.callback(message.close)
    return SyncService(config, data, message, store)
