from typing import Dict, Optional, Type
from protocols.base import BaseProtocol
from protocols.yjx import YjxProtocol
from protocols.compx import CompxProtocol
from protocols.rapoo import RapooProtocol
from protocols.standard_hid import StandardHidProtocol

class ProtocolRegistry:
    def __init__(self):
        self._protocols: Dict[str, BaseProtocol] = {}
        # 注册内置协议
        self.register(YjxProtocol())
        self.register(CompxProtocol())
        self.register(RapooProtocol())
        self.register(StandardHidProtocol())

    def register(self, protocol_instance: BaseProtocol):
        self._protocols[protocol_instance.protocol_id] = protocol_instance

    def get(self, protocol_id: str) -> Optional[BaseProtocol]:
        return self._protocols.get(protocol_id)

    def list_all(self) -> Dict[str, str]:
        """返回所有支持的协议字典 {protocol_id: display_name}"""
        return {pid: p.display_name for pid, p in self._protocols.items()}

protocol_registry = ProtocolRegistry()
