# -*- coding: utf-8 -*-
"""
已弃用：UI 回调接口已统一至 agent.AgentCallbacks。

保留此文件仅为兼容旧 import，新代码请使用：
    from coding_agent.agent import AgentCallbacks
"""

from __future__ import annotations

from ..agent import AgentCallbacks as UIEventBus  # noqa: F401 兼容别名

EVENT_BUS = UIEventBus()
