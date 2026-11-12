"""Typed operations that move a local file: Metadata deploy packages and ContentVersion files.

Each is one documented vendor resource over the same pinned-org transport as
the other typed operations:

- Metadata API Developer Guide, "Deploy Metadata with Apex Testing Using
  REST": ``POST /services/data/vXX.X/metadata/deployRequest`` takes a
  ``multipart/form-data`` body whose ``json`` part holds ``deployOptions`` and
  whose ``file`` part is the deploy ``.zip``. The package is read from under
  ``registered path addresses``, exactly as a Bulk ingest CSV is.
- REST API Developer Guide, "Insert or Update Blob Data": a non-multipart
  ContentVersion insert carries the file base64-encoded in ``VersionData``.
  The vendor limits that form to 37.5 MB of base64 data and enforces it
  itself; the plugin adds no limit of its own.
- REST API Developer Guide, sObject Blob Retrieve:
  ``GET /sobjects/ContentVersion/{id}/VersionData`` returns the raw file
  bytes. The ``sf`` CLI's ``--include`` transcript is text, so the download
  uses ``--stream-to-file``, which prints no status. The bytes are therefore
  kept only after they match the version's own ``ContentSize`` and MD5
  ``Checksum``, read first from the same record; an unverifiable file is
  kept with an advisory, a mismatching one is removed.

Nothing here classifies reads against writes; Salesforce permissions decide.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import uuid
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, cast

from .client import SalesforceCliExecutor
from .constants import ERROR_API_ERROR
from .errors import SalesforceServiceError, TypedFamilyError
from .request_actions import Forwarded, forward, stream_to_file

PathGate = Callable[[str], str]

# deployOptions members from the deployRequest guide, with their vendor types.
_DEPLOY_BOOLEANS = ("allowMissingFiles", "autoUpdatePackage", "checkOnly", "ignoreWarnings", "performRetrieve", "purgeOnDelete", "rollbackOnError", "singlePackage")
_TEST_LEVELS = frozenset({"NoTestRun", "RunSpecifiedTests", "RunRelevantTests", "RunLocalTests", "RunAllTestsInOrg"})
# The ContentVersion fields that prove a downloaded file.
_VERSION_FIELDS = "Id,ContentDocumentId,Title,PathOnClient,FileExtension,VersionNumber,ContentSize,Checksum"
_LOCAL_PATH = {"zip_package": "zip_path", "file_upload": "file_path", "file_download": "output_path"}


@dataclass(frozen=True)
class TransferGates:
    """Containment gates for a family that both reads and writes local files."""

    read: PathGate
    write: PathGate


def local_path_parameter(transfer: str) -> str:
    return _LOCAL_PATH[transfer]


def validate(transfer: str, params: Mapping[str, Any], body: Mapping[str, Any] | None) -> None:
    """Dispatch-time shape checks; containment runs on the worker, at the moment of I/O."""
    key = _LOCAL_PATH[transfer]
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise TypedFamilyError(f"{key} must be an absolute local path string")
    if transfer == "zip_package":
        if not value.endswith(".zip"):
            raise TypedFamilyError("zip_path must name a .zip deploy package")
        _deploy_options(body)
    if transfer == "file_upload" and body is not None and "VersionData" in body:
        raise TypedFamilyError("body must not carry VersionData; the file at file_path becomes VersionData")


def _deploy_options(body: Mapping[str, Any] | None) -> None:
    options = body.get("deployOptions") if body is not None else None
    if not isinstance(options, dict):
        raise TypedFamilyError("body.deployOptions must be an object")
    checked: dict[str, Any] = options
    for key, value in checked.items():
        problem = _DEPLOY_OPTION_CHECKS.get(key, _any_value)(value)
        if problem:
            raise TypedFamilyError(f"deployOptions.{key} must be {problem}")


def _any_value(_value: object) -> str:
    """deployOptions members the guide adds later pass through; the vendor decides."""
    return ""


def _boolean_option(value: object) -> str:
    return "" if isinstance(value, bool) else "a boolean"


def _test_level(value: object) -> str:
    return "" if value is None or value in _TEST_LEVELS else f"one of: {', '.join(sorted(_TEST_LEVELS))}"


def _run_tests(value: object) -> str:
    names = value if isinstance(value, list) else None
    valid = value is None or (names is not None and all(isinstance(name, str) and name for name in names))
    return "" if valid else "a list of Apex test class names"


_DEPLOY_OPTION_CHECKS: Mapping[str, Callable[[object], str]] = {**dict.fromkeys(_DEPLOY_BOOLEANS, _boolean_option), "testLevel": _test_level, "runTests": _run_tests}


def run(transfer: str, executor: SalesforceCliExecutor, request: dict[str, Any], params: Mapping[str, Any], gate: object) -> Forwarded:
    """Worker entry for one transfer operation; returns the forwarded call (result plus vendor data)."""
    if transfer == "zip_package":
        return forward(executor, _package_request(request, _gate(gate, read=True)(params["zip_path"])), expected_media="json")
    if transfer == "file_upload":
        return forward(executor, _upload_request(request, _gate(gate, read=True)(params["file_path"])), expected_media="json")
    return _download(executor, request, _gate(gate, read=False), params["output_path"])


def _gate(gate: object, *, read: bool) -> PathGate:
    if isinstance(gate, TransferGates):
        return gate.read if read else gate.write
    if read and callable(gate):
        return cast(PathGate, gate)
    raise TypedFamilyError("this operation needs its local path gate")


def _package_request(request: dict[str, Any], zip_path: str) -> dict[str, Any]:
    """The documented two-part body: ``json`` (deployOptions) then ``file`` (the zip)."""
    if not zipfile.is_zipfile(zip_path):
        raise TypedFamilyError("zip_path is not a zip archive")
    with open(zip_path, "rb") as handle:
        package = handle.read()
    boundary = f"----salesforce-plugin-{uuid.uuid4().hex}"
    while boundary.encode("ascii") in package:
        boundary = f"----salesforce-plugin-{uuid.uuid4().hex}"
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="json"\r\nContent-Type: application/json\r\n\r\n'.encode("ascii") + request["body"].encode("utf-8") + b"\r\n",
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="deploy.zip"\r\nContent-Type: application/zip\r\n\r\n'.encode("ascii") + package + b"\r\n",
        f"--{boundary}--\r\n".encode("ascii"),
    ]
    headers = {key: value for key, value in request["headers"].items() if key.lower() != "content-type"}
    return {**request, "body": b"".join(parts), "headers": {**headers, "Content-Type": f"multipart/form-data; boundary={boundary}"}}


def _upload_request(request: dict[str, Any], file_path: str) -> dict[str, Any]:
    with open(file_path, "rb") as handle:
        data = base64.b64encode(handle.read()).decode("ascii")
    return {**request, "body": json.dumps({**json.loads(request["body"]), "VersionData": data})}


def _download(executor: SalesforceCliExecutor, request: dict[str, Any], write_gate: PathGate, output_path: str) -> Forwarded:
    """Read the version's size and checksum, stream its bytes, keep them only if they match."""
    headers = {key: value for key, value in request["headers"].items() if key.lower() != "accept"}
    record_path = request["path"].removesuffix("/VersionData")
    forwarded = forward(executor, {"method": "GET", "path": record_path, "query": {"fields": _VERSION_FIELDS}, "headers": {**headers, "Accept": "application/json"}}, expected_media="json")
    described = forwarded.raw_result
    status = described.get("status")
    # The proof needs the vendor's own ContentSize and Checksum.
    record = forwarded.raw_body
    if not isinstance(status, int) or not 200 <= status < 300 or not isinstance(record, dict):
        return forwarded
    destination = write_gate(output_path)
    parent = os.path.dirname(destination)
    if not os.path.isdir(parent):
        raise TypedFamilyError(f"the parent directory of output_path does not exist ({parent}); create it first")
    handle, partial = tempfile.mkstemp(dir=parent, prefix=".salesforce_download_")
    os.close(handle)
    try:
        correlation_id = stream_to_file(executor, {"path": request["path"], "query": {}, "headers": headers}, partial)
        proof = _proof(partial, record)
    except BaseException:
        os.unlink(partial)
        raise
    os.replace(partial, destination)
    # Returned to run_family as received, with the proof.
    return replace(forwarded, raw_result={**described, **proof, "output_path": destination, "download_correlation_id": correlation_id})


def _proof(partial: str, record: Mapping[str, Any]) -> dict[str, Any]:
    """Compare the written bytes with ContentSize and Checksum; a mismatch keeps nothing."""
    digest = hashlib.md5(usedforsecurity=False)
    with open(partial, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    size = os.path.getsize(partial)
    expected_size = record.get("ContentSize")
    expected_md5 = record.get("Checksum")
    if (isinstance(expected_size, int) and expected_size != size) or (isinstance(expected_md5, str) and expected_md5.lower() != digest.hexdigest()):
        raise SalesforceServiceError(ERROR_API_ERROR, "The downloaded VersionData did not match the version's ContentSize and Checksum; no file was kept.")
    proof: dict[str, Any] = {"bytes_written": size, "md5": digest.hexdigest()}
    if not isinstance(expected_size, int) or not isinstance(expected_md5, str):
        proof["integrity_advisory"] = "ContentSize or Checksum was not readable for this version, so the bytes were kept unverified."
    return proof
