"""Fixed provider-owned credential operations and public metadata projections.

Requests and results are declared in the dependency-light interface contracts.
This implementation resolves at the operation boundary and never publishes a
credential, provider result envelope or provider exception.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac

from ananta.core.domain.types import ActionResult
from ananta.core.services.call_context import CallContext, VaultAccessDeniedError, VaultKeyMalformedError
from ananta.interfaces.credential_contracts import CredentialProvider, MetadataRequest, MetadataResult, UseRequest, UseResult, VaultReference, VaultStatus, VerifyRequest
from ananta.services.vault_service.enforcement import enforce_namespace


def _sign_result(result: ActionResult, request: UseRequest) -> UseResult:
    if result.get("action_status") != "completed":
        return UseResult("provider_error")
    data = result.get("data")
    if data is None:
        return UseResult("provider_error")
    if data.get("found") is False:
        return UseResult("not_found")
    value = data.get("value")
    if not isinstance(value, str) or not value:
        return UseResult("invalid_credential")
    if request.key_encoding == "base64":
        key = base64.b64decode(value, validate=True)
    elif request.key_encoding == "utf8":
        key = value.encode("utf-8")
    else:
        return UseResult("invalid_request")
    if not key:
        return UseResult("invalid_credential")
    signature = hmac.new(key, request.message, hashlib.sha256).hexdigest()
    if isinstance(request, VerifyRequest):
        return UseResult("completed", verified=hmac.compare_digest(signature, request.signature))
    return UseResult("completed", signature=signature)


def use_credential(provider: CredentialProvider, request: UseRequest, context: CallContext | None) -> UseResult:
    """Resolve anew at the fixed signing boundary; publish categorized failures.

    Ownership and a present credential are authentication prerequisites. The
    provider and this check run on every use; the authorized-vector and
    foreign-namespace smoke cover both paths. There is no scheduled freshness
    gate, retained key, arbitrary callback or alternate identity.
    """
    try:
        enforce_namespace(request.reference.key, context)
        return _sign_result(provider.retrieve(request.reference.key, call_context=context), request)
    except VaultAccessDeniedError:
        return UseResult("denied")
    except VaultKeyMalformedError:
        return UseResult("invalid_reference")
    except (binascii.Error, UnicodeError):
        return UseResult("invalid_credential")
    except Exception:
        # Never stringify or chain a credential-bearing provider exception.
        return UseResult("provider_error")


def _owned_references(data: dict[str, object], context: CallContext) -> tuple[VaultReference, ...]:
    records = data.get("secrets")
    if not isinstance(records, list):
        raise ValueError("invalid metadata list")
    references: list[VaultReference] = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(key := record.get("key"), str):
            raise ValueError("invalid metadata reference")
        try:
            enforce_namespace(key, context)
        except VaultAccessDeniedError:
            continue
        references.append(VaultReference(key))
    return tuple(sorted(references, key=lambda reference: reference.key))


def _project_result(result: ActionResult, kind: str, context: CallContext) -> MetadataResult:
    if result.get("action_status") != "completed":
        return MetadataResult("provider_error")
    data = result.get("data")
    if data is None:
        return MetadataResult("provider_error")
    if kind == "exists":
        exists = data.get("exists")
        return MetadataResult("completed", exists=exists if isinstance(exists, bool) else None)
    if kind == "list":
        return MetadataResult("completed", references=_owned_references(data, context))
    initialized, locked = data.get("initialized"), data.get("locked")
    return MetadataResult(
        "completed",
        vault_status=VaultStatus(
            initialized if isinstance(initialized, bool) else None,
            locked if isinstance(locked, bool) else None,
        ),
    )


def project_metadata(provider: CredentialProvider, request: MetadataRequest, context: CallContext | None) -> MetadataResult:
    """Project metadata without a plaintext read or a mixed provider envelope."""
    try:
        if context is None:
            return MetadataResult("denied")
        if request.kind == "exists":
            if request.reference is None:
                return MetadataResult("invalid_reference")
            enforce_namespace(request.reference.key, context)
            result = provider.exists(request.reference.key, call_context=context)
        elif request.kind == "list":
            result = provider.list(request.tag, call_context=context)
        elif request.kind == "status":
            result = provider.status(call_context=context)
        else:
            return MetadataResult("invalid_request")
        return _project_result(result, request.kind, context)
    except VaultAccessDeniedError:
        return MetadataResult("denied")
    except VaultKeyMalformedError:
        return MetadataResult("invalid_reference")
    except Exception:
        return MetadataResult("provider_error")
