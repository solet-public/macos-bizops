"""Typed People methods from Google's discovery revision 20261106.

Each process has explicit named, typed vendor parameters and runs through the
shared account-aware deferred producer. See workspace_registry for schemas.
"""

from __future__ import annotations

from typing import Any

from ananta.core.actions.action_metadata import ContextHandling, MergeErrorProcessorCustomizations, ParameterType, ReturnValueSchema, platform_process
from ananta.core.domain.enums import ProcessorPolicyCategory

from .workspace_registry import process_parameters


class PeopleAPIProcesses:
    """Complete People API; the plugin supplies the deferred dispatcher."""

    def _run_deferred(self, params: dict[str, Any], state: dict[str, Any], verb: str) -> dict[str, Any]:
        raise NotImplementedError

    @platform_process(
        name="people_contact_groups_batch_get",
        display_name="People Contact Groups Batch Get",
        description="people.contactGroups.batchGet: GET v1/contactGroups:batchGet. Get a list of contact groups owned by the authenticated user by specifying a list of contact group resource names. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("people_contact_groups_batch_get"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def people_contact_groups_batch_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "people_contact_groups_batch_get")

    @platform_process(
        name="people_contact_groups_create",
        display_name="People Contact Groups Create",
        description="people.contactGroups.create: POST v1/contactGroups. Create a new contact group owned by the authenticated user. Created contact group names must be unique to the users contact groups. Attempting to create a group with a duplicate name will return a HTTP 409 error. Mutate requests for the same user should be sent sequentially to avoid increased latency and failures. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("people_contact_groups_create"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def people_contact_groups_create(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "people_contact_groups_create")

    @platform_process(
        name="people_contact_groups_delete",
        display_name="People Contact Groups Delete",
        description="people.contactGroups.delete: DELETE v1/{+resourceName}. Delete an existing contact group owned by the authenticated user by specifying a contact group resource name. Mutate requests for the same user should be sent sequentially to avoid increased latency and failures. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("people_contact_groups_delete"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def people_contact_groups_delete(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "people_contact_groups_delete")

    @platform_process(
        name="people_contact_groups_get",
        display_name="People Contact Groups Get",
        description="people.contactGroups.get: GET v1/{+resourceName}. Get a specific contact group owned by the authenticated user by specifying a contact group resource name. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("people_contact_groups_get"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def people_contact_groups_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "people_contact_groups_get")

    @platform_process(
        name="people_contact_groups_list",
        display_name="People Contact Groups List",
        description="people.contactGroups.list: GET v1/contactGroups. List all contact groups owned by the authenticated user. Members of the contact groups are not populated. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("people_contact_groups_list"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def people_contact_groups_list(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "people_contact_groups_list")

    @platform_process(
        name="people_contact_groups_members_modify",
        display_name="People Contact Groups Members Modify",
        description="people.contactGroups.members.modify: POST v1/{+resourceName}/members:modify. Modify the members of a contact group owned by the authenticated user. The only system contact groups that can have members added are `contactGroups/myContacts` and `contactGroups/starred`. Other system contact groups are deprecated and can only have contacts removed. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("people_contact_groups_members_modify"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def people_contact_groups_members_modify(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "people_contact_groups_members_modify")

    @platform_process(
        name="people_contact_groups_update",
        display_name="People Contact Groups Update",
        description="people.contactGroups.update: PUT v1/{+resourceName}. Update the name of an existing contact group owned by the authenticated user. Updated contact group names must be unique to the users contact groups. Attempting to create a group with a duplicate name will return a HTTP 409 error. Mutate requests for the same user should be sent sequentially to avoid increased latency and failures. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("people_contact_groups_update"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def people_contact_groups_update(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "people_contact_groups_update")

    @platform_process(
        name="people_other_contacts_copy_other_contact_to_my_contacts_group",
        display_name="People Other Contacts Copy Other Contact To My Contacts Group",
        description='people.otherContacts.copyOtherContactToMyContactsGroup: POST v1/{+resourceName}:copyOtherContactToMyContactsGroup. Copies an "Other contact" to a new contact in the user\'s "myContacts" group Mutate requests for the same user should be sent sequentially to avoid increased latency and failures. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.',
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("people_other_contacts_copy_other_contact_to_my_contacts_group"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def people_other_contacts_copy_other_contact_to_my_contacts_group(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "people_other_contacts_copy_other_contact_to_my_contacts_group")

    @platform_process(
        name="people_other_contacts_list",
        display_name="People Other Contacts List",
        description='people.otherContacts.list: GET v1/otherContacts. List all "Other contacts", that is contacts that are not in a contact group. "Other contacts" are typically auto created contacts from interactions. Sync tokens expire 7 days after the full sync. A request with an expired sync token will get an error with an [google.rpc.ErrorInfo](https://cloud.google.com/apis/design/errors#error_info) with reason "EXPIRED_SYNC_TOKEN". In the case of such an error clients should make a full sync request without a `sync_token`. The first page of a full sync request has an additional quota. If the quota is exceeded, a 429 error will be returned. This quota is fixed and can not be increased. When the `sync_token` is specified, resources deleted since the last sync will be returned as a person with `PersonMetadata.deleted` set to true. When the `page_token` or `sync_token` is specified, all other request parameters must match the first call. Writes may have a propagation delay of several minutes for sync requests. Incremental syncs are not intended for read-after-write use cases. See example usage at [List the user\'s other contacts that have changed](/people/v1/other-contacts#list_the_users_other_contacts_that_have_changed). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.',
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("people_other_contacts_list"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def people_other_contacts_list(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "people_other_contacts_list")

    @platform_process(
        name="people_other_contacts_search",
        display_name="People Other Contacts Search",
        description="people.otherContacts.search: GET v1/otherContacts:search. Provides a list of contacts in the authenticated user's other contacts that matches the search query. The query matches on a contact's `names`, `emailAddresses`, and `phoneNumbers` fields that are from the OTHER_CONTACT source. **IMPORTANT**: Before searching, clients should send a warmup request with an empty query to update the cache. See https://developers.google.com/people/v1/other-contacts#search_the_users_other_contacts Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("people_other_contacts_search"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def people_other_contacts_search(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "people_other_contacts_search")

    @platform_process(
        name="people_people_batch_create_contacts",
        display_name="People People Batch Create Contacts",
        description="people.people.batchCreateContacts: POST v1/people:batchCreateContacts. Create a batch of new contacts and return the PersonResponses for the newly Mutate requests for the same user should be sent sequentially to avoid increased latency and failures. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("people_people_batch_create_contacts"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def people_people_batch_create_contacts(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "people_people_batch_create_contacts")

    @platform_process(
        name="people_people_batch_delete_contacts",
        display_name="People People Batch Delete Contacts",
        description="people.people.batchDeleteContacts: POST v1/people:batchDeleteContacts. Delete a batch of contacts. Any non-contact data will not be deleted. Mutate requests for the same user should be sent sequentially to avoid increased latency and failures. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("people_people_batch_delete_contacts"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def people_people_batch_delete_contacts(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "people_people_batch_delete_contacts")

    @platform_process(
        name="people_people_batch_update_contacts",
        display_name="People People Batch Update Contacts",
        description="people.people.batchUpdateContacts: POST v1/people:batchUpdateContacts. Update a batch of contacts and return a map of resource names to PersonResponses for the updated contacts. Mutate requests for the same user should be sent sequentially to avoid increased latency and failures. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("people_people_batch_update_contacts"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def people_people_batch_update_contacts(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "people_people_batch_update_contacts")

    @platform_process(
        name="people_people_connections_list",
        display_name="People People Connections List",
        description="people.people.connections.list: GET v1/{+resourceName}/connections. Provides a list of the authenticated user's contacts. Sync tokens expire 7 days after the full sync. A request with an expired sync token will get an error with an [google.rpc.ErrorInfo](https://cloud.google.com/apis/design/errors#error_info) with reason \"EXPIRED_SYNC_TOKEN\". In the case of such an error clients should make a full sync request without a `sync_token`. The first page of a full sync request has an additional quota. If the quota is exceeded, a 429 error will be returned. This quota is fixed and can not be increased. When the `sync_token` is specified, resources deleted since the last sync will be returned as a person with `PersonMetadata.deleted` set to true. When the `page_token` or `sync_token` is specified, all other request parameters must match the first call. Writes may have a propagation delay of several minutes for sync requests. Incremental syncs are not intended for read-after-write use cases. See example usage at [List the user's contacts that have changed](/people/v1/contacts#list_the_users_contacts_that_have_changed). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("people_people_connections_list"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def people_people_connections_list(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "people_people_connections_list")

    @platform_process(
        name="people_people_create_contact",
        display_name="People People Create Contact",
        description="people.people.createContact: POST v1/people:createContact. Create a new contact and return the person resource for that contact. The request returns a 400 error if more than one field is specified on a field that is a singleton for contact sources: * biographies * birthdays * genders * names Mutate requests for the same user should be sent sequentially to avoid increased latency and failures. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("people_people_create_contact"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def people_people_create_contact(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "people_people_create_contact")

    @platform_process(
        name="people_people_delete_contact",
        display_name="People People Delete Contact",
        description="people.people.deleteContact: DELETE v1/{+resourceName}:deleteContact. Delete a contact person. Any non-contact data will not be deleted. Mutate requests for the same user should be sent sequentially to avoid increased latency and failures. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("people_people_delete_contact"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def people_people_delete_contact(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "people_people_delete_contact")

    @platform_process(
        name="people_people_delete_contact_photo",
        display_name="People People Delete Contact Photo",
        description="people.people.deleteContactPhoto: DELETE v1/{+resourceName}:deleteContactPhoto. Delete a contact's photo. Mutate requests for the same user should be done sequentially to avoid // lock contention. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("people_people_delete_contact_photo"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def people_people_delete_contact_photo(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "people_people_delete_contact_photo")

    @platform_process(
        name="people_people_get",
        display_name="People People Get",
        description="people.people.get: GET v1/{+resourceName}. Provides information about a person by specifying a resource name. Use `people/me` to indicate the authenticated user. The request returns a 400 error if 'personFields' is not specified. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("people_people_get"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def people_people_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "people_people_get")

    @platform_process(
        name="people_people_get_batch_get",
        display_name="People People Get Batch Get",
        description="people.people.getBatchGet: GET v1/people:batchGet. Provides information about a list of specific people by specifying a list of requested resource names. Use `people/me` to indicate the authenticated user. The request returns a 400 error if 'personFields' is not specified. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("people_people_get_batch_get"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def people_people_get_batch_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "people_people_get_batch_get")

    @platform_process(
        name="people_people_list_directory_people",
        display_name="People People List Directory People",
        description="people.people.listDirectoryPeople: GET v1/people:listDirectoryPeople. Provides a list of domain profiles and domain contacts in the authenticated user's domain directory. When the `sync_token` is specified, resources deleted since the last sync will be returned as a person with `PersonMetadata.deleted` set to true. When the `page_token` or `sync_token` is specified, all other request parameters must match the first call. Writes may have a propagation delay of several minutes for sync requests. Incremental syncs are not intended for read-after-write use cases. See example usage at [List the directory people that have changed](/people/v1/directory#list_the_directory_people_that_have_changed). Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("people_people_list_directory_people"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def people_people_list_directory_people(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "people_people_list_directory_people")

    @platform_process(
        name="people_people_search_contacts",
        display_name="People People Search Contacts",
        description="people.people.searchContacts: GET v1/people:searchContacts. Provides a list of contacts in the authenticated user's grouped contacts that matches the search query. The query matches on a contact's `names`, `nickNames`, `emailAddresses`, `phoneNumbers`, and `organizations` fields that are from the CONTACT source. **IMPORTANT**: Before searching, clients should send a warmup request with an empty query to update the cache. See https://developers.google.com/people/v1/contacts#search_the_users_contacts Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("people_people_search_contacts"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def people_people_search_contacts(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "people_people_search_contacts")

    @platform_process(
        name="people_people_search_directory_people",
        display_name="People People Search Directory People",
        description="people.people.searchDirectoryPeople: GET v1/people:searchDirectoryPeople. Provides a list of domain profiles and domain contacts in the authenticated user's domain directory that match the search query. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("people_people_search_directory_people"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def people_people_search_directory_people(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "people_people_search_directory_people")

    @platform_process(
        name="people_people_update_contact",
        display_name="People People Update Contact",
        description='people.people.updateContact: PATCH v1/{+resourceName}:updateContact. Update contact data for an existing contact person. Any non-contact data will not be modified. Any non-contact data in the person to update will be ignored. All fields specified in the `update_mask` will be replaced. The server returns a 400 error if `person.metadata.sources` is not specified for the contact to be updated or if there is no contact source. The server returns a 400 error with reason `"failedPrecondition"` if `person.metadata.sources.etag` is different than the contact\'s etag, which indicates the contact has changed since its data was read. Clients should get the latest person and merge their updates into the latest person. If making sequential updates to the same person, the etag from the `updateContact` response should be used to avoid failures. The server returns a 400 error if `memberships` are being updated and there are no contact group memberships specified on the person. The server returns a 400 error if more than one field is specified on a field that is a singleton for contact sources: * biographies * birthdays * genders * names Mutate requests for the same user should be sent sequentially to avoid increased latency and failures. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.',
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("people_people_update_contact"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def people_people_update_contact(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "people_people_update_contact")

    @platform_process(
        name="people_people_update_contact_photo",
        display_name="People People Update Contact Photo",
        description="people.people.updateContactPhoto: PATCH v1/{+resourceName}:updateContactPhoto. Update a contact's photo. Mutate requests for the same user should be sent sequentially to avoid increased latency and failures. Runs as a background job; completion preserves vendor response fields and names the selected account. Pass pageToken for the next page.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters=process_parameters("people_people_update_contact_photo"),
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Queued job_id and status; completion carries response and account, or a file_blob_key for media."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def people_people_update_contact_photo(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "people_people_update_contact_photo")
