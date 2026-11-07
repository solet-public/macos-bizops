"""Workspace-root containment for TSV exports (A2, operator-ruled 2026-07-15).

Bulk query results belong in the OPERATOR'S OWN workspace as files — never in
context, never in platform blob storage. The caller supplies an absolute
``output_tsv_path``; this gate admits it only when the realpath-resolved
candidate lives under a realpath of one operator-configured
``export_allowed_roots`` entry (``commonpath`` equality, NOT string-prefix).
An empty ``export_allowed_roots`` is the secure default: every export is
refused until the operator opts workspace roots in via the plugin config.

Own-copy per connector plugin (deliberate duplication over a shared module,
per the platform's per-plugin-copy direction), mirroring
``salesforce_plugin``/``external_postgres_plugin``/``snowflake_plugin``'s
identical own-copies and
``ananta/src/ananta/services/session_ledger_service/enforcement.py`` (the
``ledger_allowed_roots`` gate). Deliberately NO ``Path.cwd()`` and NO
session-cwd plumbing — a client-reported cwd is caller-supplied by the same
principal that supplies the path, so it is not containment, and internal EDGE
dispatch has no bridge session at all (Architect ruling 2026-07-15,
``workbench/2026-07-15_architect_connector_rollout_rulings.md``).
"""

from __future__ import annotations

import os

from .constants import ERROR_EXPORT_PATH_REFUSED, TSV_SUFFIX


class ExportPathRefusedError(RuntimeError):
    """Raised when an export path fails workspace-root containment."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = ERROR_EXPORT_PATH_REFUSED


def assert_export_path_allowed(
    output_tsv_path: str,
    allowed_roots: list[str],
    *,
    config_key: str,
    plugin_name: str,
    param_name: str = "output_tsv_path",
    suffix: str | None = TSV_SUFFIX,
) -> str:
    """Admit an absolute path (``.tsv`` unless ``suffix`` says otherwise) under one allowed root.

    ``suffix=None`` admits any file name: typed file downloads keep the
    vendor's own format (PDF, CSV, gzip). ``param_name`` names the caller's
    parameter in every refusal.

    Returns the realpath-resolved path to write. Refusals name the config key
    so the operator knows exactly which knob opts a workspace root in.
    """
    if not os.path.isabs(output_tsv_path):
        raise ExportPathRefusedError(
            f"{param_name} must be an ABSOLUTE path (got {output_tsv_path!r}); pass a path under one of the operator-configured workspace roots ({config_key} in the {plugin_name} config)",
        )
    if suffix is not None and not output_tsv_path.endswith(suffix):
        raise ExportPathRefusedError(
            f"{param_name} must end in '{suffix}' (got {output_tsv_path!r}); this verb writes that file type",
        )
    if not allowed_roots:
        raise ExportPathRefusedError(
            f"no workspace roots are configured — {config_key} in the {plugin_name} config is empty, which refuses every export by default. Add the workspace directory (or directories) exports may land in to {config_key}, then re-run.",
        )
    candidate = os.path.realpath(output_tsv_path)
    for allowed in allowed_roots:
        expanded = os.path.expandvars(os.path.expanduser(allowed))
        if not expanded or not os.path.isabs(expanded):
            # A relative or blank root would realpath against the SERVER
            # process cwd — exactly the implicit-cwd containment hole the
            # 2026-07-15 ruling prohibits. Config fault: fail loud.
            raise ExportPathRefusedError(
                f"misconfigured {config_key} entry {allowed!r} in the {plugin_name} config: every root must be an ABSOLUTE directory path (after ~/$VAR expansion); relative or empty roots would resolve against the server process cwd",
            )
        allowed_real = os.path.realpath(expanded)
        if os.path.commonpath([allowed_real, candidate]) == allowed_real:
            return candidate
    raise ExportPathRefusedError(
        f"{param_name} {output_tsv_path!r} is not contained in any operator-configured workspace root ({config_key} in the {plugin_name} config)",
    )
