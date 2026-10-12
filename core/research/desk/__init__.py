"""Virtual Research Desk — 虚拟投研台，模拟机构晨会五岗协作。"""

from core.research.desk.models import AgentOutput, CIOOutput
from core.research.desk.virtual_research_desk import VirtualResearchDesk

__all__ = ["AgentOutput", "CIOOutput", "VirtualResearchDesk"]
