"""``@platform_process`` declarations for Google Calendar (mixed into ``GSuitePlugin``).

Thirty-eight EDGE verbs, one per Calendar API v3 method (see
:mod:`calendar_operations`). Each dispatches in milliseconds through the
plugin's D0.3 deferred-completion worker and runs exactly one vendor request.
Arguments are the vendor's own path and query parameters in snake case,
``body`` (the vendor request object) where the method takes one, and an
optional ``account``.

:meth:`CalendarProcesses._resolve_calendar_account` is the single place a
Calendar call learns which Google account it runs as, which scopes that
account granted and its tier; everything else takes the
:class:`~.calendar_operations.CalendarContext` it returns.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Any, Final, cast

from ananta.core.actions.action_metadata import (
    ContextHandling,
    MergeErrorProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
    platform_process,
)
from ananta.core.domain.enums import ProcessorPolicyCategory

from . import calendar_discovery, calendar_operations
from .account_processes import ACCOUNT_PARAM, ACTIVE_ACCOUNT, AccountProcesses, account_selector
from .accounts import AccountStore
from .calendar_operations import ACCOUNT_ARGUMENT, BODY_ARGUMENT, CalendarContext, CalendarError, CalendarOperation
from .constants import ERROR_INVALID_PARAMS
from .errors import classify_exception
from .oauth.service_factory import GoogleServiceFactory
from .oauth.token_store import TokenStore

QUEUED: Final[ReturnValueSchema] = ReturnValueSchema(
    type=ParameterType.OBJECT,
    description="Dispatch envelope — job_id + status: queued. The vendor response arrives when the job completes.",
    properties={
        "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
        "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
    },
)
_QUEUED_NOTE: Final[str] = " Runs as a background job: dispatch returns job_id/status queued at once; completion carries the vendor response (plus next_page_token/next_sync_token when Google returns them) and names the account used."
_WATCH_BODY: Final[str] = "Channel object: id (your unique channel id), type 'web_hook', address (an HTTPS URL you control), optional token, expiration (ms) and params.ttl."
_API_PARAMETER_NOTE: Final[str] = (
    " API-wide query arguments: alt (json), fields (partial-response selector), pretty_print (boolean), quota_user and user_ip (deprecated). Arguments key/access_token/oauth_token are declared only to refuse credential overrides: credentials belong to the Google account contract; select credentials with account."
)


def _spec(verb: str, display_name: str, description: str, body: str = "") -> dict[str, Any]:
    """``platform_process`` keyword arguments for one Calendar verb, typed from the vendor method it runs."""
    op = calendar_operations.operation(verb)
    parameters = calendar_discovery.parameter_metadata(op.resource, op.method)
    if op.takes_body:
        parameters[BODY_ARGUMENT] = ParameterMetadata(type=ParameterType.OBJECT, required=True, description=body)
    parameters[ACCOUNT_ARGUMENT] = ACCOUNT_PARAM
    return {
        "name": verb,
        "display_name": display_name,
        "description": f"{description} ({op.http_method} {op.path}; scope {op.scope.rsplit('/', 1)[1]}).{_QUEUED_NOTE}{_API_PARAMETER_NOTE}",
        "processor_policy_category": ProcessorPolicyCategory.EDGE,
        "is_async": True,
        "is_long_running": True,
        "parameters": parameters,
        "return_value_schema": QUEUED,
        "error_processor_customizations": MergeErrorProcessorCustomizations(retryable=op.retryable),
        "context_handling": ContextHandling.NONE,
    }


class CalendarProcesses:
    """Typed Calendar verbs. ``GSuitePlugin`` supplies the three hooks below."""

    def _run_deferred(self, params: dict[str, Any], state: dict[str, Any], verb: str) -> dict[str, Any]:
        raise NotImplementedError

    def _error(self, code: str, message: str) -> dict[str, Any]:
        raise NotImplementedError

    def _require_accounts(self) -> AccountStore:
        raise NotImplementedError

    def _require_factory(self) -> GoogleServiceFactory:
        raise NotImplementedError

    def _require_token_store(self) -> TokenStore:
        raise NotImplementedError

    # -- dispatch, worker and the account seam ----------------------------------

    def _call_as_account(self, verb: str, params: dict[str, Any], produce: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Calendar's exit also types account and job errors."""
        if verb not in calendar_operations.OPERATIONS_BY_VERB:
            return AccountProcesses._call_as_account(cast(AccountProcesses, self), verb, params, produce)  # noqa: SLF001
        return calendar_operations.outward_call(partial(self._run_as_account, verb, params, produce))

    def _run_as_account(self, verb: str, params: dict[str, Any], produce: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Bind the resolved account and add its fields to the vendor result."""
        accounts = self._require_accounts()
        resolved = accounts.resolve(account_selector(params), cast(AccountProcesses, self)._probe_store)  # noqa: SLF001
        email = resolved.record.email
        store = accounts.token_store(email)
        token = ACTIVE_ACCOUNT.set(email)
        try:
            data = produce()
        except CalendarError:
            raise
        except Exception as exc:  # noqa: BLE001 — classified here
            code, message = classify_exception(exc, verb, store.credential_facts()["granted_scopes"])
            raise CalendarError(code, message) from exc
        finally:
            ACTIVE_ACCOUNT.reset(token)
        result = {**data, "account": email}
        if resolved.note:
            result["account_note"] = resolved.note
        return result

    def _dispatch_calendar(self, verb: str, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return calendar_operations.outward_call(partial(self._calendar_dispatch_fields, verb, params, state))

    def _calendar_dispatch_fields(self, verb: str, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        try:
            calendar_operations.validate(calendar_operations.operation(verb), params)
        except ValueError as exc:
            return self._error(ERROR_INVALID_PARAMS, str(exc))
        return self._run_deferred(params, state, verb)

    def _calendar_producers(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        return {op.verb: partial(self._run_calendar, op) for op in calendar_operations.OPERATIONS}

    def _run_calendar(self, op: CalendarOperation, params: dict[str, Any]) -> dict[str, Any]:
        return calendar_operations.execute(op, params, self._resolve_calendar_account())

    def _resolve_calendar_account(self) -> CalendarContext:
        """Use the account already resolved by the plugin's inline/job wrapper.

        Unknown credential facts remain advisory; known grants and consumer tier
        constrain only the corresponding vendor operations. The account seam
        names the account; the Calendar exit scans every outcome once.
        """
        email = ACTIVE_ACCOUNT.get()
        store = self._require_accounts().token_store(email)
        service = self._require_factory().calendar()
        facts = store.credential_facts()
        granted = facts["granted_scopes"]
        return CalendarContext(
            service=service,
            account=email,
            granted_scopes=frozenset(granted) if granted is not None else None,
            tier=lambda: facts["account_tier"],
        )

    # -- calendarList -------------------------------------------------------------

    @platform_process(
        **_spec(
            "calendar_calendarlist_list",
            "Calendar: List My Calendars",
            "List the calendars on the account's calendar list (owned, subscribed and shared), one page per call with page_token, filtered by min_access_role, show_hidden, show_deleted and show_own_organization_only; sync_token returns only changes since the last full listing",
        )
    )
    def calendar_calendarlist_list(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_calendarlist_list", params, state)

    @platform_process(
        **_spec(
            "calendar_calendarlist_get",
            "Calendar: Get Calendar List Entry",
            "Read one calendar list entry: the account's own view of a calendar (summary override, colours, hidden/selected, default reminders, notification settings, access role); calendar_id 'primary' is the account's main calendar",
        )
    )
    def calendar_calendarlist_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_calendarlist_get", params, state)

    @platform_process(
        **_spec(
            "calendar_calendarlist_insert",
            "Calendar: Subscribe To Calendar",
            "Add an existing calendar to the account's calendar list (subscribe), with optional display overrides; color_rgb_format=true lets body set backgroundColor/foregroundColor",
            body="CalendarListEntry: id (the calendar id to add, required), optional summaryOverride, colorId or backgroundColor/foregroundColor, hidden, selected, defaultReminders, notificationSettings.",
        )
    )
    def calendar_calendarlist_insert(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_calendarlist_insert", params, state)

    @platform_process(
        **_spec(
            "calendar_calendarlist_update",
            "Calendar: Replace Calendar List Entry",
            "Replace the account's settings for one calendar list entry (full update: omitted writable fields are cleared)",
            body="Full CalendarListEntry: summaryOverride, colorId or backgroundColor/foregroundColor, hidden, selected, defaultReminders, notificationSettings.",
        )
    )
    def calendar_calendarlist_update(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_calendarlist_update", params, state)

    @platform_process(
        **_spec(
            "calendar_calendarlist_patch",
            "Calendar: Change Calendar List Entry",
            "Change selected fields of the account's settings for one calendar list entry (patch: only the fields in body change; an explicit null clears one)",
            body="Partial CalendarListEntry with only the fields to change, e.g. {'summaryOverride': 'Team', 'hidden': false}.",
        )
    )
    def calendar_calendarlist_patch(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_calendarlist_patch", params, state)

    @platform_process(
        **_spec(
            "calendar_calendarlist_delete",
            "Calendar: Unsubscribe From Calendar",
            "Remove a calendar from the account's calendar list (unsubscribe); the calendar itself and its events are not deleted",
        )
    )
    def calendar_calendarlist_delete(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_calendarlist_delete", params, state)

    @platform_process(
        **_spec(
            "calendar_calendarlist_watch",
            "Calendar: Watch Calendar List",
            "Open a push-notification channel that tells an HTTPS address you control when the account's calendar list changes; stop it with calendar_channels_stop",
            body=_WATCH_BODY,
        )
    )
    def calendar_calendarlist_watch(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_calendarlist_watch", params, state)

    # -- calendars ----------------------------------------------------------------

    @platform_process(
        **_spec(
            "calendar_calendars_get",
            "Calendar: Get Calendar",
            "Read a calendar's own metadata: summary, description, location, time zone, conference properties, working hours; calendar_id 'primary' is the account's main calendar",
        )
    )
    def calendar_calendars_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_calendars_get", params, state)

    @platform_process(
        **_spec(
            "calendar_calendars_insert",
            "Calendar: Create Calendar",
            "Create a new secondary calendar owned by the account; it is added to the account's calendar list",
            body="Calendar: summary (required), optional description, location, timeZone (IANA, e.g. 'Europe/Zurich'), conferenceProperties.",
        )
    )
    def calendar_calendars_insert(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_calendars_insert", params, state)

    @platform_process(
        **_spec(
            "calendar_calendars_update",
            "Calendar: Replace Calendar",
            "Replace a calendar's metadata (full update: omitted writable fields are cleared)",
            body="Full Calendar: summary (required), description, location, timeZone, conferenceProperties.",
        )
    )
    def calendar_calendars_update(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_calendars_update", params, state)

    @platform_process(
        **_spec(
            "calendar_calendars_patch",
            "Calendar: Change Calendar",
            "Change selected metadata fields of a calendar (patch: only the fields in body change)",
            body="Partial Calendar with only the fields to change, e.g. {'description': 'On-call rota'}.",
        )
    )
    def calendar_calendars_patch(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_calendars_patch", params, state)

    @platform_process(
        **_spec(
            "calendar_calendars_delete",
            "Calendar: Delete Calendar",
            "Permanently delete a secondary calendar the account owns, with all its events; the primary calendar cannot be deleted (use calendar_calendars_clear)",
        )
    )
    def calendar_calendars_delete(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_calendars_delete", params, state)

    @platform_process(
        **_spec(
            "calendar_calendars_clear",
            "Calendar: Clear Primary Calendar",
            "Delete every event on the account's primary calendar (calendar_id 'primary'); the calendar itself stays; this cannot be undone",
        )
    )
    def calendar_calendars_clear(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_calendars_clear", params, state)

    @platform_process(
        **_spec(
            "calendar_calendars_transfer_ownership",
            "Calendar: Transfer Calendar Ownership",
            "Transfer an active secondary calendar to another user of the same Google Workspace organization (new_data_owner); requires a Workspace administrator with the Manage Calendars privilege and use_admin_access=true; a consumer account is refused with gsuite.workspace_only",
        )
    )
    def calendar_calendars_transfer_ownership(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_calendars_transfer_ownership", params, state)

    # -- events -------------------------------------------------------------------

    @platform_process(
        **_spec(
            "calendar_events_list",
            "Calendar: List Events",
            "List events on one calendar, one page per call (max_results up to 2500, page_token to continue): free-text q, time window time_min/time_max (RFC 3339), single_events to expand recurrences, order_by, event_types, i_cal_uid, extended-property filters; sync_token returns only changes since the last full listing, and a 410 means resync",
        )
    )
    def calendar_events_list(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_events_list", params, state)

    @platform_process(
        **_spec(
            "calendar_events_get",
            "Calendar: Get Event",
            "Read one event by id: times, attendees and their responses, organizer, recurrence, conference data (Meet link), reminders, attachments and extended properties",
        )
    )
    def calendar_events_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_events_get", params, state)

    @platform_process(
        **_spec(
            "calendar_events_instances",
            "Calendar: List Recurring Event Instances",
            "List the individual occurrences of one recurring event, one page per call, optionally limited to a time window or to the occurrence starting at original_start",
        )
    )
    def calendar_events_instances(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_events_instances", params, state)

    @platform_process(
        **_spec(
            "calendar_events_insert",
            "Calendar: Create Event",
            "Create an event: attendees (send_updates all/externalOnly/none decides who Google emails), recurrence rules, reminders, attachments (supports_attachments) and a Google Meet link via conferenceData.createRequest with conference_data_version=1",
            body="Event: start and end ({dateTime, timeZone} or all-day {date}), summary, description, location, attendees [{email, optional}], recurrence ['RRULE:FREQ=WEEKLY;COUNT=5'], reminders, conferenceData {createRequest: {requestId, conferenceSolutionKey: {type: 'hangoutsMeet'}}}, eventType.",
        )
    )
    def calendar_events_insert(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_events_insert", params, state)

    @platform_process(
        **_spec(
            "calendar_events_import",
            "Calendar: Import Event",
            "Import a private copy of an existing event (identified by its iCalUID) into a calendar, as when copying an invitation from another system; no invitations are sent",
            body="Event with iCalUID (required), start, end and the other Event fields to import; organizer and attendees are kept as data.",
        )
    )
    def calendar_events_import(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_events_import", params, state)

    @platform_process(
        **_spec(
            "calendar_events_quick_add",
            "Calendar: Quick-Add Event",
            "Create an event from one line of text that Google parses, e.g. 'Lunch with Ana at Cafe Roma tomorrow 12:30-13:30'",
        )
    )
    def calendar_events_quick_add(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_events_quick_add", params, state)

    @platform_process(
        **_spec(
            "calendar_events_update",
            "Calendar: Replace Event",
            "Replace an event (full update: omitted writable fields are cleared, so start from calendar_events_get); send_updates decides who is notified",
            body="Full Event: start, end and every field to keep (summary, attendees, recurrence, reminders, conferenceData, ...).",
        )
    )
    def calendar_events_update(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_events_update", params, state)

    @platform_process(
        **_spec(
            "calendar_events_patch",
            "Calendar: Change Event",
            "Change selected fields of an event (patch: only the fields in body change, e.g. reschedule, rename, add an attendee); send_updates decides who is notified",
            body="Partial Event with only the fields to change, e.g. {'start': {...}, 'end': {...}}; an array field such as attendees is replaced whole.",
        )
    )
    def calendar_events_patch(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_events_patch", params, state)

    @platform_process(
        **_spec(
            "calendar_events_delete",
            "Calendar: Delete Event",
            "Delete an event (or cancel the meeting, for its organizer); send_updates decides whether attendees are told",
        )
    )
    def calendar_events_delete(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_events_delete", params, state)

    @platform_process(
        **_spec(
            "calendar_events_move",
            "Calendar: Move Event",
            "Move an event to another calendar (destination), changing its organizer calendar; only default events can be moved",
        )
    )
    def calendar_events_move(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_events_move", params, state)

    @platform_process(
        **_spec(
            "calendar_events_watch",
            "Calendar: Watch Events",
            "Open a push-notification channel that tells an HTTPS address you control when events on one calendar change; stop it with calendar_channels_stop",
            body=_WATCH_BODY,
        )
    )
    def calendar_events_watch(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_events_watch", params, state)

    # -- freebusy -----------------------------------------------------------------

    @platform_process(
        **_spec(
            "calendar_freebusy_query",
            "Calendar: Query Free/Busy",
            "Return the busy intervals of one or more calendars or groups between two times, to find a meeting slot without reading event details",
            body="FreeBusyRequest: timeMin and timeMax (RFC 3339, required), items [{id: calendar id or email}], optional timeZone, groupExpansionMax, calendarExpansionMax.",
        )
    )
    def calendar_freebusy_query(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_freebusy_query", params, state)

    # -- acl ----------------------------------------------------------------------

    @platform_process(
        **_spec(
            "calendar_acl_list",
            "Calendar: List Sharing Rules",
            "List a calendar's sharing (access-control) rules, one page per call; sync_token returns only changes since the last full listing",
        )
    )
    def calendar_acl_list(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_acl_list", params, state)

    @platform_process(
        **_spec(
            "calendar_acl_get",
            "Calendar: Get Sharing Rule",
            "Read one sharing rule of a calendar by rule id: who (scope) has which role",
        )
    )
    def calendar_acl_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_acl_get", params, state)

    @platform_process(
        **_spec(
            "calendar_acl_insert",
            "Calendar: Share Calendar",
            "Share a calendar by creating a sharing rule: a role (freeBusyReader, reader, writer, owner) for a user, group, domain or everyone (default); send_notifications controls the email; a domain rule needs a Google Workspace account",
            body="AclRule: role and scope {type: user|group|domain|default, value: email or domain}.",
        )
    )
    def calendar_acl_insert(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_acl_insert", params, state)

    @platform_process(
        **_spec(
            "calendar_acl_update",
            "Calendar: Replace Sharing Rule",
            "Replace one sharing rule of a calendar (full update of role and scope)",
            body="Full AclRule: role and scope {type, value}.",
        )
    )
    def calendar_acl_update(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_acl_update", params, state)

    @platform_process(
        **_spec(
            "calendar_acl_patch",
            "Calendar: Change Sharing Rule",
            "Change the role of one sharing rule of a calendar (patch)",
            body="Partial AclRule, e.g. {'role': 'reader'}.",
        )
    )
    def calendar_acl_patch(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_acl_patch", params, state)

    @platform_process(
        **_spec(
            "calendar_acl_delete",
            "Calendar: Remove Sharing Rule",
            "Stop sharing a calendar with someone by deleting one sharing rule",
        )
    )
    def calendar_acl_delete(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_acl_delete", params, state)

    @platform_process(
        **_spec(
            "calendar_acl_watch",
            "Calendar: Watch Sharing Rules",
            "Open a push-notification channel that tells an HTTPS address you control when a calendar's sharing rules change; stop it with calendar_channels_stop",
            body=_WATCH_BODY,
        )
    )
    def calendar_acl_watch(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_acl_watch", params, state)

    # -- settings, colors, channels --------------------------------------------------

    @platform_process(
        **_spec(
            "calendar_settings_list",
            "Calendar: List My Calendar Settings",
            "List the account's Calendar user settings (time zone, locale, week start, default event length, hide weekends, format options); read-only, as Google offers no settings write",
        )
    )
    def calendar_settings_list(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_settings_list", params, state)

    @platform_process(
        **_spec(
            "calendar_settings_get",
            "Calendar: Get Calendar Setting",
            "Read one Calendar user setting by id, e.g. timezone, locale, weekStart, defaultEventLength, format24HourTime",
        )
    )
    def calendar_settings_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_settings_get", params, state)

    @platform_process(
        **_spec(
            "calendar_settings_watch",
            "Calendar: Watch Calendar Settings",
            "Open a push-notification channel that tells an HTTPS address you control when the account's Calendar settings change; stop it with calendar_channels_stop",
            body=_WATCH_BODY,
        )
    )
    def calendar_settings_watch(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_settings_watch", params, state)

    @platform_process(
        **_spec(
            "calendar_colors_get",
            "Calendar: Get Colour Palette",
            "Read Google Calendar's colour palette: the colorId values usable for calendars and events, with their background and foreground hex colours",
        )
    )
    def calendar_colors_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_colors_get", params, state)

    @platform_process(
        **_spec(
            "calendar_channels_stop",
            "Calendar: Stop Watch Channel",
            "Stop a push-notification channel opened by any Calendar watch verb, so Google stops calling its address",
            body="Channel to stop: id and resourceId exactly as the watch response returned them.",
        )
    )
    def calendar_channels_stop(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_calendar("calendar_channels_stop", params, state)
