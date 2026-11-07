"""Registry of the typed verbs: Workflows, SQL Data Query jobs and Files (build 2); Orders, Subscriptions and Accounts (build 3); Payment Methods, Payments, Custom Objects and Object Query (build 4); Billing Documents, Invoices, Credit and Debit Memos, Bill Runs and Usage (build 5).

One place maps each verb name to its worker callable (registered in
``async_jobs.ACTION_HANDLERS``), its dispatch-time validator and its retry
class (reads retryable, writes and deletes not), so the decorator in
``*_processes`` modules, the EDGE definition in
``plugin.get_edge_process_definitions`` and the worker can never disagree.
"""

from __future__ import annotations

from typing import Any, Final

from . import account_actions, bill_run_usage_actions, billing_document_actions, custom_object_actions, data_query_actions, order_actions, payment_actions, workflow_actions
from .typed_operations import ActionFn, Operation, action, build_path, validate

_SPEC_OPERATIONS: Final[dict[str, Operation]] = {
    op.name: op
    for op in (
        *workflow_actions.OPERATIONS,
        *data_query_actions.OPERATIONS,
        *order_actions.ORDER_OPERATIONS,
        *order_actions.SUBSCRIPTION_OPERATIONS,
        *account_actions.ACCOUNT_OPERATIONS,
        *payment_actions.PAYMENT_METHOD_OPERATIONS,
        *payment_actions.PAYMENT_OPERATIONS,
        *custom_object_actions.CUSTOM_OBJECT_OPERATIONS,
        *custom_object_actions.OBJECT_QUERY_OPERATIONS,
        *billing_document_actions.BILLING_DOCUMENT_OPERATIONS,
        *billing_document_actions.INVOICE_OPERATIONS,
        *billing_document_actions.CREDIT_MEMO_OPERATIONS,
        *billing_document_actions.DEBIT_MEMO_OPERATIONS,
        *bill_run_usage_actions.BILL_RUN_OPERATIONS,
        *bill_run_usage_actions.USAGE_OPERATIONS,
    )
}
_DOWNLOADS: Final[dict[str, tuple[Operation, ActionFn]]] = {
    "download_data_query_result": (data_query_actions.DOWNLOAD_RESULT, data_query_actions.download_data_query_result),
    "download_file": (data_query_actions.DOWNLOAD_FILE, data_query_actions.download_file),
}

TYPED_HANDLERS: Final[dict[str, ActionFn]] = {
    **{name: action(op) for name, op in _SPEC_OPERATIONS.items()},
    **{name: handler for name, (_, handler) in _DOWNLOADS.items()},
}
TYPED_RETRYABLE: Final[dict[str, bool]] = {
    **{name: op.retryable for name, op in _SPEC_OPERATIONS.items()},
    **{name: op.retryable for name, (op, _) in _DOWNLOADS.items()},
}


def result_type(name: str) -> str:
    """The EDGE result type each typed verb declares, matching its process JSON."""
    return f"zuora_{name}_result"


def validate_typed(name: str, params: dict[str, Any]) -> None:
    """Dispatch-time validation; raises ``ValueError`` before any job is persisted."""
    if name in _DOWNLOADS:
        build_path(_DOWNLOADS[name][0], params)
        data_query_actions.validate_download(params)
        return
    validate(_SPEC_OPERATIONS[name], params)
