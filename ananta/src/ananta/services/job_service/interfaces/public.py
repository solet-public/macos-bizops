"""Job Service Public API.

AI-discoverable asynchronous job operations with @service_interface_process decorators.
All methods in this interface are indexed for process discovery.

Discoverability Policy (Task #47, 2026-05-24):
- ``get_latest_job`` declares ``is_discoverable=True`` explicitly. The base
  decorator default for ``@service_interface_process`` is
  ``is_discoverable=False`` (service methods are presumed internal); fetching
  the latest async job is agent-callable (diagnose stuck job, retrieve job_id
  before status monitoring).
- Adding a new method without ``is_discoverable=True`` will SILENTLY exclude it
  from ``process_search`` and the agent will not be able to find it.
"""

from abc import ABC, abstractmethod
from typing import Any

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.services.service_interface_decorator import service_interface_process


class JobServiceAPI(ABC):
    """Public job service operations - AI-discoverable via process registry.

    This interface defines asynchronous job operations that can be discovered
    and invoked by the AI orchestration system:

    1. get_latest_job - Retrieve the most recently created asynchronous job

    Each method is decorated with complete metadata for process registry.
    """

    @service_interface_process(
        name="get_latest_job",
        is_discoverable=True,
        provider="job_service",
        parameters={
            "plugin_name": ParameterMetadata(
                description=(
                    "Filter to jobs created by the specified plugin "
                    "(e.g., default_image_generation_plugin)"
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "action_name": ParameterMetadata(
                description=(
                    "Filter to jobs created by the specified action (e.g., generate_image)"
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "status": ParameterMetadata(
                description=(
                    "Filter to jobs in the specified status "
                    "(pending, processing, completed, failed, cancelled)"
                ),
                required=False,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Asynchronous job metadata",
            type=ParameterType.OBJECT,
            properties={
                "job": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Job record from core__asynchronous_jobs",
                    required=False,
                ),
            },
            usage_patterns=[
                "Fetch the latest job for a plugin/action to retrieve job_id before status monitoring",
                "Diagnose async job failures by inspecting stored result/error payloads",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=True
        )
    )
    @abstractmethod
    def get_latest_job(
        self,
        plugin_name: str | None = None,
        action_name: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve the most recently created asynchronous job.

        Service interface receives individual kwargs (action_processor pattern).
        Queries the core__job table with optional filters.
        """
        ...
