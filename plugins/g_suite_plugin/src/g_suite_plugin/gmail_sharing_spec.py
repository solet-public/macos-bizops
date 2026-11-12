"""Gmail build 2, Workspace-leaning half: delegates, send-as, S/MIME and CSE.

Rows use :class:`gmail_settings_spec.SettingsVerb`; see that module for the
contract. Delegates, S/MIME and client-side encryption are Google Workspace
features, so their verbs refuse on a consumer account naming the tier. Send-as
reads, patch and update work on every tier (they carry signatures and reply-to
addresses); creating, deleting or verifying a send-as alias needs
``gmail.settings.sharing`` and is Workspace only.
"""

from __future__ import annotations

from .constants import GMAIL_SETTINGS_BASIC_SCOPE
from .gmail_settings_spec import (
    BOOL,
    GMAIL_SETTINGS_SHARING_SCOPE,
    INT,
    INT64,
    OBJECT_LIST,
    STR,
    Field,
    SettingsVerb,
    paging_fields,
)

_READ = "Reads with the default gmail.modify scope."
_BASIC = "Needs the gmail.settings.basic scope."
_SHARING = "Needs gmail.settings.sharing, which Google grants only to a service account with domain-wide delegation."
_WORKSPACE = "Workspace only: a consumer account is refused naming its tier."

_DELEGATE_EMAIL = Field("delegate_email", "delegateEmail", STR, "The delegate's email address (a user in the same Workspace organization).", required=True)
_SEND_AS_EMAIL = Field("send_as_email", "sendAsEmail", STR, "The send-as address, e.g. the account's own address or an alias (from gmail_list_send_as).", required=True)
_SMIME_ID = Field("id", "id", STR, "The S/MIME config id (from gmail_list_smime_info).", required=True)
_CSE_EMAIL = Field("cse_email_address", "cseEmailAddress", STR, "The email address of the client-side encryption identity.", required=True)
_KEY_PAIR_ID = Field("key_pair_id", "keyPairId", STR, "The client-side encryption key pair id (from gmail_list_cse_key_pairs).", required=True)

_SEND_AS_FIELDS = (
    Field("display_name", "displayName", STR, "Name shown in the From header."),
    Field("reply_to_address", "replyToAddress", STR, "Reply-To address for mail sent from this address; empty clears it."),
    Field("signature", "signature", STR, "HTML signature added to new mail sent from this address."),
    Field("is_default", "isDefault", BOOL, "Make this the default From address (only true is accepted by Gmail)."),
    Field("treat_as_alias", "treatAsAlias", BOOL, "Treat as an alias of the account (Gmail's 'Treat as an alias' option)."),
    Field("smtp_host", "smtpMsa.host", STR, "Outbound SMTP relay host for a custom From address."),
    Field("smtp_port", "smtpMsa.port", INT, "SMTP relay port."),
    Field("smtp_username", "smtpMsa.username", STR, "SMTP relay user name (write-only at Gmail)."),
    Field("smtp_password", "smtpMsa.password", STR, "SMTP relay password (write-only at Gmail)."),
    Field("smtp_security_mode", "smtpMsa.securityMode", STR, "SMTP relay security: none, ssl or starttls.", enum=("securityModeUnspecified", "none", "ssl", "starttls")),
    Field("is_primary", "isPrimary", BOOL, "Read-only at Gmail (the login address); accepted because the resource documents it."),
    Field("verification_status", "verificationStatus", STR, "Read-only at Gmail (set by verification); accepted because the resource documents it.", enum=("verificationStatusUnspecified", "accepted", "pending")),
)
_SEND_AS_BODY_EMAIL = Field("send_as_email", "sendAsEmail", STR, "The send-as address (also sent in the body).")
_SMIME_FIELDS = (
    Field("pkcs12", "pkcs12", STR, "PKCS#12 key and certificate chain, base64url-encoded (one key).", required=True),
    Field("encrypted_key_password", "encryptedKeyPassword", STR, "Password for the PKCS#12 file, if it has one."),
    Field("is_default", "isDefault", BOOL, "Make this the default S/MIME config for the send-as address."),
    Field("pem", "pem", STR, "PEM-formatted X509 certificate chain (Gmail normally derives it from pkcs12)."),
    Field("issuer_cn", "issuerCn", STR, "Certificate issuer common name (normally derived by Gmail)."),
    Field("expiration", "expiration", INT64, "Certificate expiry in milliseconds since the epoch (normally derived by Gmail)."),
    Field("id", "id", STR, "Config id (normally assigned by Gmail)."),
)
_CSE_IDENTITY_FIELDS = (
    Field("primary_key_pair_id", "primaryKeyPairId", STR, "Key pair used to both sign and encrypt (set this OR the two separate ids)."),
    Field("encryption_key_pair_id", "signAndEncryptKeyPairs.encryptionKeyPairId", STR, "Key pair used to encrypt, when signing uses a different one."),
    Field("signing_key_pair_id", "signAndEncryptKeyPairs.signingKeyPairId", STR, "Key pair used to sign, when encryption uses a different one."),
)
_KEY_PAIR_STATE = ("Gmail: {} CSE Key Pair", "{} a client-side encryption key pair (users.settings.cse.keypairs.{}).")

DELEGATE_VERBS: tuple[SettingsVerb, ...] = (
    SettingsVerb(
        "gmail_list_delegates",
        ("settings", "delegates", "list"),
        "GET settings/delegates",
        "Gmail: List Delegates",
        f"List the users who can read, send and delete this mailbox's mail (users.settings.delegates.list) with each verification status. {_READ} {_WORKSPACE}",
        workspace_only=True,
        list_key="delegates",
        rows_name="delegates",
    ),
    SettingsVerb("gmail_get_delegate", ("settings", "delegates", "get"), "GET settings/delegates/{delegateEmail}", "Gmail: Get Delegate", f"Read one mailbox delegate and its verification status (users.settings.delegates.get). {_READ} {_WORKSPACE}", workspace_only=True, path=(_DELEGATE_EMAIL,)),
    SettingsVerb(
        "gmail_create_delegate",
        ("settings", "delegates", "create"),
        "POST settings/delegates",
        "Gmail: Add Delegate",
        f"Grant another user in the organization delegate access to this mailbox (users.settings.delegates.create). {_SHARING} {_WORKSPACE}",
        scope=GMAIL_SETTINGS_SHARING_SCOPE,
        workspace_only=True,
        body=(
            Field("delegate_email", "delegateEmail", STR, "The user to grant delegate access to.", required=True),
            Field("verification_status", "verificationStatus", STR, "Read-only at Gmail (set when the delegate accepts); accepted because the resource documents it.", enum=("verificationStatusUnspecified", "accepted", "pending", "rejected", "expired")),
        ),
        has_body=True,
    ),
    SettingsVerb(
        "gmail_delete_delegate",
        ("settings", "delegates", "delete"),
        "DELETE settings/delegates/{delegateEmail}",
        "Gmail: Remove Delegate",
        f"Revoke a delegate's access to this mailbox (users.settings.delegates.delete). {_SHARING} {_WORKSPACE}",
        scope=GMAIL_SETTINGS_SHARING_SCOPE,
        workspace_only=True,
        path=(_DELEGATE_EMAIL,),
        empty_result="deleted",
    ),
)

SEND_AS_VERBS: tuple[SettingsVerb, ...] = (
    SettingsVerb(
        "gmail_list_send_as",
        ("settings", "sendAs", "list"),
        "GET settings/sendAs",
        "Gmail: List Send-As Addresses",
        f"List the From addresses this account can send as (users.settings.sendAs.list): the primary address and aliases, with display name, reply-to, signature, default flag and verification status. {_READ}",
        list_key="sendAs",
        rows_name="send_as",
    ),
    SettingsVerb("gmail_get_send_as", ("settings", "sendAs", "get"), "GET settings/sendAs/{sendAsEmail}", "Gmail: Get Send-As Address", f"Read one send-as address (users.settings.sendAs.get): display name, reply-to, signature, SMTP relay and verification status. {_READ}", path=(_SEND_AS_EMAIL,)),
    SettingsVerb(
        "gmail_create_send_as",
        ("settings", "sendAs", "create"),
        "POST settings/sendAs",
        "Gmail: Create Send-As Address",
        f"Create a custom From alias (users.settings.sendAs.create), optionally with an SMTP relay; Gmail verifies the address before it can be used. {_SHARING} {_WORKSPACE}",
        scope=GMAIL_SETTINGS_SHARING_SCOPE,
        workspace_only=True,
        body=(Field("send_as_email", "sendAsEmail", STR, "The alias address to send as.", required=True), *_SEND_AS_FIELDS),
        has_body=True,
    ),
    SettingsVerb(
        "gmail_patch_send_as",
        ("settings", "sendAs", "patch"),
        "PATCH settings/sendAs/{sendAsEmail}",
        "Gmail: Patch Send-As Address",
        f"Change only the send-as fields passed (users.settings.sendAs.patch): signature, display name, reply-to, default, alias flag or SMTP relay. {_BASIC}",
        scope=GMAIL_SETTINGS_BASIC_SCOPE,
        path=(_SEND_AS_EMAIL,),
        body=(_SEND_AS_BODY_EMAIL, *_SEND_AS_FIELDS),
        has_body=True,
    ),
    SettingsVerb(
        "gmail_update_send_as",
        ("settings", "sendAs", "update"),
        "PUT settings/sendAs/{sendAsEmail}",
        "Gmail: Replace Send-As Address",
        f"Replace a send-as address in full (users.settings.sendAs.update, HTTP PUT); fields not passed are reset. Use gmail_patch_send_as to change one field such as the signature. {_BASIC}",
        scope=GMAIL_SETTINGS_BASIC_SCOPE,
        path=(_SEND_AS_EMAIL,),
        body=(_SEND_AS_BODY_EMAIL, *_SEND_AS_FIELDS),
        has_body=True,
    ),
    SettingsVerb(
        "gmail_delete_send_as",
        ("settings", "sendAs", "delete"),
        "DELETE settings/sendAs/{sendAsEmail}",
        "Gmail: Delete Send-As Address",
        f"Delete a custom From alias and revoke its verification (users.settings.sendAs.delete). {_SHARING} {_WORKSPACE}",
        scope=GMAIL_SETTINGS_SHARING_SCOPE,
        workspace_only=True,
        path=(_SEND_AS_EMAIL,),
        empty_result="deleted",
    ),
    SettingsVerb(
        "gmail_verify_send_as",
        ("settings", "sendAs", "verify"),
        "POST settings/sendAs/{sendAsEmail}/verify",
        "Gmail: Resend Send-As Verification",
        f"Send the verification email for a pending send-as alias again (users.settings.sendAs.verify). {_SHARING} {_WORKSPACE}",
        scope=GMAIL_SETTINGS_SHARING_SCOPE,
        workspace_only=True,
        path=(_SEND_AS_EMAIL,),
        empty_result="verification_sent",
    ),
)

SMIME_VERBS: tuple[SettingsVerb, ...] = (
    SettingsVerb(
        "gmail_list_smime_info",
        ("settings", "sendAs", "smimeInfo", "list"),
        "GET settings/sendAs/{sendAsEmail}/smimeInfo",
        "Gmail: List S/MIME Configs",
        f"List the S/MIME certificates for one send-as address (users.settings.sendAs.smimeInfo.list) with issuer, expiry and default flag. {_READ} {_WORKSPACE}",
        workspace_only=True,
        path=(_SEND_AS_EMAIL,),
        list_key="smimeInfo",
        rows_name="smime_info",
    ),
    SettingsVerb(
        "gmail_get_smime_info",
        ("settings", "sendAs", "smimeInfo", "get"),
        "GET settings/sendAs/{sendAsEmail}/smimeInfo/{id}",
        "Gmail: Get S/MIME Config",
        f"Read one S/MIME certificate config of a send-as address (users.settings.sendAs.smimeInfo.get). {_READ} {_WORKSPACE}",
        workspace_only=True,
        path=(_SEND_AS_EMAIL, _SMIME_ID),
    ),
    SettingsVerb(
        "gmail_insert_smime_info",
        ("settings", "sendAs", "smimeInfo", "insert"),
        "POST settings/sendAs/{sendAsEmail}/smimeInfo",
        "Gmail: Upload S/MIME Certificate",
        f"Upload an S/MIME key and certificate (PKCS#12) for a send-as address (users.settings.sendAs.smimeInfo.insert). {_BASIC} {_WORKSPACE}",
        scope=GMAIL_SETTINGS_BASIC_SCOPE,
        workspace_only=True,
        path=(_SEND_AS_EMAIL,),
        body=_SMIME_FIELDS,
        has_body=True,
    ),
    SettingsVerb(
        "gmail_set_default_smime_info",
        ("settings", "sendAs", "smimeInfo", "setDefault"),
        "POST settings/sendAs/{sendAsEmail}/smimeInfo/{id}/setDefault",
        "Gmail: Set Default S/MIME Config",
        f"Make one S/MIME config the default for its send-as address (users.settings.sendAs.smimeInfo.setDefault). {_BASIC} {_WORKSPACE}",
        scope=GMAIL_SETTINGS_BASIC_SCOPE,
        workspace_only=True,
        path=(_SEND_AS_EMAIL, _SMIME_ID),
        empty_result="default_set",
    ),
    SettingsVerb(
        "gmail_delete_smime_info",
        ("settings", "sendAs", "smimeInfo", "delete"),
        "DELETE settings/sendAs/{sendAsEmail}/smimeInfo/{id}",
        "Gmail: Delete S/MIME Config",
        f"Delete one S/MIME certificate config of a send-as address (users.settings.sendAs.smimeInfo.delete). {_BASIC} {_WORKSPACE}",
        scope=GMAIL_SETTINGS_BASIC_SCOPE,
        workspace_only=True,
        path=(_SEND_AS_EMAIL, _SMIME_ID),
        empty_result="deleted",
    ),
)


def _key_pair_state(action: str, method: str, verb: str, flag: str, detail: str) -> SettingsVerb:
    title, summary = _KEY_PAIR_STATE
    return SettingsVerb(
        verb,
        ("settings", "cse", "keypairs", method),
        f"POST settings/cse/keypairs/{{keyPairId}}:{method}",
        title.format(action),
        f"{summary.format(action, method)} {detail} {_BASIC} {_WORKSPACE}",
        scope=GMAIL_SETTINGS_BASIC_SCOPE,
        workspace_only=True,
        path=(_KEY_PAIR_ID,),
        has_body=True,
        empty_result=flag,
    )


CSE_VERBS: tuple[SettingsVerb, ...] = (
    SettingsVerb(
        "gmail_list_cse_identities",
        ("settings", "cse", "identities", "list"),
        "GET settings/cse/identities",
        "Gmail: List CSE Identities",
        f"List the client-side encryption identities of the user (users.settings.cse.identities.list), one page at a time. {_READ} {_WORKSPACE}",
        workspace_only=True,
        query=paging_fields(),
        list_key="cseIdentities",
        rows_name="cse_identities",
        paged=True,
    ),
    SettingsVerb(
        "gmail_get_cse_identity", ("settings", "cse", "identities", "get"), "GET settings/cse/identities/{cseEmailAddress}", "Gmail: Get CSE Identity", f"Read one client-side encryption identity and its key pair ids (users.settings.cse.identities.get). {_READ} {_WORKSPACE}", workspace_only=True, path=(_CSE_EMAIL,)
    ),
    SettingsVerb(
        "gmail_create_cse_identity",
        ("settings", "cse", "identities", "create"),
        "POST settings/cse/identities",
        "Gmail: Create CSE Identity",
        f"Create a client-side encryption identity that binds an email address to key pairs (users.settings.cse.identities.create). {_BASIC} {_WORKSPACE}",
        scope=GMAIL_SETTINGS_BASIC_SCOPE,
        workspace_only=True,
        body=(Field("email_address", "emailAddress", STR, "The address the identity is for (the user's primary address or an alias).", required=True), *_CSE_IDENTITY_FIELDS),
        has_body=True,
    ),
    SettingsVerb(
        "gmail_patch_cse_identity",
        ("settings", "cse", "identities", "patch"),
        "PATCH settings/cse/identities/{emailAddress}",
        "Gmail: Patch CSE Identity",
        f"Point a client-side encryption identity at different key pairs (users.settings.cse.identities.patch). {_BASIC} {_WORKSPACE}",
        scope=GMAIL_SETTINGS_BASIC_SCOPE,
        workspace_only=True,
        path=(Field("email_address", "emailAddress", STR, "The address of the identity to change.", required=True),),
        body=(Field("email_address", "emailAddress", STR, "The address of the identity to change."), *_CSE_IDENTITY_FIELDS),
        has_body=True,
    ),
    SettingsVerb(
        "gmail_delete_cse_identity",
        ("settings", "cse", "identities", "delete"),
        "DELETE settings/cse/identities/{cseEmailAddress}",
        "Gmail: Delete CSE Identity",
        f"Delete a client-side encryption identity; its key pairs are kept (users.settings.cse.identities.delete). {_BASIC} {_WORKSPACE}",
        scope=GMAIL_SETTINGS_BASIC_SCOPE,
        workspace_only=True,
        path=(_CSE_EMAIL,),
        empty_result="deleted",
    ),
    SettingsVerb(
        "gmail_list_cse_key_pairs",
        ("settings", "cse", "keypairs", "list"),
        "GET settings/cse/keypairs",
        "Gmail: List CSE Key Pairs",
        f"List the client-side encryption key pairs of the user (users.settings.cse.keypairs.list) with state and subject addresses, one page at a time. {_READ} {_WORKSPACE}",
        workspace_only=True,
        query=paging_fields(),
        list_key="cseKeyPairs",
        rows_name="cse_key_pairs",
        paged=True,
    ),
    SettingsVerb(
        "gmail_get_cse_key_pair",
        ("settings", "cse", "keypairs", "get"),
        "GET settings/cse/keypairs/{keyPairId}",
        "Gmail: Get CSE Key Pair",
        f"Read one client-side encryption key pair: public certificate, state, subject addresses and private key metadata (users.settings.cse.keypairs.get). {_READ} {_WORKSPACE}",
        workspace_only=True,
        path=(_KEY_PAIR_ID,),
    ),
    SettingsVerb(
        "gmail_create_cse_key_pair",
        ("settings", "cse", "keypairs", "create"),
        "POST settings/cse/keypairs",
        "Gmail: Create CSE Key Pair",
        f"Upload a client-side encryption key pair: the public certificate chain (PKCS#7) plus metadata for the wrapped private key held by a key service or hardware key (users.settings.cse.keypairs.create). {_BASIC} {_WORKSPACE}",
        scope=GMAIL_SETTINGS_BASIC_SCOPE,
        workspace_only=True,
        query=(Field("chain_validation", "chainValidation", STR, "Certificate validation: all (Google's default) checks the chain and revocation; none allows historical-decryption imports with invalid or revoked chains, which cannot be used in a CSE identity.", enum=("all", "none")),),
        body=(
            Field("pkcs7", "pkcs7", STR, "The public certificate chain in PKCS#7 PEM form.", required=True),
            Field("private_key_metadata", "privateKeyMetadata", OBJECT_LIST, "List of Gmail CsePrivateKeyMetadata objects, e.g. [{'kaclsKeyMetadata': {'kaclsUri': ..., 'kaclsData': ...}}] or [{'hardwareKeyMetadata': {'description': ...}}].", required=True),
        ),
        has_body=True,
    ),
    _key_pair_state("Enable", "enable", "gmail_enable_cse_key_pair", "enabled", "An enabled key pair can encrypt, decrypt and sign again."),
    _key_pair_state("Disable", "disable", "gmail_disable_cse_key_pair", "disabled", "A disabled key pair cannot encrypt new mail; it can be re-enabled, or obliterated 30 days after disabling."),
    _key_pair_state("Obliterate", "obliterate", "gmail_obliterate_cse_key_pair", "obliterated", "Permanent: mail encrypted with it can no longer be decrypted. Allowed only 30 days after the key pair was disabled."),
)

SHARING_VERBS: tuple[SettingsVerb, ...] = (*DELEGATE_VERBS, *SEND_AS_VERBS, *SMIME_VERBS, *CSE_VERBS)
