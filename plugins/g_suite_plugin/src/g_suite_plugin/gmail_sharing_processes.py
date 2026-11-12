"""``@platform_process`` Gmail build 2 verbs: delegates, send-as, S/MIME, client-side encryption.

Declarations come from :mod:`gmail_sharing_spec` through
:func:`gmail_settings_processes.settings_process`; ``GSuitePlugin`` supplies
``_run_deferred`` and the worker runs each verb through the same producer as
the other build-2 verbs.
"""

from __future__ import annotations

from typing import Any

from ananta.core.actions.action_metadata import platform_process

from .gmail_settings_processes import settings_process


class GmailSharingProcesses:
    """Delegates, send-as, S/MIME and CSE verbs; ``GSuitePlugin`` supplies ``_run_deferred``."""

    def _run_deferred(self, params: dict[str, Any], state: dict[str, Any], verb: str) -> dict[str, Any]:
        raise NotImplementedError

    @platform_process(name="gmail_list_delegates", **settings_process("gmail_list_delegates"))
    def gmail_list_delegates(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_list_delegates")

    @platform_process(name="gmail_get_delegate", **settings_process("gmail_get_delegate"))
    def gmail_get_delegate(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_get_delegate")

    @platform_process(name="gmail_create_delegate", **settings_process("gmail_create_delegate"))
    def gmail_create_delegate(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_create_delegate")

    @platform_process(name="gmail_delete_delegate", **settings_process("gmail_delete_delegate"))
    def gmail_delete_delegate(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_delete_delegate")

    @platform_process(name="gmail_list_send_as", **settings_process("gmail_list_send_as"))
    def gmail_list_send_as(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_list_send_as")

    @platform_process(name="gmail_get_send_as", **settings_process("gmail_get_send_as"))
    def gmail_get_send_as(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_get_send_as")

    @platform_process(name="gmail_create_send_as", **settings_process("gmail_create_send_as"))
    def gmail_create_send_as(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_create_send_as")

    @platform_process(name="gmail_patch_send_as", **settings_process("gmail_patch_send_as"))
    def gmail_patch_send_as(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_patch_send_as")

    @platform_process(name="gmail_update_send_as", **settings_process("gmail_update_send_as"))
    def gmail_update_send_as(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_update_send_as")

    @platform_process(name="gmail_delete_send_as", **settings_process("gmail_delete_send_as"))
    def gmail_delete_send_as(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_delete_send_as")

    @platform_process(name="gmail_verify_send_as", **settings_process("gmail_verify_send_as"))
    def gmail_verify_send_as(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_verify_send_as")

    @platform_process(name="gmail_list_smime_info", **settings_process("gmail_list_smime_info"))
    def gmail_list_smime_info(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_list_smime_info")

    @platform_process(name="gmail_get_smime_info", **settings_process("gmail_get_smime_info"))
    def gmail_get_smime_info(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_get_smime_info")

    @platform_process(name="gmail_insert_smime_info", **settings_process("gmail_insert_smime_info"))
    def gmail_insert_smime_info(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_insert_smime_info")

    @platform_process(name="gmail_set_default_smime_info", **settings_process("gmail_set_default_smime_info"))
    def gmail_set_default_smime_info(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_set_default_smime_info")

    @platform_process(name="gmail_delete_smime_info", **settings_process("gmail_delete_smime_info"))
    def gmail_delete_smime_info(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_delete_smime_info")

    @platform_process(name="gmail_list_cse_identities", **settings_process("gmail_list_cse_identities"))
    def gmail_list_cse_identities(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_list_cse_identities")

    @platform_process(name="gmail_get_cse_identity", **settings_process("gmail_get_cse_identity"))
    def gmail_get_cse_identity(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_get_cse_identity")

    @platform_process(name="gmail_create_cse_identity", **settings_process("gmail_create_cse_identity"))
    def gmail_create_cse_identity(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_create_cse_identity")

    @platform_process(name="gmail_patch_cse_identity", **settings_process("gmail_patch_cse_identity"))
    def gmail_patch_cse_identity(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_patch_cse_identity")

    @platform_process(name="gmail_delete_cse_identity", **settings_process("gmail_delete_cse_identity"))
    def gmail_delete_cse_identity(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_delete_cse_identity")

    @platform_process(name="gmail_list_cse_key_pairs", **settings_process("gmail_list_cse_key_pairs"))
    def gmail_list_cse_key_pairs(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_list_cse_key_pairs")

    @platform_process(name="gmail_get_cse_key_pair", **settings_process("gmail_get_cse_key_pair"))
    def gmail_get_cse_key_pair(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_get_cse_key_pair")

    @platform_process(name="gmail_create_cse_key_pair", **settings_process("gmail_create_cse_key_pair"))
    def gmail_create_cse_key_pair(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_create_cse_key_pair")

    @platform_process(name="gmail_enable_cse_key_pair", **settings_process("gmail_enable_cse_key_pair"))
    def gmail_enable_cse_key_pair(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_enable_cse_key_pair")

    @platform_process(name="gmail_disable_cse_key_pair", **settings_process("gmail_disable_cse_key_pair"))
    def gmail_disable_cse_key_pair(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_disable_cse_key_pair")

    @platform_process(name="gmail_obliterate_cse_key_pair", **settings_process("gmail_obliterate_cse_key_pair"))
    def gmail_obliterate_cse_key_pair(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_obliterate_cse_key_pair")
