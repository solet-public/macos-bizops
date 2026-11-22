#!/usr/bin/env python3
"""The platform ships no project work model: the seed carries none of its names, and a session
starts, lists, meters and stops from generic fields alone, with an empty HOME and no
project tool in reach. Plan r74-d1-z1 (upl_ca45e914b0b74a5caf03c4967e6d468a version 9) of
Change chg_9a9da28b, requirement no-project-work-model on Problem prb_bc725a48.

Three legs, each reported on its own. The smoke exits 1 when any check fails, 77 when every
check that ran passed but a leg could not be measured here, and 0 otherwise.

SCAN. The paths and bytes of the union of every seed profile, as the seed factory derives
them (its bundle resolver and its manifest predicate over this tree's Git listing), are
matched case-insensitively against the plan's vocabularies: N, the external work
register's names; C, the removed model's concepts; W, the removed fields as whole words;
plus the three retired table names and the retired payload digest. The vocabulary is
assembled from pieces at run time, so this file never matches itself. A matching line
passes only as a reviewed line: its exact bytes hash to a recorded SHA-256 at a recorded
path, no more often than recorded. The plan's kept lines (6 upgrade inputs, 48 upgrade test
inputs, 34 negative assertions) are also bound to a site (the enclosing function, ``module
constant NAME`` or a schema constructor's argument) and are printed one by one; the reviewed
homonyms (an ordinary or vendor sense of a wider word) are bound to their path. Anything
else, a matching path, a changed byte, an extra copy or a moved line, is a finding printed
with its path, line, tokens and profiles. An unreadable file or an empty profile fails.

The factory is imported by module name at run time, never by an import statement: a born
clone carries no factory, and the seed's contract scanner refuses a shipped smoke that
statically imports an unshipped plugin. In a born clone (``PROVENANCE.json`` at its root, the
convention quality_gates/macos_bizops_identity_gate.py uses) the scan reads the files of the
clone's root commit from Git objects. That commit records the seed exactly as it was
installed (the seal's commit in the born-clone gate, the first commit genesis makes in a
newborn), so nothing a solet wrote since is read: not its owner's work, not the instruction
files hydration renders, not runtime state or another smoke's scratch. A listed file another
process removes before it is read is an advisory. Where nothing can be enumerated (no Git
listing, not exactly one root commit, neither the factory nor a seed's marker) the leg is
reported unmeasured, never clean.

Built-in controls rerun the scanner on scratch copies of real files and require each
introduced line to be reported where it was put, and nothing else: a new token on a homonym
path, a test, a gate, the quality service and role binding; a matching path; an unshipped
file (ignored); an upgrade input changed by one byte, duplicated, moved to another file and
moved out of its site; a third class-column line in a test, in a comment and as a copy; for
each amendment reason one changed byte, a duplicate, a move to another site and to another
file, and a new line naming a retired table; two lines the earlier pieces removed; and a
born clone read from its root commit, before it has one and after its owner wrote more.

RUNTIME. With HOME at a new empty directory, every variable carrying the register's prefix
removed, and subprocess.run, Popen and os.exec* refusing any argv naming the register CLI or
git, spawn_session starts a session from runtime, model, effort, workspace (a new empty
directory), first message, permissions and label only, with and without a calling session;
list_sessions by label returns it; budget_report by label meters it as one entry;
retire_session stops it and leaves the workspace untouched. A model cell a policy rule
turned off is refused naming the rule before any row is written, and spawn_session declares
exactly the generic parameters. The launch fixture and recording host are
generic_spawn_smoke.py's.

UPGRADE. With a private PostgreSQL (PRIVATE_TEST_PG_MANIFEST), the predecessor schema is
installed through the state service and populated with session_lifecycle_store_smoke.py's
fixture. The release's whole messaging declaration then installs through SchemaManager and
the session origin classifier runs through backfill_session_origins, the entry the plugin's
startup calls. The smoke reads back retirement, columns, rows, origins and the completion
marker, stops the state plugin and starts it again on the same database, where the
classifier returns already_done without writing its marker and nothing is retired again.
Without the manifest the leg is a disclosed skip.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/no_project_work_model_smoke.py
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import os
import re
import subprocess
import sys
import tempfile
import traceback
from collections import Counter
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import generic_spawn_smoke as launch  # noqa: E402 -- importing it puts the platform and plugin sources on sys.path
import session_lifecycle_store_smoke as upgrade  # noqa: E402 -- importing it adds the state plugin source and the checkout root
from ananta.config.core_schemas import CoreSchemaDefinitions  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from ananta.llm.agent_messaging.state_results import require_completed, require_records  # noqa: E402
from ananta.services.schema_manager import SchemaManager  # noqa: E402

from agent_messaging_plugin import session_origin_backfill  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.schema import TABLE_MANAGED_SESSION, get_session_lifecycle_schema_definition  # noqa: E402
from agent_messaging_plugin.session_lifecycle_store import read_managed_session  # noqa: E402
from quality_gates.git_environment import scrubbed_git_environment  # noqa: E402
from quality_gates.private_test_pg import MANIFEST_ENV, LedgerFixture, ledger_fixture, skip_fixture  # noqa: E402

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


# --- The plan's vocabularies, assembled from pieces so that this file never matches them ---
#
# The name vocabulary is written as patterns whose bracketed classes keep every literal (and
# every literal joined from pieces) from spelling the name the seed's identity scan reserves.

_NAME = ("project[-_ ]?solet", "p[s]olet")
_PRIMARY_WORDS = ("unit" + "_id", "lane" + "_id", "char" + "ter", "dispatch" + "_id", "budget" + "_line", "role" + "_class")
_PRIMARY_PARTS = ("Unit" + "Assign" + "ment", "managed" + "_dispatch", "lane" + " work" + "tree")
_WIDER_WORDS = (
    "lane" + "_id",
    "unit" + "_id",
    "assign" + "ment",
    "brief" + "_ref",
    "dispatch" + "_id",
    "dispatch" + "_kind",
    "reviewed_report" + "_vendor",
    "pair" + "_id",
    "scope" + "_tags",
    "capability" + "_floors",
    "difficulty" + "_score",
    "selection" + "_receipt",
    "work" + "_class",
    "budget" + "_line",
    "worktree" + "_disposition",
    "provisioning" + "_mode",
    "lane_repo" + "_root",
)
_FORMER_PARTS = ("lane" + "_char" + "ter", "assign" + "ment_sha256", "managed" + "_dispatch_event")


def _words(words: Sequence[str]) -> str:
    """A pattern that matches any of ``words`` as a whole word: an underscore or a letter
    joined to either end is part of a longer name and does not match."""
    return r"\b(?:" + "|".join(re.escape(word) for word in words) + r")\b"


def _parts(parts: Sequence[str]) -> str:
    """A pattern that matches any of ``parts`` anywhere, inside a longer name too."""
    return "|".join(re.escape(part) for part in parts)


_VOCABULARY: dict[str, re.Pattern[bytes]] = {
    "name": re.compile("|".join(_NAME).encode(), re.IGNORECASE),
    "primary": re.compile((_words(_PRIMARY_WORDS) + "|" + _parts(_PRIMARY_PARTS)).encode(), re.IGNORECASE),
    "wider": re.compile(_words(_WIDER_WORDS).encode(), re.IGNORECASE),
    "former": re.compile(_parts(_FORMER_PARTS).encode(), re.IGNORECASE),
}

# --- Reviewed lines (plan version 9), materialized so the scan runs offline ---
#
# _KEPT_LINES: the plan's records as (path, site, reason, the SHA-256 of each kept line's exact
# UTF-8 bytes), each line allowed once at its site. The six upgrade inputs equal master
# b24df6dc9 and the landed blobs of c1 (merge a4a5fe508) and o1 (merge 4c7780923, the two
# classifier lines); the 48 upgrade test inputs and 32 negative assertions are the amendment's
# 18 groups; the other two negative assertions are the class-column records, one of which
# shares its site with an amendment group. Release 75 removes the upgrade inputs.
# _HOMONYM_TEXT: the plan's baseline of reviewed homonyms (510 occurrences in 180 paths) resolved
# on master b24df6dc9 to the digest of each occurrence, repeated once per allowed copy;
# one listed line (a curation report process description) has since gone, which is allowed.

_UPGRADE_INPUT = "upgrade input"
_UPGRADE_TEST_INPUT = "upgrade test input"
_NEGATIVE_ASSERTION = "negative assertion"
_PIN_MAXIMUM = 1

_KEPT_LINES: tuple[tuple[str, str, str, str], ...] = (
    ("plugins/agent_messaging_plugin/src/agent_messaging_plugin/schema.py", "SchemaDefinition.retired_tables", _UPGRADE_INPUT, "abda57808e34010e8c0618b0172ab7af6fad267ef747276bff89408cb3d54714"),
    ("plugins/agent_messaging_plugin/src/agent_messaging_plugin/session_origin_backfill.py", "_backfill_row", _UPGRADE_INPUT, "c2980fc970157a7e6aca5397272f90af89de06a6ca7bbbcb4a09a0da1ac0f803 7120d3c05736b9b4a776524a3c7cd229b1a38611350a12a5789d1d2264b73ddb"),
    ("plugins/agent_messaging_plugin/src/agent_messaging_plugin/schema.py", "managed_session.columns", _UPGRADE_INPUT, "7a171194552c82bf79a0ab876ed43400f4d1a684b4adb60a8cf9bb775aadfcac ddafcaebf1a0fe4ac42cb29c26fecca106cd4adcd4087bda83f78c6864a58813 dbd288ea7a434f4011155ecae719230e9ab9a3ab5b6614e1e9c55db03ab2e44b"),
    ("plugins/agent_messaging_plugin/tests/role_authority_smoke.py", "module constant _CLASS_COLUMN", _NEGATIVE_ASSERTION, "1bab4bb8ce4428bd6c4c4234fab8c4b268c52449263b121c94a4628d87c94d20"),
    (
        "plugins/agent_messaging_plugin/tests/generic_spawn_smoke.py",
        "module constant _OLD_FIELDS",
        _NEGATIVE_ASSERTION,
        "0107ea24bdaf76b67c1da83ed54fe5b471b213f1745d25c412e242d8fce05f24 a0afcaed939bde5131ff54595deeb712a511ed303f5ac37bd000f148c2a7dba5 d406b613d13a5e463b8c287d08721e9098892f6b46b30c6c400f2f6dd0bc6f3e 638eda04c3a209679eef84629f973be1eefa48aa498f69c9bfdc1ab15a7c157d 04c80f1c7b4d4cee4fe2ea07e8723a43c369520aeabd5c362affebba8a121a9d c8a664ac8825a721648efcbd7623c8373cba5213257a8338d91806cfacef299d b9d061621f29806e79f8752ec0813c2d1ec7f43020d1e93ebf0c23948914bfe8 2d07df9a5d7b61dcab26029b48718bfcddcd79b851fb6913cbba03c859a9059d 73ba8355d2733cd553c4829a7779b5c18beb98bf5b1823f9d87c0db0e2f0d6aa 6cbbe41a13bdbd8464faae9dac682e87dcb837a90aeabe592f79f99fde5ace18 338129311bd21a23f4f1f343f90ebe3f2ad04cebfaab93be2e5987bdead149b3",
    ),
    ("plugins/agent_messaging_plugin/tests/session_origin_backfill_smoke.py", "seed_legacy", _UPGRADE_TEST_INPUT, "b1f37f95580ddffdb37062ec2f3a47160c88d7ccccd096af60108459f1dfb5c1 3f02e1052ede6214a165937525d1bdd1be2e261d41cb374b707d880baa9eb1a1 b3b8490dc9b40039a2dcf857e044e33f3818d6bd9d4a27c8858380f7b7795964"),
    (
        "plugins/agent_messaging_plugin/tests/session_origin_backfill_smoke.py",
        "check_page_fault_and_retry",
        _UPGRADE_TEST_INPUT,
        "ef21cf4744b721366b315597d76bdf886556f2ec7fec0467e5677ae404765fb4 3f02e1052ede6214a165937525d1bdd1be2e261d41cb374b707d880baa9eb1a1 c9732d72c37db45c8e827b095b3872fd2156a270f53faa03c09e3f5ffb4ec040",
    ),
    (
        "plugins/agent_messaging_plugin/tests/session_origin_backfill_smoke.py",
        "check_compare_and_set",
        _UPGRADE_TEST_INPUT,
        "f6a19558344542d4e6f180121ef3873883e0623f177852a9e3e6cdf2e7bc235b e30525c53d19345b524c571f55af5ea5359df5704438caaae2a05e06a657d016 46ca684300ee9aa0ee4d2f7d46dc419b16fa0f80a37bac40846f330d98a9f595",
    ),
    (
        "plugins/agent_messaging_plugin/tests/session_lifecycle_store_smoke.py",
        "module constant _REMOVED_SESSION_COLUMNS",
        _UPGRADE_TEST_INPUT,
        "86c420ee51d601cebf2e750973020de0fba609452b830bfad2b8412ee6e8ac38 b895b3a9328bc493a602d9007f41e7a759e7d60eb7d5f602f8b72197e765e36e 20cc9978286164f081a31809a4cfbe62629be2e636ea0207bad63d335a45d1e4 a883d9d7907c695b07988a0976ab0c7e1d49db1906eeba86bf72e5a1c1193d01 9fd5bcc1ab45b875b990f0e839df7e7d7ea42bc93cb0eaeeff02baa66a6c2c68 7325425486191bb0bb7b11b1398c57377477fc26b5dfa9c84f20944663ccb23d 1d5467dd980b457bd3be3768e284dbbcb77392a2f00fd7fe079a6d1b886863fb 99feedf18cc7baf212f962c0c5a1c09e787102ec0bc20f5b40c63bc15cb5f6df 30b15c7f79a981c2df970568bfed3c8ec7b67f08ac45b9fc6c1509dae69c57a1 4a2edfd81896f9f02345c61cb415afacb2063cb518f87ca8799063f213658ae5 5dc49c15a601a4fd685a2b83ee1d76a0dec23c56fd3d3a4006c11cce038bf28a 1a2c9e80459733d55eecc42b6b8073ffa5dbaadcdbfa2ed36d6cc89068cada1f 14fd3432eaeb67844b64789a6d393274cf0199e2ceca2a119ce6b24c4c141096 f139acc0658d102fe9042fb5336d5defc8c51a21bebbfb4cb0e857a0c4c0bd5b e8c4c2c4ba36590f2ae7b56a20b360660fb5e7b1c0784d9ed0d0f3add12dafd7",
    ),
    ("plugins/agent_messaging_plugin/tests/session_lifecycle_store_smoke.py", "module constant _REMOVED_SESSION_INDEXES", _UPGRADE_TEST_INPUT, "03c1f5d1c9cd9f79c8868a489ef12024e9ca1491b5a178a0c935e16b178dfe47"),
    (
        "plugins/agent_messaging_plugin/tests/session_lifecycle_store_smoke.py",
        "module constant _RETIRED_TABLES",
        _UPGRADE_TEST_INPUT,
        "2fb9d31d3c5852106bb539e50622f6bcc1ef3d7c3d2ec941c771a2c3ea48b412 b70e2fbb7ca63ab7aa6764fe5233c23d1510d99578904518b88c52d45e53be75 f8ee91e7f6073621e07b36ebca6c31efa6b16363d44364b6add67289f8dcc633",
    ),
    ("plugins/agent_messaging_plugin/tests/session_lifecycle_store_smoke.py", "module constant _HISTORICAL_SESSIONS", _UPGRADE_TEST_INPUT, "70e65fc86bf8db3160abfe95433fab841387b68731749d9c8b698ac75fe49417"),
    (
        "plugins/agent_messaging_plugin/tests/session_lifecycle_store_smoke.py",
        "_seed_predecessor",
        _UPGRADE_TEST_INPUT,
        "1fc59122d7da2e8f423716942e8a224dd0f92ee858c091d74b1b582c505e262e b1f37f95580ddffdb37062ec2f3a47160c88d7ccccd096af60108459f1dfb5c1 0f90b5659ca9065ae42b81bf4cd80c9780ccfe032fc72c9e9105b1332d66ccfb ed5bb678354bafa27136e0bf2ad479d0d78dfcf9d98e25adf4b8c1b9489924eb 6b3c95d09d8367a57e4ea5d2cdb980370a7180e4470f6259081c5014c4991b42 f7c336578d554b9815e6c614316633aa753120bdee110958e387afccbf216495 c5978a5619239d28edeb79a6bbc55080c7804facf982fca5ebe27ca96e279908 20d11a5212b6d0dc95844ae845245a127b1d77b80f7b07a9cefacdcd8f0efebb 4719abf6d3e73b8da0a04108cfb28641a8f6b2e4431b5140f93ca99c73345324 b6f28275af1c07098dbddfd4fa0b22f627dfe47ef9805a4ff7a1396d35c33afd 5a008deb6740d8d5ff47753c0cfe9cdfbec50d70cfac1b4054b003ccc582b5a6 5f93937751dc12d2710d57925afa70ca6ce14aab6edfe39570d5096505a5da2a 9a195e76b840056a137672c8e38607f1d0bafde15ef7de9d4f16e6ba3c39e45d 7be99362b9e8889a9e37b1c66c0848cfc8f354588cd7bc6a038b1ad7dee1a943 df48f148e125810341f21787296ace05e0ddeaacca49c4b02588d13218b240fb 3a58519361b6b48645ff660d4c4ddbc45a5c4c61ef913b17f0a5d8283468d4dd 451749f5d46ad013e2e6493bf9bcc5e306e6c5977a9fe4fe9b7719f843c81bb2 826b9255afa4713b348eaa2efd73ad00766934c4716195dacb622a4a372ffefc 1432ccbc62d17e5510036d785637d96421b661b6ddd5c34c93f15f2606b4b084",
    ),
    (
        "plugins/agent_messaging_plugin/tests/session_lifecycle_store_smoke.py",
        "module constant _RETIRED_CONSTANTS",
        _NEGATIVE_ASSERTION,
        "e322c0f0f9a8ef0071b3e7b148b7eef3fcaea5f6a284d4bd0bbd7bd770196a0d c51e5770ae03e7f582fbe6bfe668c6fe29bb73bc22709edba60f8743e0a710bc dbe62a6dd07fe28bb38b5ac3cabb8fa255d45da501b44e30b66beb64ca965841 b8d81df9133fa2effe2937560e5395b7598cf284be55bf4f6ec972fe7a0df470 8bf16f075abfefd622e38c16f59db9b22aacd392ae29b0ee160c2ffdbc84b1c3 b1993e01eba75d4a42d87408913ba8bf2997ad45babc2b4651aa8256805cbb7e",
    ),
    ("plugins/agent_messaging_plugin/tests/session_lifecycle_store_smoke.py", "test_schema_holds_no_work_model", _NEGATIVE_ASSERTION, "7c0a87cdcd25c2fc7ff42a775886005e317c4f0c86d8080c8107e2f0cbcb9b0b"),
    ("plugins/agent_messaging_plugin/tests/session_lifecycle_verbs_smoke.py", "test_list_and_status", _NEGATIVE_ASSERTION, "649785b9ecf445d811b31bfb066601916dfd6ef2eba5168fa542448fcb0b8051"),
    (
        "plugins/agent_messaging_plugin/tests/generic_spawn_smoke.py",
        "module constant _RETIRED_WORK_VERBS",
        _NEGATIVE_ASSERTION,
        "6b8068012cb14d54d60790a3616181e937fc66b15585c5a25deecfd2663c1667 c7dcdd58faa837a766e2f69a7a9a24cc96088ff80e76bad2e288d8c34a645998 6197df91390ee503712ce78cebdb41e4ba272a21bc00541a65422e818c517c06 34b683c8c84a17eac16f912534c7e65f5bb71795cac8ad767e9e488a3336d925 49d5eed0fbcf5b27b2437d1c857ef8a3d81342283098c28e5ea44c40829a0d83 be836ce99f3228dec5e0599a6bf6b496bd70e80c3bdc9a72fe322e70b124e473",
    ),
    ("plugins/agent_messaging_plugin/tests/generic_spawn_smoke.py", "case_h_work_contract_verbs_are_gone", _NEGATIVE_ASSERTION, "59f7e247af79cdb071ce3727b0e77bf3c2f4f391994be646e7d2616a20f2630a"),
    ("plugins/agent_messaging_plugin/tests/generic_spawn_smoke.py", "module constant _DELETED_DISPATCH_MODULES", _NEGATIVE_ASSERTION, "f584f88cd734bce3f20cfa149ec1857f09f85d4b70a5ea53f6761d2677e4e546"),
    ("plugins/agent_messaging_plugin/tests/generic_spawn_smoke.py", "case_k_spawn_path_has_no_dispatch_contract", _NEGATIVE_ASSERTION, "f3700514ed66d0c4fa1f71b8078e850d3b16ab19bba7cfc4a1ba6e3ba465f69c 4ed90fe64a1e849b462aee092314c463e0f2ca225ba27126ab2e43a4c129e2d7"),
    ("plugins/agent_messaging_plugin/tests/generic_spawn_smoke.py", "case_s2p_host_builders_have_no_assignment_prompt", _NEGATIVE_ASSERTION, "9645e3ee215d06fd2ff8218b5329aef711eca15b08ca874fec949da41af03caa"),
    (
        "plugins/agent_messaging_plugin/tests/generic_spawn_smoke.py",
        "case_s2_retired_surfaces_absent",
        _NEGATIVE_ASSERTION,
        "a7dd37fa050159886f545ac610e71810c239d27d5df3faf3f992cb44563d3cb4 73a09a598a2466b122e62d0ff4ee62eba19129b0d64363b1c8104a70a814d9ad a7943fdf25a092046974b3f46357c2bbdd205d731444cd3ce0f0db4b590011e8",
    ),
)
# One reviewed path per line, then the digest of each reviewed occurrence there. Kept as one text
# block: these paths key a review record and are not files this smoke needs (a path a profile does
# not ship has no lines to check), while the seed's contract scanner reads every path-shaped literal
# as a file its smoke needs.
_HOMONYM_TEXT = """\
ananta/src/ananta/core/actions/action_manager.py 5382cfaaf2c62c01a59a0bc69883f74522872e7a744ab57f790f4e3cbe866fba d19abf0da5909a2c4600a8b981ae0f5eab9da97ac3cd33f13ae2c304aab31f00 d2157ab7c6a654d1bb132044a0a938593d48805bb6e351a31a2d96a2d9edf2f9
ananta/src/ananta/core/actions/action_metadata.py 8f510956a4e2c4cd740e88b66ec2f260f0532815f1f163522a79bd536c5b2159
ananta/src/ananta/core/actions/action_path_liveness.py b1cd9c1d5d7658a40a76053405702a9e2326779136060b498a49f7e1d3922718
ananta/src/ananta/core/actions/action_processor.py 87e5622b74dd01f71be802d09b75f198048a363282884282bc59a77bf6d0fc2d
ananta/src/ananta/core/actions/action_queue_poller.py b080309a76ab808e28345c224ccdd64cb5b406090e0f1273811699c88f05961d
ananta/src/ananta/core/actions/result_processing_glue.py 8c1b024db5b0ce58759cb99ebcc4dd73c1e4ebf6f6187cbab7218bb2dcb31717 6c73a944105d17cdae02d24aa31a9af019475cfb61a45e2a176ad04cd6ca1b49 bd1ae009a7b66db5851d11a771b468546c2e15cb7e557ec71926f28f397cae03
ananta/src/ananta/core/event_orchestrator.py 0d7ffd4038daa45a51ccffae20a0a37588a7435fc5d54b837a5a36898b0968a4 11e543560e43e3b513f1cf6a3f1fc29b84bf8a9ee699039ae8dcda3ef75a4b65 bdb0f22a0f3b7f5246afe57156a5898d5f826deaa5dea9d742cb4dc579aae858 22a947127dc8841c0e7755f8d8f29454766cd1ff244b98f369c068b73f20f6ec 0dfa3e6bd5495a442c62dc15755749c326acbfa357171c2dd30915fadb1986d3
ananta/src/ananta/core/orchestration/postgres_authorization_guard.py d6f4719c8aa7fe9cc90d61681463861ad259032880eb3298d5e3e3bda6988b10 72514e4819026d567c8bc36f036c94223701bf6984f9869193a65961925c4c2d
ananta/src/ananta/core/orchestration/service_manager.py cfcd0e55be7bbd5becad9e602d00bab6edb803fcdb73c6f095776f27b797bf4e e6482a4ebe4084ac9ca92c9e13ed9a954381de7ed8db3701bf7d8c89582f2adf
ananta/src/ananta/core/orchestration/tests/postgres_authorization_guard_smoke.py 6f0b67d15a5f2dd3288a8035ec50520b67567e38733225f5f797f90942d22838 2679b9239a93242d9c1daa7667e5e31d44db62591d89cdb5f5e317bd5694009f
ananta/src/ananta/core/process_registry/invocation_schema_generator.py 0b2d5972c97e41d034e90e440c90e78a48eaba84025f3516492c3e93fa109170 3f8b34d8b00d55cd6088164cacd6ef1d7584ee5f72863a17cdd2c2dd5402d5e7
ananta/src/ananta/core/prompts/api_stage/serialization.py 1601197bf645eacda4e9a1115c0da67be56596b0b101732b47dcc9aad31b49ec
ananta/src/ananta/core/prompts/plan_drivers/guidance_articles.py c614f0aa2bf7703326ae104cdc855b738bbd5e186377215ccc7a673bcf38d5e7
ananta/src/ananta/core/result_processing/__init__.py 579987e151199a6676e2bd8312193841d495a2d92f5c15508b44ccc7288d1156 6db88b6eb8465fe146a54940909174da3bc6066a1253badda83b5d45b75b386f 1cc3b844800455c551c522bf878452776bb5d2387e8f2a7fa30c55557b9bf74e
ananta/src/ananta/core/result_processing/contracts.py a3a45bafd64e5b616717613685279598ac94b788e4774599fbf2b1ee5ed15023 6d4940cef4bd4926c160fb346b643a24c56825ba5317960c5021f00a3ca0c3b4 f66c34c89dabcbce67de5bb2c9c149940c1ff13b66562fb0a6683e43b506b50e 15d77e629064223ac48c3650b453ab96f1d16c46daf64b7f5437a345372e2557
ananta/src/ananta/core/state/state_manager.py 18d9c24c97c3e232156cd314f189edfa3debaa2bc93230f857245acc35c46e58 18d9c24c97c3e232156cd314f189edfa3debaa2bc93230f857245acc35c46e58
ananta/src/ananta/core/templates/template_functions.py a23d2496408f038c2699b34652c155b2edf31fd200e02f089500013f8488631d
ananta/src/ananta/llm/agent_messaging/role_binding.py 4894832b07be4a47dbc3dde053147e570805f476761c2380ebfa35d41505ca7b
ananta/src/ananta/services/action_preparation_service.py 55db0852cd019b0770e661f0c405773cd27df1486ab57c58f1a04a40cbd9fb86
ananta/src/ananta/services/inference_service/__init__.py 890692048547fcdf092d0c546af7eaf5440098e9b5b2532d6f38e14a42bf9e15
ananta/src/ananta/services/inference_service/tests/autonomic_flip_smoke.py 638b49f7515be59ef85fe1afd3696c58ac144c5af6709cfe4f17ffaf987add77
ananta/src/ananta/services/inference_service/tests/completion_request_smoke.py 3e8de54d4faf8d18c142301703bebc93c672f0551326241500e7b3f5b025fbe2
ananta/src/ananta/services/inference_service/tests/inference_vertex_resolver_smoke.py 2663df0a0facc32afda5ee740c5f75a6fd1e61d97429b521c9066967836ebced 1732877c1c55a699183aa2df628b8bfc2845a92330e9b3a154cb60abef8fa5aa
ananta/src/ananta/services/inference_service/tests/source_namespace_canonical_get_smoke.py 3181b876bbd12c9f45282421eb88a57f6b8943d5fbd815692de5d15842d30220 3181b876bbd12c9f45282421eb88a57f6b8943d5fbd815692de5d15842d30220
ananta/src/ananta/services/inference_service/vertex_resolver.py f30e1ef1ea7f5dd14148b6667199b8c576f0dc138d07a994b565476d79334b43
ananta/tests/cli/fatal_path_logging_smoke.py f306d2ceeff7cb3fa27021afe5400dcc27f89d2f4681d6d007f63d87fdb3d32c 3c73e85067d5d9aff39e6479c9dfec6f4774bf53984be5972cc123a8c74ac33b 534e249365dd7f8d0f22718bc92ba2ea220d441d6a27af1389f2284391498b71 e34a422d183521d0e1722cadf9105e07d5a8a65c96cdf1d0a1116dc1fa9a0c54
ananta/tests/core/actions/action_event_resolution_smoke.py b83368ae76ba9a07824116387faa1a55b59e0a4f1be910fb64121151160201bf 626e97e31a3b2edccaa60ae5517dbf093580901dc0f43717ec6fa331d092fb06 626e97e31a3b2edccaa60ae5517dbf093580901dc0f43717ec6fa331d092fb06 8781df608c5ae4d8c4a7baf0b8e595e5689949a8e42bdfe7c2af7d6415ed96f3 cf8a5684286146edf79fc8b53c7bb351dbbe29345439bcb174c11ef14b3a71cf
ananta/tests/core/actions/action_path_diagnosability_smoke.py 1bff17d3e6e5c9039cdd2b9497f825771b410428551d1a2d3d0e6abc62e8f39d
ananta/tests/core/actions/candidate_starting_actions_pin_smoke.py 4742be199a36198f5d08ed93752c282a6bb6019ba8d90c57105dfeeb65f3e0a3 ef96dae947c2bd1ee55808119a2e4497989f3e5d1c082a6eedec4e4e6e0b1dac 742cf5d070bd1ec98dd16894ee8afcc4cef60210f2dde51818de7de7e52426d0
ananta/tests/core/actions/deploy_detached_from_poller_smoke.py 6241b9e6fc48ffc647e37872228c4b1099dc206769d81248a96606ce416287a4 895ff59907428bf58204ee6c39857063d59e5ca657190fed73fa755c1577e993 d42b8fc22e484daf5ef7b4a7c4dd92baf072ec10516853b6f6f4f27b355d9638 6241b9e6fc48ffc647e37872228c4b1099dc206769d81248a96606ce416287a4 ac52d9aa9a1fc8ce14e23c31ff5bd25b5189219151c779ca030799c490668827 15bf855b5c45ff025daff219506d52890acdba702d8e45bd0de26b7225be6fbc ed0a338ed94691dbb50a018b73b376c3cdbb83c301d3e695726548fd9ee6b05f 73f7f63191cb293f8afcd1a6866629aae85e62438d09b454ffefc170ac9a4a7a 1ddab8f92497ff4e1c0220e44b0b82e53031d0b1d61ca6336c83381f0e5e8bfc
ananta/tests/core/context_service/assemble_agent_context_smoke.py 726af82d476e2b60d990b4f55d9b5aba7bfb952b83e8aa21ce677644a0fec21b
ananta/tests/core/plans/plan_templates_smoke.py 0eadefa219c6c4e134d9e375b76e260304ab7405ca9d3c76157241524171b212
ananta/tests/core/process_registry/starting_actions_registry_resolution_smoke.py 7274c6dc47e437647f13e1c9748a0c10de19dfc7d906167749c9d761e26647eb
ananta/tests/lifecycle_management_service/work_folder_records_smoke.py 2c6d9dd46495fc17f57343b4aa7ae8904cdbb3efc89ab4ef0f6f24577ab12769
ananta/tests/llm/session_ledger/embedding_token_budget_smoke.py 2e1e67eae688c250b11978d4546ad3557fe58d93459b658ac3e2bc10935207c6 2e1e67eae688c250b11978d4546ad3557fe58d93459b658ac3e2bc10935207c6
ananta/tests/llm/session_ledger/ensure_periodic_poll_smoke.py 88a1952c115d8da97fc39a4e969518687ef03e980758d5f95adb3a5ad39b1b15 b4c518c49ee50e4f42713fd4f703eb4fa106f106b3244af25feaddd70be8d408 087e96d2708b94eb5f981dfe2870c7fe83dbcde1c542ea46eb7d23cf22a3492a 7d0d7a761d67e478b92c25140de912f2a369fdfe364234bc7b00a5cb109a66e2 959693a75372f724758ffa67255bb632a565a95166003efe4cc24e42127d0abe 3978290fcaa8387037736105afafeb7a0ee4ede1152902acefee553c3b6a51c6 3ec69949ce5e54651a6c3a0e867c8369a4ca232d09c4c9d15ff41d9d0c91b0ec
ananta/tests/llm/session_ledger/event_embed_drain_progress_smoke.py cf447315fa701a261c09cea005acb469522632852f77d25408c72e13c9de1097 8493ad1ab619d118619364e5a33db3b226dee55bd88dd2ec20e60090908685d6 f2ab8521fd915fea0f6081dff266f2da5b9b5ba8c59be193066518f0156d4544 cf780254fe61c19a3d6c0b36aa53a6420354c687559f85d70a6af12243ac766e 504a34421ecf256f71a95f1a8946f9fd76de6bd86aca77618ee637d7369c648d d287222d41c19b8ae247ece5d9be7f81c2221f9ce321520c733bfa0dce9f94f9 f904afbc745227e9a43a6e23290530e2286199a92cab83287ec4649746c385af
ananta/tests/llm/session_ledger/integration_smoke.py 6fc4ce31a20dd291fc9114647aeeabc8acbc1756d92aa8c926f8f5e92424df90
ananta/tests/llm/session_ledger/inverted_bounds_repair_live_smoke.py 260006a098176e2de4507d71e2246a2b0d2b8165aea98c909d2b58555928d53c
ananta/tests/llm/session_ledger/lift_canonical_pointer_smoke.py b16da56598aeb300041f728cbc74180e7e5f18e581f100f2f3b5dd73bf277640 eac81dbcb2ec5ce18c50555e2fb19dab536b70f5b5bfd3ea429682db68073256 88597d5f8261dea802b45bc4f41eaa16497a5fbff43cd0222f39d62303e728e5 98f049e3b87b978f7a273b7b8235c0d1ae4c83f511ecd40d34fe6743e6984a02
ananta/tests/llm/session_ledger/m6_wiring_smoke.py 88a1952c115d8da97fc39a4e969518687ef03e980758d5f95adb3a5ad39b1b15 53b1c8bfe2d1cfa19b6b712771348a5ded637690ac84c201898d0eff5d38433c 087e96d2708b94eb5f981dfe2870c7fe83dbcde1c542ea46eb7d23cf22a3492a 7d0d7a761d67e478b92c25140de912f2a369fdfe364234bc7b00a5cb109a66e2 959693a75372f724758ffa67255bb632a565a95166003efe4cc24e42127d0abe bd25e86f71ebeea6ab76fcff58ab5111b05ca15188b6fdcf0df20bf36902b497 7bf5dacb5c0b5f852ae22f498605e874009cd35d29a9e2826e401ac8a60b6d5a 3ec69949ce5e54651a6c3a0e867c8369a4ca232d09c4c9d15ff41d9d0c91b0ec
ananta/tests/llm/session_ledger/quiescent_drain_smoke.py 04eea8b1640e3e224dcc50b846e0ad0fb62873f5d7d10cd7a8bb9058cce6880c 53b1c8bfe2d1cfa19b6b712771348a5ded637690ac84c201898d0eff5d38433c bd25e86f71ebeea6ab76fcff58ab5111b05ca15188b6fdcf0df20bf36902b497 be38ad20640367ad1322756276cd16a421b3d08bc6870637a70e43b14ea501a5 6b5d2d0a39ecc8cac6fe424dece4ebc1a22cad4d2f019a6192b6f97d312a8e82
ananta/tests/llm/session_ledger/reset_ingest_state_live_db_smoke.py fd1ebabe371b2ad20c0156ebe43e93dac6d1ab103a8ed19370c625a1a54641e7 b16da56598aeb300041f728cbc74180e7e5f18e581f100f2f3b5dd73bf277640 04fa80162e77447ac7a7d00d4b5aa52f7a979cd05e8cd06ae098c8d7c7d6a3b7 06ab5e4baaa992325029ee7c1736548e820452ba790b9046f0760f01561aea65 0dcd848b58cf684c39be24adb83f4d1c24f1a164d7fda8933eaf6117290da05b d5326246119f6656035510bcb3c870517a3d0da9bd5229a806fbdb8534556370
ananta/tests/llm/session_ledger/reset_ingest_state_smoke.py fd1ebabe371b2ad20c0156ebe43e93dac6d1ab103a8ed19370c625a1a54641e7 b16da56598aeb300041f728cbc74180e7e5f18e581f100f2f3b5dd73bf277640 04fa80162e77447ac7a7d00d4b5aa52f7a979cd05e8cd06ae098c8d7c7d6a3b7 06ab5e4baaa992325029ee7c1736548e820452ba790b9046f0760f01561aea65 0dcd848b58cf684c39be24adb83f4d1c24f1a164d7fda8933eaf6117290da05b d5326246119f6656035510bcb3c870517a3d0da9bd5229a806fbdb8534556370
ananta/tests/llm/session_ledger/summarize_outcome_visibility_smoke.py 53b1c8bfe2d1cfa19b6b712771348a5ded637690ac84c201898d0eff5d38433c bd25e86f71ebeea6ab76fcff58ab5111b05ca15188b6fdcf0df20bf36902b497 c5f28c71f1a1a38fd31c6b4471e7ab8e618ec7e453cf8559418701261aa889b2
ananta/tests/llm/session_ledger/trigger_poll_offloads_drain_smoke.py d53c39148d3531e455862be9e5d565b55d89794d674e19c0691ae378b19ed380 6243e79d210603ac960c6f5dffabcde812947067b25d9bac5c2f44f5a3fcc2e4
ananta/tests/platform/install_plugin_from_path_smoke.py 7b52c6b1ceeef9a525258868b832f164f4b947c3e660db7b9df5a8b689447d6f
plugins/actr_memory_plugin/tests/actr_vector_rebuild_live_smoke.py 89f3acfd01b53191d97e9a2e4d4334ec3525815fe465f262bb5d9facb903ff12
plugins/actr_memory_plugin/tests/config_binding_order_smoke.py 7f3fc97d258e666d4ef06fdd20b3563e3a68284b810d8f7e3a1ae5801249c22b 3dcf18be33e3915d205004ab60773c0b344616815277a083a3a4425dc27f8776
plugins/actr_memory_plugin/tests/cron_remediation_smoke.py 7274c6dc47e437647f13e1c9748a0c10de19dfc7d906167749c9d761e26647eb e1e9a9ed9d131bb8f37bc9c708d798c01ef04140e9cd4ee0936e2e9a50507f09 c0711294757f3cb52ad51c7355396b12495d8f069ed9c67266fdda3944b604c0
plugins/actr_memory_plugin/tests/setup_schedules_canonical_smoke.py 7274c6dc47e437647f13e1c9748a0c10de19dfc7d906167749c9d761e26647eb 631f8ce14cb692d20c6d7875dbe1fb8f34f8b645b0402b407abaa3344be76b0b 383476d6ef2aec3e3a53d6bb800a96616831e6b303a33945ad007107cfa67fa3 cb812bbcc6d9827725cbafb81f07b8b91c90be81033dde07144e53f998d0e477
plugins/actr_memory_plugin/tests/setup_schedules_real_scheduling_service_smoke.py 98caf35f747d0fc5264edc5c9c19278039396a907068bfedcacd4cf3700a61d6 7274c6dc47e437647f13e1c9748a0c10de19dfc7d906167749c9d761e26647eb 631f8ce14cb692d20c6d7875dbe1fb8f34f8b645b0402b407abaa3344be76b0b bb236e142541f85d66bf0ecf2f4cfc41484dccdf1be313771e50e4f9cb785946 c0711294757f3cb52ad51c7355396b12495d8f069ed9c67266fdda3944b604c0 cb812bbcc6d9827725cbafb81f07b8b91c90be81033dde07144e53f998d0e477
plugins/agent_messaging_plugin/knowledge_base/03_inter_agent_messaging.md 8bb686467666a82620a2d30474627b0fb93418dee42c1fd4bcf4fed9d9ba336c
plugins/agent_messaging_plugin/knowledge_base/10_system_slots_and_claim_gate.md b0b21fa402c0db3037b1dad3e2ad4bf620d7dcad5b2af9c7aafbd8c43385373a d7abbb56b8156833aaf6681bbb5db8d2df9eabad36028d31d3829586bc3eed71 a6aee4cb49520bf715f33db2159f778e37915b529cb45f20a9179c2e2bd132a7 fb7717f24b1b6bcced3cdd9a2cddd50399e349c50a7845cb9fa967835531c069
plugins/agent_messaging_plugin/knowledge_base/processes/read_model_capability_catalog.json 4ed3551dae75c112bded9e07eab1bccfd6481a75c3fbeff73faab3b9f96f6393
plugins/agent_messaging_plugin/knowledge_base/processes/set_autonomic_slot.json 7f809a242cab3e28b64b86c1b86003673c24ae3b7d8bcbb0e4a22d0d46088b47
plugins/agent_messaging_plugin/plugin.yaml 6abb1b2672e9a184c6517be45763827783984b9043bc1d18f9acfb210fb6e35d c0fc5d5f8cfcfe1f3029fb7fe84e2f1da02173d166103094eebeee82191487e1
plugins/agent_messaging_plugin/src/agent_messaging_plugin/autonomic_assignment.py 73f5b5c3678b8a9255812e4ad1e4822bcdb08e67f64472474113702637500bdf 8c5f3a840c804291c0cfb9570ae89386a14c2a71df416c6fc27a43d595160b8d
plugins/agent_messaging_plugin/src/agent_messaging_plugin/choreography_verbs.py 4100a474c07cc70ae39f66f2367817de0fd425f71d27b856817a1847cd54e9ef
plugins/agent_messaging_plugin/src/agent_messaging_plugin/local_cli/cli.py ce2776fab690c261e507a93666792d60f14993f9969bb00bdc3255f10f4b3378
plugins/agent_messaging_plugin/src/agent_messaging_plugin/mcp_bridge/__main__.py d35562cda971f1b5311c99dd61f7724921ef1d51ffb99e03548724ac18fb80cb
plugins/agent_messaging_plugin/src/agent_messaging_plugin/mcp_streamable/auth.py 0539704b77f505a6ff772cead0ca180ed4827e2e8f432c864dcf1d9335062136
plugins/agent_messaging_plugin/src/agent_messaging_plugin/mcp_streamable/tools.py 55ba73ecec1d6980ec9fc68481090385d61d89ef0f962ba2bfa1dddbcdbb6792
plugins/agent_messaging_plugin/src/agent_messaging_plugin/operator_codex_registration.py 90fe5c9fe87ddfdcf37fcdbfab52fcb47182a4dd0e532a3719dca88ec5acd578
plugins/agent_messaging_plugin/src/agent_messaging_plugin/plugin.py fecd7ad8e0eefd0df6be8119a0407178b4f3fc63bea580ec857890c91d3d9b17 8e27ed8d51b376de4ef40a9b8af58d1cf9d1f1de257c28e1e6ba0d72f634e1c3 b8d2b44a66ee3c79ab3a98086a281d7f32141c85d0768c63d7a29775800e60af 8edfe26beb0bb7d2949cb37382e010adfe40bbc27356e0ad394ae933de7b60ec 7a81da5372fe6db859b2219d22dcff183df6a14b93657de898dbcc8e0c28ba26 0db6f5191c59a7b0401e4cfd76a9eb6478664c5f96002e7a986b0b3890f28e1d
plugins/agent_messaging_plugin/src/agent_messaging_plugin/role_binding_store.py fe8a1555524d2b49b1e80d6c884585f09fe59343b1016b98049049f09cd92736
plugins/agent_messaging_plugin/src/agent_messaging_plugin/role_claim.py 037c0a4ec1b6b1562fa66f65ec968e0110dbe3df4d25094234315b7f27f49e05
plugins/agent_messaging_plugin/src/agent_messaging_plugin/seat_rotation_helper.py 77b89fc5b299769bf1b406dfd7a265972c37539cfa6214c45f66d70fa7e627d2
plugins/agent_messaging_plugin/src/agent_messaging_plugin/system_slots.py 00d560df6e667579a543c6dc1751975552191fb9c05713dbac8366646d0e972f 0a7eecf4163a04fb4051b1e49f2c3ce6752f30b8b531e5c82a66f0349afa0d82 f72476891a921fdd7f9d2ebdc5ef30e00ca021a1bb9059b90a3d744cf163a2ae
plugins/agent_messaging_plugin/src/agent_messaging_plugin/tmux_adapter.py bcfe3d36bbd8f4380824ea4dc2948b0a144ceae47bdbaf21338b9a7d9a8c8664
plugins/agent_messaging_plugin/tests/autonomic_assignment_smoke.py 49a9f94af1197f56418816c2add377cc76bf9c9361f99f94854fa11a6fb3d896 5c6939963b4322d0163f799bf9431697bd96b75f85fabfd6807cb4fc1478368c a28ce929ea805a5110f5b5be1b88d26115de166663df4af9bcea19446ba44094 eae523238f8b87163f99659d944ee635b5fc0a2bb9b4d851c49cd90dccbdfb63 81f74dfdda46900fae2fa1964f4fce873b2c6f4c56c83816ef141604ee1ab5f2 157701f52ddc15d8f7a4f472a4557dd3748f50b3f4f7b4592e0c9aac6d324e38 c008ac5edf51d905c3792afa3c98f9a4feb8cbbedcc43e2f295d12a5d34eb28c d4a4c726ba3fc171d62d4dad51eec1bbeed6c4f77b02f02f64f8625240a72db7 a28ce929ea805a5110f5b5be1b88d26115de166663df4af9bcea19446ba44094 1e0cb9bb4c8e81b1d16b89a584249959a290e713d90214e5dfabe9aa67b2a5ca 472d5b9fb6ddc9396ed542065a329e54e795099e9bed510882505fdf6408b307 18da2a623ce8a5966d29af1e28d2458ade80660f21d6963d5c75defd35bbc3fb ca6d71b05f6e8c908a7743461658ee4eb0cc2f8ef62edaedfa2a0f9e74b2261d 7077a3586cdd91e8ee88fafae4e30aa943c15ec445899c8d05cc43e8dc08e6ee 604c0de00867594de53a997417c8ed5d82673f80e90a223fd1c620534082e26a ef25ec0ef0b3f1236b3ea992e2a17b8f175a68190f17d056e1c1519984fcd926 3f39261f302e351cbec5e565995884ab82646fcf8f7877c8bad7a0ee4b8d8442 8ae8388fb5d85d0b8b8b6525f540f1de2b5877d51b072bf5d19aaed1974e013e 472d5b9fb6ddc9396ed542065a329e54e795099e9bed510882505fdf6408b307 19702fef7a38d8b6e81a89599e915ef26d3462bec89a504a7457800fc8744f05 b8cb32561841c1eb47a10982b9c7b1238eec04f1506ff1a0481f4b1507f93af5 fde1a47ed99c536b351059a885aa77d51cac9bd342dcfafa3d42d078a93dc499 472d5b9fb6ddc9396ed542065a329e54e795099e9bed510882505fdf6408b307 19702fef7a38d8b6e81a89599e915ef26d3462bec89a504a7457800fc8744f05 3a5a414d5d496e19cd9d28c8e6c0545bf0e22aecb66f08f048c0c45676fc050e 9d965816dd634fbca2a0da1ae81c61e5c11286f7f7cc4b8fbaf01e387962b15c 0e707ed99da7d7384d27070cfb0f4172f4b364d1774e93b204a847d5ae968192 61c040f3805f03ce9db3e7a781061256e8381a34b6e125f92cb103f0a2552fbe 6f0976df89a14c9649974d3c2f76b4230812ec9a8a44351a8c87777170490e2b 472d5b9fb6ddc9396ed542065a329e54e795099e9bed510882505fdf6408b307 19702fef7a38d8b6e81a89599e915ef26d3462bec89a504a7457800fc8744f05 226bbb579b20e272475d3c2a85e9b0803a9f3f3ea997866e4d63db8bd1401202 3d19d3295bdb6e2bd87c549db312ab691ed4c3d1e45bd466b2958d4ed9addf16 0831b96ee7743abb651caa87bc4c555d164354ba01d4199fc6c65d90932bae7d 472d5b9fb6ddc9396ed542065a329e54e795099e9bed510882505fdf6408b307 19702fef7a38d8b6e81a89599e915ef26d3462bec89a504a7457800fc8744f05 c7ac72ee179e8466361d4098709c3b4728421dfe8c9577c05b30a28bb7e2245a 147787c8bf128f378474de3ac37252f12d3475fe7af795a6302505548174b731 afe448473cd3dbca0a70d6be95091779a1473c4633413453c9326edb582d4334 881691775d31f9618332a24be1af449905a65308ad7b23a3f03f5d3d8216b1d9 8cff59b722d4d2eddf672a7bf892aa167c9ca3eaffb563b1652263d9602c244f 49fdec23277e9b194f645bb378cd8a71a91b19252dffd4661cffc70a8ea27538 3484d7db8fdfee27b8f897227736b94854698f102877527acff35066e836f31d b2174a889f0e55aff455254c65aaa100b078f1999cb13c508cda0df964ea0a7f 472d5b9fb6ddc9396ed542065a329e54e795099e9bed510882505fdf6408b307 19702fef7a38d8b6e81a89599e915ef26d3462bec89a504a7457800fc8744f05 f2c73d61f9a9fa4915937c9dae8b44460635d381f31b87baea2be6c5f4984dd6 054e14d3f4766c766e702e31b86d585d3415af61a67a7ea30beae82c67d4d14a ce0f647f6ea1ae37a5ddd2f35d66c96abdfd7875b80504125eeee49ac321c383 472d5b9fb6ddc9396ed542065a329e54e795099e9bed510882505fdf6408b307 bfd21ac7b706d4aa8179c46566efa75ec98eee04a48146b092ae0685c61008a6 6fc96fdfc0fb90f76e4183910e73933a64c5180eadfbff5232be083d1f60da24 b15e69d82e93b11386e181b7e1b57de7b8a75918efb5d65f3c989cc5740adee5 08bbe70dc16f88b7b5734758f1c3ac01f41fc270e1a325f699007ba1060ab199
plugins/agent_messaging_plugin/tests/autonomic_completion_smoke.py a331526ad98444d74d04f4b8379f7a4df40f8ed8771c36f03bc18bdb40126ae3 472d5b9fb6ddc9396ed542065a329e54e795099e9bed510882505fdf6408b307 ea1e21f984180594ed9a9398b91b2dcdd28d6848dc5124181ebf631dd3f55635 ea1e21f984180594ed9a9398b91b2dcdd28d6848dc5124181ebf631dd3f55635 472d5b9fb6ddc9396ed542065a329e54e795099e9bed510882505fdf6408b307 ea1e21f984180594ed9a9398b91b2dcdd28d6848dc5124181ebf631dd3f55635 91f59dc8a3dbbdff4327bde0646da5825e8335866027154f88df7c04628868f9 a038223a9308e916f7ae83f36fa3cc56513aa667e7f021d556a387fda01e1a66 df3bcde75ccf13cafa51f6349e325799cdeb54ac3e3fda10399ccbdbc04c44b7
plugins/agent_messaging_plugin/tests/bridge_lifecycle_sweep_smoke.py 3d6b87b261653f22ba56338a7baff3b049a4d10413fae6d3f4366e342e08fb9c 472d5b9fb6ddc9396ed542065a329e54e795099e9bed510882505fdf6408b307 db012cbcfb9627032e5e39d1f7f59c5c5ecd4c923e7773a99c8e10b31bf264d9 e87533ba20b4dea11caa8fafd07582e8d6d5f4d8b38c0e50ebecb3bec99cfb2e 153ace82fa332b8b021eba239636161d3ca3c1721573a618a65c763bf8343318 c1a9913ae2774eae7fcb277d2eb12b6c5ea0124d3cd4ea352bb43d1a62b78872
plugins/agent_messaging_plugin/tests/bridge_reconnect_evidence_smoke.py 27141ecd4fa9d465f7e81a5c9fbd6e6827c7d11c599b96e7d8317ad2697da53f
plugins/agent_messaging_plugin/tests/caller_provenance_smoke.py 4d642124f5ff8745916a9202f4b1c8393e6e13893c85cd3de86931766dcaa520 65503d6c6a083330934723136546ed31e51ee62bf33a30f8273e6b653727814d 689989fed5a6f097ac628de0aabfdc389a1d78f3e507989a86470d2d8355d8e5 030810aa4bbd6fc57ffbd77ebb0418cc81de2893d570a11669bd446415318bea 4d642124f5ff8745916a9202f4b1c8393e6e13893c85cd3de86931766dcaa520 846d18081eff98705aa35fd1edff2f163235f51fdad157f12ef11551283209a6 689989fed5a6f097ac628de0aabfdc389a1d78f3e507989a86470d2d8355d8e5 030810aa4bbd6fc57ffbd77ebb0418cc81de2893d570a11669bd446415318bea
plugins/agent_messaging_plugin/tests/cli_job_await_smoke.py 886ff24d8756c5687767e291e6cd37dc908d969dc4d3828b2c571a0e4e386300 14b103621c6437408f5308d422074892599abc6057774b7d0e6902329aeb236f
plugins/agent_messaging_plugin/tests/fleet_identity_qualification_smoke.py 0c3a9c68beaa548f1a5de1991461348d9f469523689395a6c01e4bb94d54166c 5ae1b58a093189b169fcccdbf736257ffcbc5df2505d89a7f9a4deb4308ed6ce bdf6ba5fe5a005a5751bc7c22b6c70e75194be2a129e1412d7d963133810c983
plugins/agent_messaging_plugin/tests/job_completion_delivery_smoke.py ffbd633ded0b6012a3d6c3255d2819c0cb3578bb14587fb922bbdef135e25edd 1ea69cdc9e82b876bedc24e6de2a4aed0dab83601eddcc8ca99fca2932616492 cc65c1cd19e2a1d380aadf0b2691a31f3ed6ed04e52ab5d8a78d3e209ce2ad12 adcae33eed337b6332abe2d113e9dc5a3e23f0e6de4283d1cc4fa5e9639ea157
plugins/agent_messaging_plugin/tests/operator_seat_wake_smoke.py 5dd98b5447c6184e9c7ddbf8439ac2d83605be655774c3e7dda7d1aef981bef7 97492ba14e707829c1dd2c9c7b95039bac733e1366dcdeff1a6b7930f2af3f0a
plugins/agent_messaging_plugin/tests/peer_send_no_wake_result_smoke.py 93dd5cb38c28ee62efb170844c9f7fc6b2803fa53e77762a8cc60c64658df99a
plugins/agent_messaging_plugin/tests/role_two_way_reply_smoke.py 490b4c7619a41820d6ed05c7302f7785c438443bea5b8a4601590f5099cde892
plugins/agent_messaging_plugin/tests/session_claude_mapping_ingest_smoke.py 9714aaa7ffd72961d225729ba3df7b8b96bff851218216ce4cc6612b25535ff8
plugins/agent_messaging_plugin/tests/session_hosts_smoke.py b58ff71ea1d5ca77f4f496e42b9a011b614f1d37dd3a17b8ce7bab6845d60214 fa7a9ed399bb33bb6fbc6f7e631916b35c51b1fca38131ab7bd18565ad9b252c
plugins/agent_messaging_plugin/tests/spawned_worker_registration_smoke.py b625c247b425250c20c7da84476816ce77483421a30a955204db19fa41f35da9 219cf8c63e2cd66626e9259158a776d33ed20f7322683d8e7a41a0913dedee79
plugins/agent_messaging_plugin/tests/streamable_no_auth_mount_smoke.py d81cd743d6d6ce5eab2f5e220960e0e1f9a4616b6c37dd89b65a2c27d4d2b065 16875b368e29d993c121e985f333af57c0f42d79781287b9c8f8ff8d4bffcb84
plugins/agent_messaging_plugin/tests/system_slots_smoke.py 5a1b5e59f09d952a7eaf4d0de1e55ccbd35795ff03df061a37b1debd51d8600b 4ea45d4738201bbaf9881efe8838377fa2cd9e37e1e275a7d3dc9f2d1c035aab
plugins/chatgpt_export_session_source_plugin/tests/resolve_blob_path_smoke.py 0142adf26ec6106a3188abda2a5ce0c69c74da4c1eeb52d89eb5cd4fa34379e5
plugins/claude_code_filesystem_session_source_plugin/tests/claude_code_filesystem_session_source_smoke.py 4cefcc271bfad15e4b4e50b2095489add44502f5fd3ce34687c6f1b848d48b0c 4cefcc271bfad15e4b4e50b2095489add44502f5fd3ce34687c6f1b848d48b0c 4cefcc271bfad15e4b4e50b2095489add44502f5fd3ce34687c6f1b848d48b0c 145dbebb3c9e52403e8b1d5c21a9f32432f9c93a11a571d0c8bb7955006c7b7c
plugins/code_vetting_plugin/src/code_vetting_plugin/verify/lenses.py 3af756e8b2073fb3d3684c27d220f97410b54229d29f6ae30f5e1bd36000450a
plugins/code_vetting_plugin/src/code_vetting_plugin/verify/tiers.py 415d4fc4652fe54ccd7b19154921b41896616d809afee8d94c3419787dfbe19e
plugins/code_vetting_plugin/tests/inference_wiring_smoke.py bd5d150048c6b002bdb72c87c08c45d23ac02e0b3eadaac3f9d327b7f63d8171 a8a60866c4d6912ccad9837a9fdebf20002e368303ff926a33a129f933766de5
plugins/code_vetting_plugin/tests/python_type_check_smoke.py b80b14c54a8728788b93d6cde77625381fcfdc9cad99652e6aeaaf378d45b468 25e4c23f216b68fa1b9a689d8b918a67c0d0e0c745bff5a4556084955b258037 1aee6af76113ac56fc10229194c6a87ce15fb7f0afd72fb5fbdb774eab565300
plugins/codex_filesystem_session_source_plugin/tests/codex_filesystem_session_source_smoke.py 4cefcc271bfad15e4b4e50b2095489add44502f5fd3ce34687c6f1b848d48b0c 4cefcc271bfad15e4b4e50b2095489add44502f5fd3ce34687c6f1b848d48b0c 4cefcc271bfad15e4b4e50b2095489add44502f5fd3ce34687c6f1b848d48b0c 145dbebb3c9e52403e8b1d5c21a9f32432f9c93a11a571d0c8bb7955006c7b7c
plugins/default_inference_plugin/tests/boot_optional_dependency_smoke.py 529acf4ebba74352f60e3b00b8f3f1dde0dbcd52ee8cab8e00dfd119622c3968 df8f9b32b715e9a56203ff72f6f4eff6a65c31f40a4db46cadd3901a4dd752e5 33b6201c2ac5d388a4ddfb44bd6d20cb5a685f742b70ebb8a1d8cf540ee5ec77 df8f9b32b715e9a56203ff72f6f4eff6a65c31f40a4db46cadd3901a4dd752e5 937fff7fc3c8ec160e27f3cdb3a601ef5337952b6acce717c4e2797dba428a50 5870acc9a76d3ae2722a31245442daf75fc1868572cb7b056502407c80c0df18
plugins/default_inference_plugin/tests/timeout_wiring_smoke.py ea4b16ff07e9147e59e6075edcd1437833de540658cd1d59e41baa8ea59095fb ba0964db99b6cfadca6f2c0ac3e0729d1376f07eb75f94d824997139a63d7372 c2df0d7d68dc8cf917ee8606486285694b9e28d93711414f4484c1e07f3de334 bf7528176a8365e27d4cda61299ae7455cf41cd4a8503289c65cdfdf5384c745
plugins/default_knowledge_plugin/tests/audit_retrieval_corpus_cron_smoke.py 65c49aec115fa9ba2a1a50a07c3572b5c589dc7b8289be2e5d4725c761e7a3c4
plugins/default_knowledge_plugin/tests/file_ops_posture_smoke.py c932905474e1f06c560f4fe435452f30ad7d262898bb8ea51d8de60eabdf0622 cbb8d11c281316e52019d05a7bad0079f6394f0af68a8306290e817291b95c08
plugins/default_knowledge_plugin/tests/ingest_kb_smoke.py ccb32f6a4cede53826d72c3e4ca49d2ce06e3c03a66781d372782b35ead4a532 687df96634a62c34059715ef8142d3d37c3d534639a874b0196654974a595333
plugins/default_knowledge_plugin/tests/resumable_kb_install_smoke.py a3f99b61b28872d62db358a98b3cc66df22be26caac430fda49ab4c4314f8153
plugins/default_scheduling_plugin/src/default_scheduling_plugin/plugin.py 5d74cf312401e2a76b7a268a4cd8a9fe30e6274ea24375a0c8334cfd18e85472
plugins/default_scheduling_plugin/tests/action_executor_deferred_fire_smoke.py 50785547487c5ed2c99d85f42f7521aa11a7c7ed6bee6fb09e6d73d46ed45494 6fafe37fd6080b850d0b1cfbda234d59a04566f6d88e5cf3b6e4bda5a99d864c
plugins/default_scheduling_plugin/tests/action_factory_injection_smoke.py 5fc8a245ac0734f7c641df5ea4e06a2030dd93d04fe0ddce3fd98bf6ac7e5cb8
plugins/default_scheduling_plugin/tests/clear_scheduled_action_orphan_smoke.py ebf40bbc5e5197e97a77c0d2875d466fddb0b9afe5a8b3a7db444ba101afed2e
plugins/default_scheduling_plugin/tests/scheduler_action_definition_configuration_smoke.py f6cfa63a42268bd42059423386089bc366febc1fb118eaf857eb2525557dbd0f
plugins/default_thinking_plugin/src/default_thinking_plugin/pipeline_spec.py 1899bcdbf407dcdafcd34866822b07d10fa2860d8cf1dcbb8af287c39b1f4f8e 6046c4114eed89901b1789adcc1844e751d6c4f5df3420a1098ae2d1d19121fe
plugins/default_thinking_plugin/src/default_thinking_plugin/plugin.py 5d3edbac0066ba7f2d9a007e596bb3f2df09604ab749c111affe6aed1a67e24c
plugins/default_thinking_plugin/tests/system_prompt_shipped_default_smoke.py 3eff8573e2520355d3cab9296100d00e7a3674d2e2374e5bed5573e4a6876315
plugins/external_postgres_plugin/tests/smoke_async_jobs.py ed970db26f5dbb789bc7b437d47ab10223d069a64ba51e735b1e65328733b20a ea14c1e688cf1f87469c025ed2741b709abcd8654eee5dffaf9ea28947058176 0a101e18f41bce68a86bbd66f3bfcff4dfb8f3bbf0adc2ef4a9f6f130b6ebebd ea14c1e688cf1f87469c025ed2741b709abcd8654eee5dffaf9ea28947058176 5b4902b7642d8863865e478fc1c085fbdca7590ad666cdb9306d45f38a00ee29 ea14c1e688cf1f87469c025ed2741b709abcd8654eee5dffaf9ea28947058176 ed970db26f5dbb789bc7b437d47ab10223d069a64ba51e735b1e65328733b20a ea14c1e688cf1f87469c025ed2741b709abcd8654eee5dffaf9ea28947058176 4ce985bab93e11d92f58fd9edb245980464af647f3d5de33dfc756c4c0db17e7 27487ee8cfa117506cbec0bf837d2ae04149828887387d8c79c851a3be13f776 5b4902b7642d8863865e478fc1c085fbdca7590ad666cdb9306d45f38a00ee29 ea14c1e688cf1f87469c025ed2741b709abcd8654eee5dffaf9ea28947058176 ed970db26f5dbb789bc7b437d47ab10223d069a64ba51e735b1e65328733b20a ea14c1e688cf1f87469c025ed2741b709abcd8654eee5dffaf9ea28947058176 ed970db26f5dbb789bc7b437d47ab10223d069a64ba51e735b1e65328733b20a ea14c1e688cf1f87469c025ed2741b709abcd8654eee5dffaf9ea28947058176 4ce985bab93e11d92f58fd9edb245980464af647f3d5de33dfc756c4c0db17e7 27487ee8cfa117506cbec0bf837d2ae04149828887387d8c79c851a3be13f776
plugins/external_postgres_plugin/tests/smoke_build1_families.py 543a352b41f8472243176bfe815a48d11c12d65b0d2d53cc43d699fb57afa31a
plugins/g_suite_plugin/knowledge_base/processes/tasks_tasklists_delete.json 8867e427800ea7efa1449ffac13470cbfce7a7d55d9be0f741083a76276711a0
plugins/g_suite_plugin/knowledge_base/processes/tasks_tasks_delete.json 1d97b1fe0dcff37888b12749fca2de896c938cbb4198290f71c77f3430d33332
plugins/g_suite_plugin/src/g_suite_plugin/discovery/tasks.json a74c0baa371932a750e496ffb6df16a357fe82beeee56810371392f77226aff2 02ef61dc2c413bfc79b3abff20b99e8d79d10c7bd6cfe1e202c6791efeb3a047 bf76b4facb45af32abd69ccee5d3feb3d5efac7ec28c013dcb7fcb1e26c0be74 dd95ce2bf1d6505dca9b4616e09599e88be1fe377d10e3e38fd16339f06fae78 9e23bb5d62128185900c610cc5460baa6cf8bf5216dbee6334a716cd32f16cba
plugins/g_suite_plugin/src/g_suite_plugin/tasks_api_processes.py 833c2b5f5d0b7ff561ff7d71544a05768ebec8182f46a728a5355d8554e4cbcf 7a342df97eeca26bbf96de87dd9d2de60b2e778461b4ac8c1163a9ce1f8c28d7
plugins/g_suite_plugin/tests/smoke_accounts.py 6779b74a942b34b22ef745a13bd6be4dddde5bde86af42804271c117391b1eea b3e6bb54bca4957cac9b2ba12fba8562a38fdced220f45215b0360a2f5a8fffb 40abbdb962d97fdfa46591a6b9ff87ba8c30da19799fd0fdd45bb1035e37c55b 53b041de364d701709f90bd3100c6836b180a305f464685d54e8c9027eb1003d 40abbdb962d97fdfa46591a6b9ff87ba8c30da19799fd0fdd45bb1035e37c55b
plugins/g_suite_plugin/tests/smoke_async_jobs.py 08040c98a284e92156e6f1a52cd59944d1bb34dfaa36f056595087bc684c6851 c86aab384c315d599bd66da23c00d436b569d355bb3aca0625aecc3a59620ce5 d02c0a0a73f71c9f9672002382bda2f54ce89f7d3f981609ddfb1d9c49d59100 c86aab384c315d599bd66da23c00d436b569d355bb3aca0625aecc3a59620ce5 732d0794e99ab460292a9afb9cbb1b85a7bf08e9076aabc4f3bfac3274717a44 899d19679bd0ba126a420b3953b13200dd4e09ec64f591a0949e56996450614b
plugins/github_midwife_plugin/knowledge_base/04_first_days_runbook.md 06cecde95c946f80254d157bbf8507669e644a91bc1306416b82fab77804e5b3 82d37bf6435a8912c2f3b351be72dbeafcff4615f30dd69fdfa7098c97446f14 f1f863f8bc0fec5c76f4c7cb6050b1f0f2332ac3819c7d564a4f3eabca1bbdef 4464985284bb77e299b568cb2fa37e9224049597e3cb5f9dde77ca60c93b234e db4e88b9d882f5654fbe7c760fe6a89b97393b16ec53ab0f3603bba73ec0b7a2 f601efa05ef78f07daa84bbc66d6a1b5f9159dc3636c1893b62392d4c824133f bc6ff58095b51551042385de07beb24ab2d92366e7c0a92272702c44aaa99e49
plugins/github_midwife_plugin/knowledge_base/04_first_days_runbook.retrieval_test.yaml 7d3d4dde95487869657797fe11148122dd47631e3095832c9603e3f30e5f3874
plugins/github_midwife_plugin/knowledge_base/05_seed_update_runbook.md 573d3d30441f363a09d00c5c6785c5b508c500861cf4700aa44a42a45184501e 7c1885d2158d14777486eb0a604faab7d8766406f9a9ea441161f47067eae8e5 638ab8fa9db2df14196e98cd71b6e77b285a52671098a2352bf726e9ab1926aa
plugins/github_midwife_plugin/knowledge_base/09_homebrew_install_troubleshooting_runbook.md 000a7de8e4219eb97a1ef5b45a337837fcf7379338df70fd4face3635a610f5a
plugins/github_midwife_plugin/knowledge_base/hydration_templates/AGENTS.md.template 5828d5c878461979beb640f262c2b743cf58c6b3faace1a97db87f25f84899b2
plugins/github_midwife_plugin/knowledge_base/hydration_templates/CLAUDE.md.template 5828d5c878461979beb640f262c2b743cf58c6b3faace1a97db87f25f84899b2
plugins/github_midwife_plugin/knowledge_base/hydration_templates/TEMPLATE_VARS.md 34cb02be45cb49121fdd13092e190393f224692874069639f6b819c019c6bb80 ad5f1fca5f5d586e12d6028b126a061abcf465756b25bd3d1dc9cd84e9dbc1a0 ce74dcf24dfb0c4323ebb20ffe75d6f7a38d0670096d4227db3f5e6b70083613
plugins/github_midwife_plugin/src/github_midwife_plugin/existing_install_operations.py c30e9bae418cf6fe8733416bfeb87e1de391c83f04745c9de0330736cf691f24 bdc39f9e0765651eb881e8ebfaed3d2cec2e736d6d662a2dae6cd6b64f07e98b bc7903e17b0b5277a44f0e058699cb2dbb9437c3b5828b4f202b11a781585f6b
plugins/github_midwife_plugin/tests/autostart_render_smoke.py 15bed9cfe515f20999fb783931f0b3fff2c91b37796cdb114fa03236b7ae91e5
plugins/github_midwife_plugin/tests/controller_setting_retirement_smoke.py 652a9fc50433536b0b161827dd379924af54134f1f26276d8fce81c595f62c91 c50b7824c7427362b42b6e316680384b9b341fd210bbe52824e05829356a8fc3
plugins/github_midwife_plugin/tests/executable_hydration_smoke.py 027e2f0cbbfb9c8af6adbce2f14b560b9193f70f8f4f7aab3e5127522dac23f5
plugins/github_midwife_plugin/tests/existing_install_operations_smoke.py ba8c2fec5d43d1066827cfa140bceacc041b10e5efa8397d653d7d8a407a3cd4 ba5dfb73092bd94801cf064d51d5faa1316de010d051f87c2a21a6f5ed3cae28
plugins/github_midwife_plugin/tests/hydration_templates_render_smoke.py 5e22f00f163588e94f8c2693d5c46452f63881125ccd2d68cbb8abf0eb0e4bfe a9c29e2f962f6796afb838bccb7a4010b1649d571e37261537f5b8c81552f0bc ebf496ba3bd121edbe5dad9d28a7c20bae4853863432795fbae45513fdd88c98
plugins/jira_plugin/tests/smoke_config.py 371a5c7c348018b8692fb05401983a6b6df656aba9a76d28aaedc6e8c65de4ff 371a5c7c348018b8692fb05401983a6b6df656aba9a76d28aaedc6e8c65de4ff
plugins/macos_self_deployment_plugin/src/macos_self_deployment_plugin/blue_green_router/install_router.py 8b4705fb160cf545743bad61a44c48a8dc989eecf0f00bfd043a2a28dce83cca f894f98aa086503c4ad1b1fb4808b52a73d5787856da30784239419ac56cb0ff
plugins/macos_self_deployment_plugin/src/macos_self_deployment_plugin/plugin.py af619eb89fea8bf88a328207b7142286833e5bd07a60775ce41377185c4f5a85
plugins/macos_self_deployment_plugin/src/macos_self_deployment_plugin/swap_executor.py 0e29cc3389ae6a1ecdf28335ec78fadcbeac31b4b5aaa00f3219e09d00451414
plugins/macos_self_deployment_plugin/tests/blg02_manifest_capture_smoke.py 9311cf22d7571215529bde2296794924be83aedcaec1cff1f27a362cd3437913 8d2ce5152854d3bb5ba4f9270ebb4b7a18832491febd54ed48bce92d7db9a163 20c6477a81fc4367ec39dd34f764d9317105c5f2c9d07d6fd435800f4441457e 1c2966639ae70438045e388adef79d5599e9c29c86cfad661cb4755ff54a8a8d
plugins/macos_self_deployment_plugin/tests/candidate_runtime_scrub_liveness_smoke.py d6ed935a899a77a91ea346c68e0353d72cb2ef85c04322aa3e1c891961806c50
plugins/macos_self_deployment_plugin/tests/complete_swap_crash_consistency_smoke.py 726abe32c06b6f9a65785af159223f0ae874652d75efcbd6637592a28e815782 4475f620952a4366536752529d4a87590d2ab7b840ef71432cf5c88a1330c526
plugins/macos_self_deployment_plugin/tests/cutover_failure_smoke.py cd31caa3c3d3d556d179106816edaf47ca220d30d95eb70c5b28c02e0035e7e4
plugins/macos_self_deployment_plugin/tests/cwd_out_of_tree_smoke.py e365d6222a45ba6e96a133ac20c68a64995dfe569a025e1dde0d6231990b4438
plugins/macos_self_deployment_plugin/tests/isolated_candidate_boot_offline_gate_smoke.py 964967ff2088876f431fb0ba00649462052a5386ab3e73fadedc032d6aea91c6 1752c471571eeb6fce6ce56c3c6b106593b4b7ef69e517af04dc78a984ffbcf2
plugins/macos_self_deployment_plugin/tests/lif02_half_booted_color_smoke.py 8d6f25b7c42505c93addaa09b55ffdfe55e130d3c73b3fc27a7c203460c160e2 c94c8d84e764453ec4631f5a4773214100576219aac35e456c0612ed6d29ea5e c6f62778ab2ef354548ee045fe94b02cbc4b4b1e85fff5aa08c446ec1919a1e5
plugins/macos_self_deployment_plugin/tests/no_sigkill_channel_smoke.py cd31caa3c3d3d556d179106816edaf47ca220d30d95eb70c5b28c02e0035e7e4
plugins/macos_self_deployment_plugin/tests/poller_gate_smoke.py cd31caa3c3d3d556d179106816edaf47ca220d30d95eb70c5b28c02e0035e7e4
plugins/macos_self_deployment_plugin/tests/prepare_readiness_restore_race_smoke.py 042c96771d341d8b853b3fb12e8c1c5fa2fb6062b9d7d560658c10212ffa6765
plugins/macos_self_deployment_plugin/tests/reconciliation_cutover_smoke.py aa9fefca6d8028a1604631ca8e54a34a8d7b39d4f6cc79f53c14a43f0707c71d 49686ff54e308597bb419c8d58bbbabf885778aabffbe21e5fc5ffb60184be7f 6edb291ae46a29ecb5a04b142419c520f86e75905055ffbbb64645aecd49a2bc
plugins/macos_self_deployment_plugin/tests/release_code_collection_batching_equivalence_smoke.py 92aecbc1a17b7de29dea7aeb5904b3ef7b0ea358a9195a4d88ce8bc99bb21673
plugins/macos_self_deployment_plugin/tests/rollback_release_smoke.py 431ce96148d9d53508482451b7cecd7d8217476de65a171dde8f35b15895be08
plugins/macos_self_deployment_plugin/tests/slice2_register_within_budget_smoke.py c6f62778ab2ef354548ee045fe94b02cbc4b4b1e85fff5aa08c446ec1919a1e5 ae1c5b6d84c47e9858def3d809edee5021ed1132de6c72915a2b007bd74b7aa5 ae1c5b6d84c47e9858def3d809edee5021ed1132de6c72915a2b007bd74b7aa5 ae1c5b6d84c47e9858def3d809edee5021ed1132de6c72915a2b007bd74b7aa5
plugins/macos_self_deployment_plugin/tests/slice4_drain_sentinel_smoke.py 5ebd307c7b325961a4c01ed99cefb5a3246be596f067931cadfa7e3d791fd9ac
plugins/macos_self_deployment_plugin/tests/stray_cold_start_guard_smoke.py 4737b95b1af21f5fd33a7a4a07de8b250969392dde87df59defadca16190ab2f 4a76662c064a76bf81b26d168518ef82a40420012ec5750a96950c8c18794052 76c66f35c8c0f1b990d6e31579403ccfd32bdb0fc70a288c43411c39e17e6e81 ea4d6424e75d167eb2c531ac8b90e95484f405dfb06848f28a31d2702bd6d083
plugins/macos_self_deployment_plugin/tests/swap_round_trip_smoke.py 0901b271c4456e430d1b7e7d872c7effd9e6695b66dd5e2d8f7213e0fe2dd290 c6f62778ab2ef354548ee045fe94b02cbc4b4b1e85fff5aa08c446ec1919a1e5
plugins/macos_self_deployment_plugin/tests/task19_action_factory_lazy_orchestrator_smoke.py ece30d72767654769530fb4b3ee59336bc80bf639ba5a24e7f5b0b13148ebc24 c6f62778ab2ef354548ee045fe94b02cbc4b4b1e85fff5aa08c446ec1919a1e5
plugins/macos_vault_plugin/knowledge_base/articles/secret_transfer_protocol_design.md 596131703607f5c964e4f9f60df26b208496986a243cb5d8f265a0b0b23cb273 5b4b8e4bfccacfd391b3e244a2cea61c7ab7080d523c88770eaa5e17abae23d4
plugins/macos_vault_plugin/tests/keychain_user_interaction_gate_smoke.py fa6770da6f48217389d1b90a6b290d8d9d9a3d797073554d83d3850440fdda48 fb3fb2b0436b14b19491e9810dea020adab3893842a12000a6294cf37bd752d6
plugins/macos_vault_plugin/tests/w_vault_local_keychain_smoke.py e92477193dd9f7c546a78525eb069ec0859a8dbfdbc543b8bb342c86ac32abf2 a3d2cb0674cceab807185973d2fdd0a0231641bd7d83d372ab202df165978651 51b35a726ac6eaf6eb58b7f632e182fed8b6632de9cdf27da2551aa952500033 2a88a6d74c4924f8b2af0a2e56cddd9ad04125d49e4e9ecdab8057e4341bb148 1800eb2491610a9236cc1369812c18274136a37f4f788958c37c896160350039 4d4b6f5181cd41f954cb2ecceb2ee0de46d25fa7d74f9bce5d6e5db2db607db1 67d04f608991cecf1ebbc67d911a5be52707264d894c0a16ea8b1be051c8a46b f5bb0e129a7124f3be7a435be883703d6b53b8aedf153e84c6808eac4dc5a140 6723163eec1764f28616303f6b0e92b6a3e54b264b74d98506b1b8314d25cf72 2857b139b5dcd0be11906608c10cf2c9c022febbb38da129e32c64c179352c92
plugins/marketo_plugin/knowledge_base/vendor_api_coverage.md 1f886eb4bb7deee65abd435ec5fcbbfedae7d9d47a1207b85b5faa2b6fe332eb 8c41ac9cbfe0e54f4fb96a095d1ea04f5547c8b21289edf3ef081b04f74eb8b4
plugins/marketo_plugin/src/marketo_plugin/constants.py fba7be2ec607082cec4c68837af4dfc93871e3e0506bc86fb31178c1438529a4 90ed5f182b4c5451088bef33ac7e8184170a4b85f79664fb204aca92a1abd7ab
plugins/marketo_plugin/src/marketo_plugin/marketing_actions.py 56d6e6be4bd7c5326846c4b24f8a6838b266be30624dd75bf6b9794900b35063
plugins/openai_embeddings_plugin/tests/startup_qualification_smoke.py ad0ee0e21e4eea57c332a85d010be431f294cd74a2ffb355e0f7579fff0d9913 ad0ee0e21e4eea57c332a85d010be431f294cd74a2ffb355e0f7579fff0d9913
plugins/platform_dev_surface_plugin/tests/repo_search_fallback_smoke.py 616992bb222d598ef57f1fed83059a87b322b726de671fb1265e105b651f5482 cc4dce015a3db9812f49dab0d6e53f560e290ab58533f8ad054119c4d37d77cf
plugins/platform_health_plugin/tests/guard_test_support.py 5b6c92b731c0f5fc2c10da90451fd96a76e37d1999ef9d914cad359e8c022a4f
plugins/platform_health_plugin/tests/model_capability_guard_smoke.py 619b9f819828158a28961288b9172355573ad8fdcec805e158ac542908d0704b 4d31d867bf42a0d46a8fd2a36ed55e70b6a5b7d1dc0cab70d1bb2b124ab3acd5 0185739f5cac18c7a7451f41b2732830fcaa78b9eb3b48e9b1fd1ceb7e97a9b9 a5c39ad4599e5818c6abfed49a9ed5964dcf68e58f4b5c419d677e9cdbc8edad
plugins/postgres_state_management_plugin/src/postgres_state_management_plugin/postgres_backend/provider.py 0c99759da1b5d2c4a623e9ef0149c7652783720f604c5ef2d2f547526091d321 0c99759da1b5d2c4a623e9ef0149c7652783720f604c5ef2d2f547526091d321
plugins/session_shipper_bootstrap_plugin/tests/session_shipper_uninstall_symmetry_smoke.py b754cf77f3d56e07193da05d3ba0a7cf55b823e1419bfd382e6d0e1bb78d132e 18c60316842500252f4470accb9e477b73788303ef0fadc2e65ba6a3d3226f0f 5313c075c1a652f1b04f1a04f8c21a409e69027f42eae37779a4eebf96fc3f4d 8dca161002af9b5a3c10ca9258597d53d5716063d020880bb575d607e82c0dfb
plugins/snowflake_plugin/tests/smoke_async_jobs.py ed970db26f5dbb789bc7b437d47ab10223d069a64ba51e735b1e65328733b20a ea14c1e688cf1f87469c025ed2741b709abcd8654eee5dffaf9ea28947058176 0a101e18f41bce68a86bbd66f3bfcff4dfb8f3bbf0adc2ef4a9f6f130b6ebebd ea14c1e688cf1f87469c025ed2741b709abcd8654eee5dffaf9ea28947058176 5b4902b7642d8863865e478fc1c085fbdca7590ad666cdb9306d45f38a00ee29 ea14c1e688cf1f87469c025ed2741b709abcd8654eee5dffaf9ea28947058176 ed970db26f5dbb789bc7b437d47ab10223d069a64ba51e735b1e65328733b20a ea14c1e688cf1f87469c025ed2741b709abcd8654eee5dffaf9ea28947058176 4ce985bab93e11d92f58fd9edb245980464af647f3d5de33dfc756c4c0db17e7 27487ee8cfa117506cbec0bf837d2ae04149828887387d8c79c851a3be13f776 5b4902b7642d8863865e478fc1c085fbdca7590ad666cdb9306d45f38a00ee29 ea14c1e688cf1f87469c025ed2741b709abcd8654eee5dffaf9ea28947058176 ed970db26f5dbb789bc7b437d47ab10223d069a64ba51e735b1e65328733b20a ea14c1e688cf1f87469c025ed2741b709abcd8654eee5dffaf9ea28947058176 0a101e18f41bce68a86bbd66f3bfcff4dfb8f3bbf0adc2ef4a9f6f130b6ebebd ea14c1e688cf1f87469c025ed2741b709abcd8654eee5dffaf9ea28947058176 4ce985bab93e11d92f58fd9edb245980464af647f3d5de33dfc756c4c0db17e7 27487ee8cfa117506cbec0bf837d2ae04149828887387d8c79c851a3be13f776
quality_gates/gate_smokes.txt bae3913c5b55d8a4bf67a1afa2c1a4b7031ed40dff61db1ef2e1a03aa43a64b9 42ea41a39fe5f5fe2f4f355f5fd50294c004ac08f6fd2d54046d79fb33a415c1 124de92324584bb82962d06ce267a7432f2f4d7565112ccd10ae907fa76c1226
quality_gates/god_class_check.py b595856d522f66a98a84f3f6b831f130cf51675bbdac8cfdfce02c9f36fc9699
quality_gates/macos_bizops_identity_gate.py e415ab2b6cdcbfe74255096549185531bf6aecd8a9175fa962e7f904e01335e9 5d7d7e8866b82133177b0bdf67bb427475ee6ac0c25168cae4dd8b877c0cc7da 3be50f323ffc0fb80aed162a433b8f21c94de7d0b4be77bb52abc139cf2124c6
quality_gates/service_interface_ast_bindings.py dd2b6d6929cad32353bca4c93b4d4aa793a1cfbd3c48bc01fe936d5a51e26490 1abd097b30d245831ef7d0b7d5bc71eaf38fc33e7af124d769ddfd535d2dd0f6
quality_gates/service_interface_ast_check.py 2d98659537bd939ebc5300ad317e1aee9e226e39a21276a6c501d851b8a4cd39
quality_gates/tests/fast_return_census_smoke.py a8aab02a6b8fa2b9f00f54cbf133cc4a1bd27e82222a44748c7bd97de2d722cd
quality_gates/tests/hydration_block_drift_smoke.py 7adcba015b790ead6312fa2f4e402a7f767e35627be4b90d2f3ddb050725fc2f b5880e15ab258e0ab550ce9d056e83e14f526ea69c9506f2ae159304a9558d11 58af23f2e3f1905cbba117e8517c56719fba27c13a8efd94c4ae68e6ecc268c3
quality_gates/wint2_vault_key_declaration_check.py bd0886418ba43adaea038a7427e4d1fb518e5866a0fd8c3008ca44dbfc0fcc6a
solet_cli/homebrew/ci/tart_stage_resume_contract.py 9dc2073a42c6cec9ebb70bd4f4548ef7c1944913cbf2c7656d51b79ec2bfa0f8 8c568a12a2311af7ecf2ffa9850dd7095d1b4c6190c4427e936b1b1639d810fe
solet_cli/homebrew/tests/stage_release_smoke.py 47fe4c213beb69f7552a9a19ca4a8d9c20ef5ef576aab5b924d382c0e42956e2 05f47bd2f5b68de1203e1bca8dc1bfd184538250f2f6b258fe4ec45f1a5d262e 173ad552f2774d15c2f6c55d734fe0519633803b659efa1fb369e8388523f3a7 c64f8cf180ec1d46f18fa540ed89292f200f495c795e480d302a535cd784d53f 5073012fd8d17395486a131fb36dc58185537189ed30da82997d801dc03d31ca 47fe4c213beb69f7552a9a19ca4a8d9c20ef5ef576aab5b924d382c0e42956e2 05f47bd2f5b68de1203e1bca8dc1bfd184538250f2f6b258fe4ec45f1a5d262e 173ad552f2774d15c2f6c55d734fe0519633803b659efa1fb369e8388523f3a7 d07df7b2e188f2aca76c916a439d140ad855ea94d35ccae085e13c5260cae4e0
solet_cli/homebrew/tests/tart_stage_resume_guest_smoke.py 1ab29594fa02ce46f1bfc08d009f62ac20d431702cdea64b396c1f91c754ca24 4aea7aefa7a99352edc3c9aee17596127e62cd92cd3fcb5c98007903f85f20da 1ab29594fa02ce46f1bfc08d009f62ac20d431702cdea64b396c1f91c754ca24 4aea7aefa7a99352edc3c9aee17596127e62cd92cd3fcb5c98007903f85f20da
solet_cli/tests/existing_install_inspection_round24_smoke.py 7f285333ca339bad97b6cc49c0dd1836ab897d12c084d12c7f2e75f8e0575740
solet_cli/tests/fixtures/existing_install_inspection/annassign_target_forbidden_call_fixture.py 1c391019a10e85cc01a1c88692e2e0e21780a2d314de120960f63e3c642e21ac
solet_cli/tests/fixtures/existing_install_inspection/assign_target_forbidden_call_fixture.py 57c1e47c6d97bcb4c3702ab957b04a25380d1536484bd389bf97a6dfae7d9072
solet_cli/tests/update_runtime_execution_smoke.py 52df27a11968b4bdd9afca7196169769e9e5a64dd11fe91f2c0f87157329a9d4
solet_setup_contracts/src/solet_setup_contracts/payload_requirements.py 4e9fbc39e8c6de9248a7a22426031dec3925e87d1090a7494d57384452c63d90 422614328f55e0d1865c23a8fe98616e2687f82e3b0001dcc6fd316abf46dab0
"""

_PINS: dict[str, dict[tuple[str, str], str]] = {path: {(site, digest): reason for kept, site, reason, digests in _KEPT_LINES if kept == path for digest in digests.split()} for path, *_ in _KEPT_LINES}
_ALLOWED_HOMONYMS: dict[str, Counter[str]] = {line.split(" ", 1)[0]: Counter(line.split()[1:]) for line in _HOMONYM_TEXT.splitlines()}


# --- The scanner ---


def _lines(data: bytes) -> list[bytes]:
    """The file's lines, each with its LF; a last line without one stays as it is."""
    parts = data.split(b"\n")
    return [part + b"\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def _digest(line: bytes) -> str:
    """The SHA-256 of a line's exact bytes, its LF included: the identity a reviewed line has."""
    return hashlib.sha256(line).hexdigest()


def _tokens(text: bytes) -> dict[str, list[str]]:
    """Each vocabulary's matches in ``text``."""
    found: dict[str, list[str]] = {}
    for name, pattern in _VOCABULARY.items():
        matches = [match.decode("utf-8", "replace") for match in pattern.findall(text)]
        if matches:
            found[name] = matches
    return found


def _render_tokens(tokens: Mapping[str, list[str]]) -> str:
    """The matches for a report line, grouped by vocabulary: ``primary:x;wider:y,z``."""
    return ";".join(f"{name}:{','.join(matches)}" for name, matches in tokens.items())


@dataclass(frozen=True)
class Finding:
    """One match the reviewed lines do not cover, with where it is and why it is not covered."""

    path: str
    line: int  # 0: the path itself matched, or the file could not be read
    reason: str
    tokens: str
    text: str

    def render(self, profiles: Mapping[str, frozenset[str]]) -> str:
        """Path, line, reason, tokens, the profiles that ship the path, and the line itself."""
        where = ",".join(sorted(name for name, paths in profiles.items() if self.path in paths)) or "-"
        return f"{self.path}:{self.line} [{self.reason}] {self.tokens} profiles={where} | {self.text}"


@dataclass
class ScanReport:
    """What one scan saw: files read, matched lines per vocabulary, the reviewed lines it
    counted (kept lines by path, site and digest; homonyms by path and digest) and findings."""

    scanned: int = 0
    matched: Counter[str] = field(default_factory=Counter[str])
    kept: Counter[tuple[str, str, str]] = field(default_factory=Counter[tuple[str, str, str]])
    homonyms: Counter[tuple[str, str]] = field(default_factory=Counter[tuple[str, str]])
    findings: list[Finding] = field(default_factory=list[Finding])
    vanished: list[str] = field(default_factory=list[str])


def _table_name(call: ast.Call, constants: Mapping[str, str]) -> str:
    """The table a TableSchema call declares, through a module constant when it names one."""
    table = next((keyword.value for keyword in call.keywords if keyword.arg == "table_name"), None)
    if isinstance(table, ast.Name):
        return constants.get(table.id, table.id)
    return str(table.value) if isinstance(table, ast.Constant) else "TableSchema"


def _schema_owner(node: ast.AST, constants: Mapping[str, str]) -> str:
    """The site prefix a schema constructor gives its arguments: the table a TableSchema
    declares, or ``SchemaDefinition``; '' for any other node."""
    function = node.func if isinstance(node, ast.Call) else None
    name = function.id if isinstance(function, ast.Name) else function.attr if isinstance(function, ast.Attribute) else ""
    if name == "TableSchema":
        return _table_name(cast(ast.Call, node), constants)
    return name if name == "SchemaDefinition" else ""


def _mark(node: ast.AST, site: str, sites: dict[int, str], constants: Mapping[str, str]) -> None:
    """Give every line of ``node`` its site; an inner site overrides an outer one."""
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        site = node.name
    first = getattr(node, "lineno", None)
    if isinstance(first, int):
        last = getattr(node, "end_lineno", None) or first
        sites.update(dict.fromkeys(range(first, last + 1), site))
    owner = _schema_owner(node, constants)
    for child in ast.iter_child_nodes(node):
        inner = f"{owner}.{child.arg}" if owner and isinstance(child, ast.keyword) and child.arg else site
        _mark(child, inner, sites, constants)


def _module_site(node: ast.stmt) -> str:
    """``module constant NAME`` for a name bound at the top level; ``module`` otherwise."""
    target = node.targets[0] if isinstance(node, ast.Assign) else node.target if isinstance(node, ast.AnnAssign) else None
    return f"module constant {target.id}" if isinstance(target, ast.Name) else "module"


def _sites(source: bytes) -> dict[int, str]:
    """Each line's innermost site; empty when the file is not Python that parses."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return {}
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            constants.update({target.id: node.value.value for target in node.targets if isinstance(target, ast.Name)})
    sites: dict[int, str] = {}
    for node in tree.body:
        _mark(node, _module_site(node), sites, constants)
    return sites


def _classify(report: ScanReport, path: str, site: str, digest: str) -> str:
    """'' for a reviewed line within its recorded count; otherwise why the line is a finding."""
    pins = _PINS.get(path, {})
    if (site, digest) in pins:
        report.kept[(path, site, digest)] += 1
        return "" if report.kept[(path, site, digest)] <= _PIN_MAXIMUM else f"more than {_PIN_MAXIMUM} at its reviewed site {site}"
    allowed = _ALLOWED_HOMONYMS.get(path)
    if allowed is not None and digest in allowed:
        report.homonyms[(path, digest)] += 1
        return "" if report.homonyms[(path, digest)] <= allowed[digest] else f"more than the {allowed[digest]} reviewed"
    elsewhere = sorted(pinned_site for pinned_site, pinned in pins if pinned == digest)
    return f"outside its reviewed site {elsewhere[0]} (here {site or 'no site'})" if elsewhere else "not a reviewed line"


def _read(root: Path, path: str, blobs: Mapping[str, bytes] | None, report: ScanReport) -> bytes | None:
    """The file's bytes: from the recorded blobs when the union came from Git objects, else from
    the tree. A file another process removed after the listing is no longer part of the tree and
    is reported as vanished (an advisory); any other read failure is a finding."""
    try:
        return blobs[path] if blobs is not None else (root / path).read_bytes()
    except FileNotFoundError:
        report.vanished.append(path)
    except OSError as exc:
        report.findings.append(Finding(path, 0, "unreadable", "", str(exc)))
    return None


def _scan_path(root: Path, path: str, report: ScanReport, blobs: Mapping[str, bytes] | None) -> None:
    """Match the path itself, then every line of the file; sites are resolved only for a file
    that holds kept lines, and only once it has a matching line."""
    path_tokens = _tokens(path.encode())
    if path_tokens:
        report.findings.append(Finding(path, 0, "the path matches", _render_tokens(path_tokens), path))
    data = _read(root, path, blobs, report)
    if data is None:
        return
    report.scanned += 1
    sites: dict[int, str] | None = None
    for number, line in enumerate(_lines(data), 1):
        tokens = _tokens(line)
        if not tokens:
            continue
        report.matched.update(tokens.keys())
        if sites is None:
            sites = _sites(data) if path in _PINS else {}
        reason = _classify(report, path, sites.get(number, ""), _digest(line))
        if reason:
            report.findings.append(Finding(path, number, reason, _render_tokens(tokens), line.decode("utf-8", "replace").rstrip("\n")))


def scan(root: Path, paths: Sequence[str], blobs: Mapping[str, bytes] | None = None) -> ScanReport:
    """Scan ``paths`` under ``root`` (or their recorded ``blobs``). The real union and every
    control go through this one function."""
    report = ScanReport()
    for path in paths:
        _scan_path(root, path, report, blobs)
    return report


# --- What the scan reads: the factory's union of every seed profile, or a born clone as installed ---

_FACTORY = "seed_factory_plugin"
_SEED_MARKER = "PROVENANCE.json"


@dataclass(frozen=True)
class SeedUnion:
    """The paths each seed profile ships, where they came from, and why they could not be
    listed here when they could not (an unmeasured union is never an empty clean one)."""

    source: str
    profiles: dict[str, frozenset[str]]
    unmeasured: str = ""
    blobs: dict[str, bytes] | None = None  # the recorded bytes, when the union came from Git objects

    @property
    def paths(self) -> list[str]:
        """Every path any profile ships, once."""
        return sorted(frozenset[str]().union(*self.profiles.values()))


def _factory_union(root: Path) -> SeedUnion:
    """Every capability bundle's shipped paths, from the factory's own bundle resolver and
    manifest predicate over this tree's Git listing (tracked and unignored files)."""
    for source in (root / "plugins" / _FACTORY / "src", root / "plugins" / "github_midwife_plugin" / "src"):
        if str(source) not in sys.path:
            sys.path.insert(0, str(source))
    resolver: Any = importlib.import_module(f"{_FACTORY}.seed_resolver")
    surface: Any = importlib.import_module(f"{_FACTORY}.shipped_surface")
    constants: Any = importlib.import_module(f"{_FACTORY}.constants")
    manifest = surface.load_seed_manifest(resolver._FACTORY_KB_ROOT / constants.SEED_MANIFEST_FILENAME)
    try:
        profiles = {str(name): frozenset(cast("frozenset[str]", surface.shipped_relpaths(root, manifest=manifest, plugins=resolver.bundle_shipped_plugins(name)))) for name in resolver.bundle_names()}
    except surface.ShippedSurfaceError as exc:
        return SeedUnion("factory", {}, f"the factory could not list this tree's files ({exc}); a tree without Git needs its exact inventory")
    return SeedUnion("factory", profiles)


def _git_output(root: Path, *argv: str, data: bytes | None = None) -> bytes:
    """Run Git read-only in ``root`` with a clean environment; a failure raises."""
    completed = subprocess.run(["git", *argv], cwd=root, env=scrubbed_git_environment(), input=data, capture_output=True, check=False)
    if completed.returncode:
        raise RuntimeError(f"git {argv[0]} exited {completed.returncode}: {completed.stderr.decode('utf-8', 'replace').strip()}")
    return completed.stdout


def _read_blobs(root: Path, blobs: Mapping[str, str]) -> dict[str, bytes]:
    """Each path's bytes from its blob, through one ``git cat-file --batch`` (answers come back in order)."""
    output = _git_output(root, "cat-file", "--batch", data="".join(f"{sha}\n" for sha in blobs.values()).encode())
    contents: dict[str, bytes] = {}
    offset = 0
    for path in blobs:
        header_end = output.index(b"\n", offset)
        size = int(output[offset:header_end].split()[2])
        contents[path] = output[header_end + 1 : header_end + 1 + size]
        offset = header_end + size + 2
    return contents


def _installed_seed(root: Path) -> SeedUnion:
    """A born clone's root commit, read from Git objects: the seed exactly as it was installed
    (the seal's commit in the born-clone gate, the commit genesis makes of a newborn). Nothing
    written since, an owner's work, hydrated files, runtime state or another smoke's scratch,
    is read. Without exactly one root commit nothing records what was installed: unmeasured."""
    try:
        roots = _git_output(root, "rev-list", "--max-parents=0", "HEAD").split()
        if len(roots) != 1:
            return SeedUnion("installed seed", {}, f"this born clone has {len(roots)} root commits, so none is known to record the installed seed")
        listing = _git_output(root, "ls-tree", "-r", "-z", "--full-tree", roots[0].decode())
    except (OSError, RuntimeError) as exc:
        return SeedUnion("installed seed", {}, f"this born clone's Git records no installed seed ({exc})")
    blobs: dict[str, str] = {}
    for meta, path in (record.split(b"\t", 1) for record in listing.split(b"\0") if record):
        mode, kind, sha = meta.decode().split()
        if kind == "blob" and mode != "120000":  # a symlink's blob is its target's name, not content
            blobs[path.decode()] = sha
    contents = _read_blobs(root, blobs)
    return SeedUnion("installed seed", {"installed seed": frozenset(contents)}, blobs=contents)


def seed_union(root: Path) -> SeedUnion:
    """The union the scan reads: the factory's profiles where the factory is present (the
    development checkout and its candidates), a born clone's installed seed where a seed's
    marker is present, and an unmeasured union anywhere else."""
    if (root / "plugins" / _FACTORY / "src").is_dir():
        return _factory_union(root)
    if (root / _SEED_MARKER).is_file():
        return _installed_seed(root)
    return SeedUnion("none", {}, f"this tree carries neither the seed factory nor a seed's {_SEED_MARKER}")


def _print_kept(report: ScanReport) -> None:
    """Print every kept line with its path, site, digest and observed count against its
    maximum, then the totals by reason and the reviewed homonyms seen."""
    observed: Counter[str] = Counter()
    recorded: Counter[str] = Counter()
    for path, site, reason, digests in _KEPT_LINES:
        for digest in digests.split():
            count = report.kept[(path, site, digest)]
            observed[reason] += count
            recorded[reason] += 1
            print(f"  KEPT  {reason} | {path} | {site} | {digest} | {count}/{_PIN_MAXIMUM}")
    print("  kept lines: " + ", ".join(f"{reason} {observed[reason]} of {recorded[reason]}" for reason in recorded))
    reviewed = sum(sum(allowed.values()) for allowed in _ALLOWED_HOMONYMS.values())
    seen = {path for path, _digest_ in report.homonyms}
    print(f"  reviewed homonyms: {sum(report.homonyms.values())} of {reviewed} occurrences, in {len(seen)} of {len(_ALLOWED_HOMONYMS)} paths")


def check_scan() -> str:
    """The scan leg on this tree; returns why it could not be measured, '' when it was."""
    union = seed_union(ROOT)
    print(f"SCAN ({union.source})")
    if union.unmeasured:
        print(f"  UNMEASURED  {union.unmeasured}")
        return f"scan: {union.unmeasured}"
    for name, paths in sorted(union.profiles.items()):
        print(f"  profile {name}: {len(paths)} paths")
    paths = union.paths
    report = scan(ROOT, paths, union.blobs)
    print(f"  union: {len(paths)} paths, {report.scanned} read")
    print("  matched lines: " + ", ".join(f"{name} {report.matched[name]}" for name in _VOCABULARY))
    _print_kept(report)
    for finding in report.findings:
        print(f"  FINDING {finding.render(union.profiles)}")
    for path in report.vanished:
        print(f"  ADVISORY  {path} was listed but removed by another process before it was read; it is no longer part of this tree")
    _check(all(union.profiles.values()), "every seed profile enumerates at least one path (an empty enumeration is never clean)")
    _check(report.scanned + len(report.vanished) == len(paths), f"every enumerated file was read ({report.scanned} of {len(paths)}, {len(report.vanished)} vanished)")
    _check(not report.findings, f"the union holds no match outside the reviewed lines ({len(report.findings)} findings)")
    return ""


# --- Controls: the scanner reports each introduced line where it was put, and nothing else ---

_CHAR = "char" + "ter"
_UNIT_KEY = "unit" + "_id"
_LANE_KEY = "lane" + "_id"
_BUDGET = "budget" + "_line"
_DISPATCH_KEY = "dispatch" + "_id"
_CLASS_COLUMN = "role" + "_class"
_RETIRED_TABLE = "lane" + "_char" + "ter"
_CLASSIFIER_FILE_LINE = "c2980fc970157a7e6aca5397272f90af89de06a6ca7bbbcb4a09a0da1ac0f803"
_CLASSIFIER_MODE_LINE = "7120d3c05736b9b4a776524a3c7cd229b1a38611350a12a5789d1d2264b73ddb"
_CLASS_COLUMN_LINE = "1bab4bb8ce4428bd6c4c4234fab8c4b268c52449263b121c94a4628d87c94d20"
_UPGRADE_TEST_LINE = "c5978a5619239d28edeb79a6bbc55080c7804facf982fca5ebe27ca96e279908"
_NEGATIVE_LINE = "6b8068012cb14d54d60790a3616181e937fc66b15585c5a25deecfd2663c1667"
_IN_FUNCTION = (b"\n", b"\n", b"def relocated_site() -> object:\n")
_PLUGIN = "plugins/agent_messaging_plugin/"
_TESTS = _PLUGIN + "tests/"
_SOURCE = _PLUGIN + "src/agent_messaging_plugin/"


@dataclass(frozen=True)
class Control:
    """A scratch tree: ``files`` (each as its lines) are listed for the scan, ``unlisted`` files
    are written but not listed, and ``expected`` holds every (path, line) that must be reported."""

    label: str
    files: dict[str, list[bytes]]
    expected: frozenset[tuple[str, int]]
    unlisted: dict[str, bytes] = field(default_factory=dict[str, bytes])


def _real(path: str) -> list[bytes]:
    """The lines of a real file of this tree, the bytes a control starts from."""
    return _lines((ROOT / path).read_bytes())


def _at(lines: list[bytes], digest: str) -> int:
    """The index of the line with this digest, wherever the file now has it."""
    return next(index for index, line in enumerate(lines) if _digest(line) == digest)


def _added(label: str, path: str, line: bytes, *, before: Sequence[bytes] = (), after: Sequence[bytes] = (), base: list[bytes] | None = None) -> Control:
    """``line`` appended to ``path`` (or to ``base``), between ``before`` and ``after``."""
    lines = [*(_real(path) if base is None else base), *before, line, *after]
    return Control(label, {path: lines}, frozenset({(path, len(lines) - len(after))}))


def _changed(label: str, path: str, digest: str, old: bytes, new: bytes) -> Control:
    """One byte of a reviewed line changed in place; the line keeps its matching token."""
    lines = _real(path)
    index = _at(lines, digest)
    lines[index] = lines[index].replace(old, new, 1)
    return Control(label, {path: lines}, frozenset({(path, index + 1)}))


def _duplicated(label: str, path: str, digest: str) -> Control:
    """A reviewed line copied directly below itself, inside the same site; the copy is the extra one."""
    lines = _real(path)
    index = _at(lines, digest)
    lines.insert(index + 1, lines[index])
    return Control(label, {path: lines}, frozenset({(path, index + 2)}))


def _moved(label: str, path: str, digest: str, wrapper: tuple[bytes, bytes], replacement: bytes | None = None) -> Control:
    """The line removed from its site (or replaced there, where removal would not parse) and
    put inside a new function at the end of the same file."""
    lines = _real(path)
    index = _at(lines, digest)
    line = lines[index]
    if replacement is None:
        del lines[index]
    else:
        lines[index] = replacement
    return _added(label, path, line, before=(*_IN_FUNCTION, wrapper[0]), after=(wrapper[1],) if wrapper[1] else (), base=lines)


def _carried(label: str, source: str, digest: str, target: str, wrapper: tuple[bytes, bytes]) -> Control:
    """A reviewed line of ``source`` put inside a new function at the end of another file."""
    lines = _real(source)
    return _added(label, target, lines[_at(lines, digest)], before=(*_IN_FUNCTION, wrapper[0]), after=(wrapper[1],) if wrapper[1] else ())


def _controls() -> list[Control]:
    """The plan's scratch controls, on copies of the real files; every file stays Python that
    parses, so a reviewed line elsewhere in it is still recognised at its site."""
    backfill, store_smoke, spawn_smoke = _SOURCE + "session_origin_backfill.py", _TESTS + "session_lifecycle_store_smoke.py", _TESTS + "generic_spawn_smoke.py"
    status_smoke, verbs_smoke = _TESTS + "fleet_status_smoke.py", _TESTS + "session_lifecycle_verbs_smoke.py"
    as_dict, as_tuple, bare = (b"    return {\n", b"    }\n"), (b"    return (\n", b"    )\n"), (b"    pass\n", b"")
    # The classifier line is indented eight spaces, so the body it joins is too.
    classifier_body = (b"        row: dict[str, str] = {}\n", b"")
    return [
        _added("a new token on a reviewed homonym path", _PLUGIN + "plugin.yaml", f"# the {_CHAR} of a new note\n".encode()),
        _added("a new token in a test", status_smoke, f'_INJECTED = "{_UNIT_KEY}"\n'.encode()),
        _added("a new token in a gate", "quality_gates/sql_access_gate.py", f"# {_BUDGET}\n".encode()),
        _added("a new token in the quality service", "ananta/src/ananta/interfaces/quality_service_interface.py", f"# {_DISPATCH_KEY} attribution\n".encode()),
        _added("a new token beside a reviewed homonym in role binding", "ananta/src/ananta/llm/agent_messaging/role_binding.py", f"# {_LANE_KEY}\n".encode()),
        Control("a matching path", {_PLUGIN + f"knowledge_base/{_UNIT_KEY}.md": [b"clean\n"]}, frozenset({(_PLUGIN + f"knowledge_base/{_UNIT_KEY}.md", 0)})),
        Control("an unshipped file is ignored", {status_smoke: _real(status_smoke)}, frozenset(), {"workbench/unshipped.md": f"{_CHAR}\n".encode()}),
        _changed("an upgrade input changed by one byte", backfill, _CLASSIFIER_FILE_LINE, b"is None", b"is Nonf"),
        _duplicated("an upgrade input duplicated in its file", backfill, _CLASSIFIER_MODE_LINE),
        _carried("an upgrade input moved to another file", backfill, _CLASSIFIER_MODE_LINE, _SOURCE + "session_lifecycle_store.py", classifier_body),
        _moved("an upgrade input moved out of its site", backfill, _CLASSIFIER_MODE_LINE, classifier_body, replacement=b"        origin = None\n"),
        _added("a third class-column line in a test", status_smoke, f'_COLUMN = "{_CLASS_COLUMN}"\n'.encode()),
        _added("a third class-column line in a comment", _TESTS + "role_authority_smoke.py", f"# {_CLASS_COLUMN} stays retired\n".encode()),
        _carried("a copy of a pinned class-column line in another file", _TESTS + "role_authority_smoke.py", _CLASS_COLUMN_LINE, spawn_smoke, bare),
        _changed("an upgrade test input changed by one byte", store_smoke, _UPGRADE_TEST_LINE, b"unt_old", b"unt_olD"),
        _duplicated("an upgrade test input duplicated in its site", store_smoke, _UPGRADE_TEST_LINE),
        _moved("an upgrade test input moved to another site", store_smoke, _UPGRADE_TEST_LINE, as_dict),
        _carried("an upgrade test input moved to another file", store_smoke, _UPGRADE_TEST_LINE, _TESTS + "session_origin_backfill_smoke.py", as_dict),
        _changed("a negative assertion changed by one byte", spawn_smoke, _NEGATIVE_LINE, b"report_", b"rePort_"),
        _duplicated("a negative assertion duplicated in its site", spawn_smoke, _NEGATIVE_LINE),
        _moved("a negative assertion moved to another site", spawn_smoke, _NEGATIVE_LINE, as_tuple),
        _carried("a negative assertion moved to another file", spawn_smoke, _NEGATIVE_LINE, verbs_smoke, as_tuple),
        _added("a new line naming a retired table in a pinned file", store_smoke, f'_EXTRA_TABLE = "{_RETIRED_TABLE}"\n'.encode()),
        _added("a line the live-writer piece removed reappears", status_smoke, f'    _check(not any("{_LANE_KEY}" in row for row in result["sessions"]), "fleet_status entries carry no {_LANE_KEY}")\n'.encode(), before=(*_IN_FUNCTION[:2], b"def relocated_site(result: dict[str, list[str]]) -> None:\n")),
        _added("a line the prose piece removed reappears", verbs_smoke, f"# -- {_RETIRED_TABLE} capture + spawn_session first-turn (phase 2 slice 6) --\n".encode()),
    ]


def _run_control(control: Control) -> None:
    """Write the control's scratch tree, scan its listed files, and require exactly the expected
    reports: every introduced line where it was put, and no reviewed line around it."""
    with tempfile.TemporaryDirectory(prefix="no-work-model-control-") as scratch:
        root = Path(scratch)
        for path, data in {**{path: b"".join(lines) for path, lines in control.files.items()}, **control.unlisted}.items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_bytes(data)
        report = scan(root, sorted(control.files))
        reported = frozenset((finding.path, finding.line) for finding in report.findings)
        _check(reported == control.expected and report.scanned == len(control.files), f"control: {control.label} (reported {sorted(reported)}, expected {sorted(control.expected)})")


def _git(root: Path, *argv: str) -> None:
    """Git in a scratch repository, with no hooks, a fixture identity and a clean environment."""
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", *argv], cwd=root, env=scrubbed_git_environment(), capture_output=True, check=True)


def _check_installed_seed_controls() -> None:
    """A born clone is scanned as its root commit recorded it: with no commit it is unmeasured;
    after the seal, an owner's file and a later change to a seed file are not read."""
    with tempfile.TemporaryDirectory(prefix="no-work-model-birth-") as scratch:
        root = Path(scratch)
        (root / _SEED_MARKER).write_text("{}\n", encoding="utf-8")
        shipped = root / "plugins" / "shipped" / "notes.md"
        shipped.parent.mkdir(parents=True)
        shipped.write_text(f"a {_CHAR}\n", encoding="utf-8")
        (root / "plugins" / "linked").symlink_to(shipped.parent)
        _git(root, "init", "-q")
        _check(seed_union(root).unmeasured != "", "control: a born clone with no commit is reported unmeasured, never clean")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "seal")
        (root / "owner.md").write_text(f"the owner's {_CHAR}\n", encoding="utf-8")
        shipped.write_text(f"a {_CHAR}\nhydrated, another {_CHAR}\n", encoding="utf-8")
        union = seed_union(root)
        _check(union.source == "installed seed" and union.paths == [_SEED_MARKER, "plugins/shipped/notes.md"], f"control: a born clone lists its root commit's files and no symlink (got {union.paths})")
        found = [(finding.path, finding.line) for finding in scan(root, union.paths, union.blobs).findings]
        _check(found == [("plugins/shipped/notes.md", 1)], f"control: the sealed token is reported and nothing written after the seal is read (got {found})")


def check_controls() -> str:
    """The controls leg: the scanner can fail, at every place the plan names."""
    print("CONTROLS")
    for control in _controls():
        _run_control(control)
    _check_installed_seed_controls()
    return ""


# --- Runtime: start, list, meter and stop from the generic fields alone ---

# The register's variable prefix, as a pattern for the same reason as the name vocabulary.
_REGISTER_VARIABLE = re.compile("PROJECT[_]SOLET_")
_EXEC_NAMES = ("execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe")
_METER_BUCKETS = ("sessions_covered", "sessions_uncovered", "sessions_excluded_operator_hosted", "sessions_excluded_pre_landing", "sessions_no_llm_turns")


@contextmanager
def _isolated(violations: list[str]) -> Generator[Path]:
    """HOME at a new empty directory, no variable with the register's prefix, and any argv
    naming the register CLI or git refused."""
    with tempfile.TemporaryDirectory(prefix="no-work-model-home-") as home, ExitStack() as stack:
        environment = {key: value for key, value in os.environ.items() if not _REGISTER_VARIABLE.match(key)}
        stack.enter_context(patch.dict(os.environ, {**environment, "HOME": home}, clear=True))
        stack.enter_context(patch.object(subprocess, "run", launch._guarded(subprocess.run, violations)))
        stack.enter_context(patch.object(subprocess, "Popen", launch._guarded(subprocess.Popen, violations)))
        for name in _EXEC_NAMES:
            stack.enter_context(patch.object(os, name, launch._guarded(getattr(os, name), violations)))
        yield Path(home)


def _stamp_created_at(fx: Any) -> None:
    """The real state layer stamps created_at on insert; the paged read budget_report uses needs it."""
    write = fx.state.write_state

    def stamped(namespace: str, data: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        if data.get("table") == TABLE_MANAGED_SESSION:
            data = {**data, "record": {"created_at": datetime.now(UTC), **data["record"]}}
        return cast(dict[str, Any], write(namespace, data, **kwargs))

    fx.state.write_state = stamped


def _data(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """A verb result's data, or an empty mapping when it failed."""
    return cast(dict[str, Any], envelope.get("data") or {})


def _sessions(envelope: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The sessions a list_sessions result returned."""
    return cast(list[dict[str, Any]], _data(envelope).get("sessions") or [])


@dataclass(frozen=True)
class Walk:
    """One generic session's walk: who calls (None: a context with no session identity)."""

    caller: str | None

    @property
    def who(self) -> str:
        """How the walk's checks name it."""
        return "with a calling session" if self.caller else "with no session identity"

    @property
    def label(self) -> str:
        """The opaque label the walk starts its session under and reads it back by."""
        return "no-work-model-" + ("caller" if self.caller else "anonymous")


def _start(fx: Any, walk: Walk) -> str:
    """Start from the seven generic fields only; return the new instance id."""
    _stamp_created_at(fx)
    fx.seed_cell(launch._MODEL)
    launch._install_driver(fx, launch._TerminatingDriver())
    params = {"agent_runtime": launch._RUNTIME, "model": launch._MODEL, "effort": launch._EFFORT, "workspace": fx.workspace, "first_message": launch._FIRST_MESSAGE, "permission_mode": "bypassPermissions", "label": walk.label}
    receipt, envelope = fx.spawn(params, caller=walk.caller)
    instance = str(_data(envelope).get("agent_instance_id") or "")
    _check(receipt.get("action_status") == "completed" and instance.startswith("agi-"), f"start {walk.who}: spawn_session completes from runtime, model, effort, workspace, first message, permissions and label")
    lineage = [row.get("spawned_by_instance_id") for row in fx.rows(TABLE_MANAGED_SESSION) if row.get("agent_instance_id") == instance]
    _check(lineage == [walk.caller or ""], f"start {walk.who}: the row's lineage is {walk.caller or 'empty'} (got {lineage})")
    return instance


def _list_and_meter(fx: Any, walk: Walk, instance: str) -> None:
    """list_sessions and budget_report, each asked by the label alone."""
    listed = [(entry.get("agent_instance_id"), entry.get("label")) for entry in _sessions(fx.call("list_sessions", {"label": walk.label}, caller=walk.caller))]
    _check(listed == [(instance, walk.label)], f"list {walk.who}: list_sessions by label returns the session with its label (got {listed})")
    metered = cast(list[dict[str, Any]], _data(fx.call("budget_report", {"label": walk.label}, caller=walk.caller)).get("labels") or [])
    sessions = sum(int(entry.get(bucket) or 0) for entry in metered for bucket in _METER_BUCKETS)
    _check([entry.get("label") for entry in metered] == [walk.label] and sessions == 1, f"meter {walk.who}: budget_report by label meters one session under the label (got {metered})")


def _stop(fx: Any, walk: Walk, instance: str) -> None:
    """retire_session by instance id: the host is observed dead, the session reads retired and
    the workspace the caller gave is left exactly as it was."""
    stopped = _data(fx.call("retire_session", {"agent_instance_id": instance}, caller=walk.caller))
    _check((stopped.get("already_retired"), stopped.get("host_liveness")) == (False, "dead"), f"stop {walk.who}: retire_session stops the host (got {stopped.get('host_liveness')})")
    states = [entry.get("lifecycle_state") for entry in _sessions(fx.call("list_sessions", {"label": walk.label}, caller=walk.caller))]
    _check(states == ["retired"], f"stop {walk.who}: the session reads retired (got {states})")
    _check(os.path.isdir(fx.workspace) and os.listdir(fx.workspace) == [], f"stop {walk.who}: the workspace still exists, untouched")


def _check_session_lifecycle(caller: str | None) -> None:
    """Start, list, meter and stop as four separate observations, each through the plugin's verb."""
    walk = Walk(caller)
    with launch._fixture() as fx:
        instance = _start(fx, walk)
        _list_and_meter(fx, walk, instance)
        _stop(fx, walk, instance)


def _check_turned_off_cell() -> None:
    """A start on a model cell a policy rule turned off is refused naming that rule, first."""
    with launch._fixture() as fx:
        fx.seed_cell(launch._MODEL, disabled_by=launch._DISABLED_RULE)
        receipt, _envelope = fx.spawn(fx.generic_params(), caller=launch._CALLER)
        error = cast(dict[str, Any], receipt.get("error") or {})
        _check(error.get("code") == "model_cell_disabled" and launch._DISABLED_RULE in str(error.get("message")), f"a model cell a policy rule turned off is refused naming the rule (got {error.get('code')})")
        _check(fx.rows(TABLE_MANAGED_SESSION) == [] and fx.rows("job") == [] and fx.driver.specs == [], "the refusal comes before any row is written or host launched")


def check_runtime() -> str:
    """The runtime leg, inside the isolated environment; always measured."""
    print("RUNTIME")
    violations: list[str] = []
    with _isolated(violations) as home:
        _check(Path.home() == home and not any(home.iterdir()), "HOME is a new empty directory")
        _check(not [key for key in os.environ if _REGISTER_VARIABLE.match(key)], "no variable carrying the register's prefix is set")
        for caller in (launch._CALLER, None):
            _check_session_lifecycle(caller)
        _check_turned_off_cell()
        print(f"  HOME holds {len(list(home.iterdir()))} entries after the leg")
    _check(violations == [], f"no argv naming the register CLI or git was executed (saw {violations})")
    declared = frozenset(launch._declared_parameters())
    _check(declared == launch._GENERIC_FIELDS, f"spawn_session declares exactly the generic parameters (extra {sorted(declared - launch._GENERIC_FIELDS)}, missing {sorted(launch._GENERIC_FIELDS - declared)})")
    _check(not [name for name in declared if _tokens(name.encode())], "no declared parameter names a removed field")
    return ""


# --- Upgrade: a populated predecessor starts as the release, then starts again ---

_NAMESPACE = AGENT_ROLE_BINDING_NAMESPACE


@dataclass(frozen=True)
class Observation:
    """One read of the database: live tables and managed_session's columns, ownership, row
    counts, the non-null cells of the removed columns, each seeded session's row id, origin
    and three classifier inputs, and the classifier's completion marker."""

    tables: frozenset[str]
    session_columns: frozenset[str]
    owned: dict[str, str]
    counts: dict[str, int]
    former_values: int
    rows: dict[str, tuple[object, ...]]
    marker: dict[str, Any]


def _former_values(state: StateManagementInterface) -> int:
    """How many cells of the removed managed_session columns hold a value."""
    stored = require_records(state.query_state(_NAMESPACE, {"table": TABLE_MANAGED_SESSION}))
    return sum(1 for record in stored for column in upgrade._REMOVED_SESSION_COLUMNS if record.get(column) is not None)


def _seeded_rows(state: StateManagementInterface) -> dict[str, tuple[object, ...]]:
    """Each seeded session's row id, origin and three classifier inputs, read by its identity."""
    return {identity: (row.get("id"), row.get("session_origin"), *(row.get(name) for name in upgrade._UPGRADE_INPUTS)) for identity in upgrade._HISTORICAL_SESSIONS for row in (read_managed_session(state, identity),)}


def _observe(fixture: LedgerFixture) -> Observation:
    """Read everything an Observation holds, through the state service and the plugin's ownership read."""
    state = cast("StateManagementInterface", fixture.state)
    live = upgrade._tables(state)
    tables = frozenset(name.removeprefix(f"{_NAMESPACE}__") for name in live)
    counts = {name: len(require_records(state.query_state(_NAMESPACE, {"table": name}))) for name in (TABLE_MANAGED_SESSION, *upgrade._RETIRED_TABLES) if name in tables}
    owned = {str(name): str(entry["status"]) for name, entry in fixture.plugin.get_installed_schema(_NAMESPACE)["tables"].items()}
    marker = cast(dict[str, Any], require_completed(state.get_key_value(_NAMESPACE, session_origin_backfill.SESSION_ORIGIN_MARKER), "read the origin marker"))
    return Observation(tables, frozenset(live[upgrade._SESSION_TABLE]["columns"]), owned, counts, _former_values(state), _seeded_rows(state), marker)


def _start_release(fixture: LedgerFixture) -> list[dict[str, Any]]:
    """Install the release's whole messaging declaration as a start does: SchemaManager over the
    state plugin's install_plugin_schema, whose actual results are recorded."""
    results: list[dict[str, Any]] = []
    install = fixture.plugin.install_plugin_schema

    def recorded(namespace: str, declared: dict[str, Any]) -> dict[str, Any]:
        result = install(namespace, declared)
        results.append({"namespace": namespace, **result})
        return result

    with patch.object(fixture.plugin, "install_plugin_schema", recorded):
        manager = SchemaManager(fixture.state, fixture.plugin)
        manager.initialize_schemas([CoreSchemaDefinitions.get_key_value_store_schema(), *AgentMessagingPlugin().get_schema_definitions()])
    fixture.state.set_schema_manager(manager)
    return results


def _retired(results: Sequence[Mapping[str, Any]]) -> list[str]:
    """The tables the install results say owner-aware retirement dropped."""
    return sorted(str(name) for result in results for name in result.get("retired_tables") or ())


def _check_predecessor(before: Observation) -> None:
    """The predecessor as seeded, before the release starts: owned, populated, no marker."""
    retired = set(upgrade._RETIRED_TABLES)
    print(f"  before: retired tables {sorted(retired & before.tables)}, rows {before.counts}, removed-column values {before.former_values}, marker {before.marker}")
    _check(retired <= before.tables and all(before.owned.get(name) == "active" for name in retired), "setup: the three predecessor tables exist and ownership records them active")
    _check(all(before.counts.get(name, 0) >= 1 for name in retired) and before.counts.get(TABLE_MANAGED_SESSION) == len(upgrade._HISTORICAL_SESSIONS), "setup: each predecessor table holds a row and every historical session is seeded")
    populated = len(upgrade._HISTORICAL_SESSIONS) * len(upgrade._REMOVED_SESSION_COLUMNS)
    _check(set(upgrade._REMOVED_SESSION_COLUMNS) <= before.session_columns and before.former_values == populated, f"setup: managed_session holds every removed column, populated in every seeded row ({before.former_values} of {populated})")
    seeded = {identity: (row[2], row[3] or "", row[4]) for identity, row in before.rows.items()}
    expected = {identity: (spec[0], spec[1], spec[2]) for identity, spec in upgrade._HISTORICAL_SESSIONS.items()}
    _check(seeded == expected, "setup: every historical session reads back its three classifier inputs")
    _check(before.marker.get("found") is False, "setup: the classifier's completion marker is absent")


def _check_upgraded(before: Observation, after: Observation, installs: Sequence[Mapping[str, Any]], classified: Mapping[str, object]) -> None:
    """The first start of the release: retirement, columns, origins, row ids and the marker."""
    retired = set(upgrade._RETIRED_TABLES)
    origins = {identity: row[1] for identity, row in after.rows.items()}
    print(f"  start: retired {_retired(installs)}, classifier {dict(classified)}, rows {after.counts}, origins {origins}, marker {after.marker}")
    _check(_retired(installs) == sorted(retired), "start: owner-aware retirement dropped exactly the three populated predecessor tables")
    _check(not retired & after.tables and not retired & set(after.owned), "start: the three tables are physically gone and ownership no longer records them")
    _check(after.session_columns.isdisjoint(upgrade._REMOVED_SESSION_COLUMNS) and after.former_values == 0 and set(upgrade._UPGRADE_INPUTS) | {"session_origin"} <= after.session_columns, "start: the removed columns are dropped; the three upgrade inputs stay and session_origin is added")
    _check(classified == {"status": "completed", "updated": 4}, f"start: the classifier classifies the four classifiable sessions (got {dict(classified)})")
    _check(origins == {identity: spec[4] for identity, spec in upgrade._HISTORICAL_SESSIONS.items()}, "start: every seeded session records the origin its inputs imply")
    _check({identity: row[0] for identity, row in after.rows.items()} == {identity: row[0] for identity, row in before.rows.items()}, "start: every seeded session keeps its row id")
    _check(after.marker.get("found") is True and after.marker.get("value") == "history_scanned", "start: the durable completion marker reads back as scanned")


def _check_restart(after: Observation, again: Observation, installs: Sequence[Mapping[str, Any]], classified: Mapping[str, object], writes: Sequence[str]) -> None:
    """The second start on the same rows: the marker's fast path, and nothing changed or dropped again."""
    print(f"  restart: retired {_retired(installs)}, classifier {dict(classified)}, marker writes {list(writes)}")
    _check(_retired(installs) == [], "restart: no table is retired again")
    _check(classified == {"status": "already_done", "updated": 0} and not writes, "restart: the classifier returns already_done, updates nothing and writes no marker")
    _check((again.tables, again.session_columns, again.counts, again.rows, again.marker) == (after.tables, after.session_columns, after.counts, after.rows, after.marker), "restart: tables, columns, row ids, origins and the marker are unchanged")


def check_upgrade() -> str:
    """The upgrade leg on a private PostgreSQL: start the release on a populated predecessor,
    stop, and start it again on the same database; unmeasured without the fixture manifest."""
    print("UPGRADE")
    if not os.environ.get(MANIFEST_ENV):
        print(f"  UNMEASURED  no private PostgreSQL ({MANIFEST_ENV} is unset)")
        return "upgrade: no private PostgreSQL"
    schema = "no_work_model_upgrade_" + uuid4().hex[:12]
    with ledger_fixture("messaging_write", schema=schema) as fixture:
        state = cast("StateManagementInterface", fixture.state)
        upgrade._install(fixture.state, fixture.plugin, upgrade._predecessor(get_session_lifecycle_schema_definition()))
        upgrade._seed_predecessor(state)
        before = _observe(fixture)
        _check_predecessor(before)
        installs = _start_release(fixture)
        classified = session_origin_backfill.backfill_session_origins(state)
        after = _observe(fixture)
        _check_upgraded(before, after, installs, classified)
    with ledger_fixture("messaging_write", schema=schema) as fixture:
        state = cast("StateManagementInterface", fixture.state)
        installs = _start_release(fixture)
        writes: list[str] = []
        set_key_value = state.set_key_value

        def recorded(*args: Any, **kwargs: Any) -> Any:
            writes.append(str(args[1] if len(args) > 1 else kwargs.get("key")))
            return set_key_value(*args, **kwargs)

        with patch.object(state, "set_key_value", recorded):
            classified = session_origin_backfill.backfill_session_origins(state)
        _check_restart(after, _observe(fixture), installs, classified, writes)
    return ""


def _run_leg(leg: Callable[[], str]) -> str:
    """Run one leg; return why it could not be measured here, '' when it was."""
    try:
        return leg()
    except Exception:  # noqa: BLE001 -- one leg's crash must not hide the others
        traceback.print_exc()
        _failed.append(f"{leg.__name__} raised")
        return ""


def main() -> int:
    """Run every leg; exit 1 on any failed check, 77 when a leg was unmeasured, else 0."""
    unmeasured = [reason for reason in map(_run_leg, (check_scan, check_controls, check_runtime, check_upgrade)) if reason]
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for line in [f"  FAILED: {label}" for label in _failed] + [f"UNMEASURED: {reason}" for reason in unmeasured]:
        print(line)
    if _failed:
        return 1
    # Exit 77 is a disclosed skip; the private PostgreSQL one prints the born-clone gate's witness.
    if any(reason.startswith("upgrade") for reason in unmeasured):
        return skip_fixture()
    return 77 if unmeasured else 0


if __name__ == "__main__":
    sys.exit(main())
