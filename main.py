from __future__ import annotations

from langbot_plugin.api.definition.plugin import BasePlugin


class QQGroupCompanion(BasePlugin):
    """插件入口。业务逻辑在 companion/ 包里，事件挂载在 components/event_listener/。"""

    async def initialize(self) -> None:
        pass
