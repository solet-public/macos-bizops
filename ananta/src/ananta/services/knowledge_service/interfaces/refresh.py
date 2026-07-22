"""Process-registry refresh service-interface verbs (W5.Q decomposition).

Reload one or all process JSON definitions from a plugin's knowledge
base and merge the changes into the live process registry without a
restart. Lifted byte-for-byte from the W5.Q-pre-decomposition
``KnowledgeServiceAPI``.
"""

from abc import ABC, abstractmethod
from typing import Any

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.domain.enums import ProcessorPolicyCategory
from ananta.core.services.service_interface_decorator import service_interface_process


class KnowledgeRefreshAPI(ABC):
    """Process-registry refresh verbs — refresh_plugin_processes / refresh_plugin_process."""

    @service_interface_process(
        name="refresh_plugin_processes",
        provider="knowledge_service",
        is_discoverable=True,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "plugin_name": ParameterMetadata(
                description="Plugin name whose processes to refresh (use 'ananta' for platform services)",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Refresh result with updated process count",
            type=ParameterType.OBJECT,
            properties={
                "updated_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Number of processes updated",
                ),
                "process_keys": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="List of updated process keys",
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        ),
    )
    @abstractmethod
    def refresh_plugin_processes(self, plugin_name: str) -> dict[str, Any]: ...

    @service_interface_process(
        name="refresh_plugin_process",
        provider="knowledge_service",
        is_discoverable=True,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "plugin_name": ParameterMetadata(
                description="Plugin name owning the process (use 'ananta' for platform services)",
                required=True,
                type=ParameterType.STRING,
            ),
            "process_key": ParameterMetadata(
                description="The specific process key to refresh",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Refresh result for single process",
            type=ParameterType.OBJECT,
            properties={
                "process_key": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="The refreshed process key",
                ),
                "updated": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="Whether the process was successfully updated",
                ),
            },
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        ),
    )
    @abstractmethod
    def refresh_plugin_process(
        self, plugin_name: str, process_key: str,
    ) -> dict[str, Any]: ...
