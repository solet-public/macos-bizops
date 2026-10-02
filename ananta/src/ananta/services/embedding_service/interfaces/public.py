"""Embedding Service Public API - AI-discoverable operations."""

from abc import ABC, abstractmethod

from ananta.core.actions.action_metadata import (
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.domain.types import ActionResult
from ananta.core.services.service_interface_decorator import service_interface_process


def _contract_properties(data_description: str) -> dict[str, ParameterMetadata]:
    """The five ActionResult contract fields every embedding_service result carries."""
    return {
        "action_status": ParameterMetadata(
            type=ParameterType.STRING,
            description="Status: completed, or error when the bound provider refused the call",
            required=False,
        ),
        "data": ParameterMetadata(
            type=ParameterType.OBJECT, description=data_description, required=False
        ),
        "actions": ParameterMetadata(
            type=ParameterType.LIST, description="Follow-up actions; always empty", required=False
        ),
        "error": ParameterMetadata(
            type=ParameterType.OBJECT,
            description="None on success; on refusal the provider's typed error (type, code, message)",
            required=False,
        ),
        "timestamp": ParameterMetadata(
            type=ParameterType.STRING, description="ISO-8601 UTC time of the result", required=False
        ),
    }


class EmbeddingServiceAPI(ABC):
    """Public embedding operations - AI-discoverable via vector search."""

    @service_interface_process(
        name="generate_embeddings",
        is_discoverable=True,
        provider="embedding_service",
        parameters={
            "inputs": ParameterMetadata(
                description="Non-empty list of texts to embed; every text must fit the bound provider's token ceiling",
                required=True,
                type=ParameterType.LIST,
            ),
            "model": ParameterMetadata(
                description="Embedding model id; omit to use the bound provider's own model",
                required=False,
                type=ParameterType.STRING,
            ),
            "input_type": ParameterMetadata(
                description="Type of input data (default: 'text')",
                required=False,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="One vector per input, with the bound provider's own model id and vector dimension",
            properties=_contract_properties(
                "On success {result: {embeddings: one vector per input in input order, "
                "dimension: the provider's vector length, model: the provider's model id}}"
            ),
            usage_patterns=[
                "Generate embeddings through the bound embedding service without naming a provider",
                "Embed a batch of texts and check model and dimension against stored vectors",
            ],
        ),
        is_inference_capable=True,
    )
    @abstractmethod
    def generate_embeddings(
        self, inputs: list[str], model: str | None = None, input_type: str = "text"
    ) -> ActionResult:
        """Generate vector embeddings from text."""
        ...

    @service_interface_process(
        name="get_embedding_dimension",
        is_discoverable=True,
        provider="embedding_service",
        parameters={
            "model": ParameterMetadata(
                description="Embedding model id; omit to ask about the bound provider's own model",
                required=False,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="The bound provider's model id and vector dimension, without embedding anything",
            properties=_contract_properties(
                "On success {result: {dimension: the provider's vector length, "
                "model: the provider's model id}}"
            ),
            usage_patterns=[
                "Read the bound embedding model and dimension without embedding text",
                "Validate vector sizes against a stored index",
            ],
        ),
    )
    @abstractmethod
    def get_embedding_dimension(self, model: str | None = None) -> ActionResult:
        """Get embedding vector dimension."""
        ...

    @service_interface_process(
        name="list_models",
        provider="embedding_service",
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="List of available embedding models",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Available models payload (models array with metadata)",
                    required=False,
                ),
            },
            usage_patterns=[
                "Discover available embedding models",
                "Choose appropriate model for task",
            ],
        ),
    )
    @abstractmethod
    def list_models(self) -> ActionResult:
        """List available embedding models."""
        ...
