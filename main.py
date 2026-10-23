from __future__ import annotations

from langbot_plugin.api.definition.plugin import BasePlugin


class QQGroupCompanion(BasePlugin):
    """插件入口。业务逻辑在 companion/ 包里，事件挂载在 components/event_listener/。"""

    async def initialize(self) -> None:
        # 事件监听组件要等第一条消息才初始化；插件一启动就检查一次配置，有问题马上能在日志里看到
        try:
            from components.event_listener.companion import load_settings

            load_settings(self.get_config() or {})
        except Exception:
            # 启动检查出错不能让插件起不来；第一条消息到来时还会再检查一次
            import logging

            logging.getLogger('qq_group_companion.main').exception('插件启动时检查配置失败')
