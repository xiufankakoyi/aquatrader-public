"""data_svc 层的数据源注册中心。

直接复用 ``infrastructure.data_providers.provider_registry`` 的实现，
避免在 ``data_svc`` 和 ``infrastructure`` 之间重复维护两套注册表。
"""

from infrastructure.data_providers.provider_registry import ProviderRegistry

__all__ = ["ProviderRegistry"]
