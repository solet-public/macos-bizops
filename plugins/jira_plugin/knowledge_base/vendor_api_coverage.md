# Jira vendor API coverage

Article Layer: 2

Article Role: plugin_reference

Article Tags: domain:jira, evidence-category:api-coverage

Embedding Description: Inventory of Jira Cloud platform, Software Agile, and Service Management API families, current typed coverage, explicit gaps and the next numbered builds, including multi-site API-token and OAuth credentials and the later Confluence sibling.

## Scope and evidence

Surveyed 2026-11-07 against the vendor [platform v3 index](https://developer.atlassian.com/cloud/jira/platform/rest/v3/intro/), [Software index](https://developer.atlassian.com/cloud/jira/software/rest/intro/), and [Service Management index](https://developer.atlassian.com/cloud/jira/service-desk/rest/intro/). API permissions and scopes granted by Atlassian determine access, including writes and deletes. An available API is a gap until implemented; no gap is labelled no-API merely because this plugin lacks a verb.

Build 1 delivers the bindings and typed operation inventory below; live commissioning remains unverified. Existing issue/comment/attachment verbs are a partial v2 surface, not full v3 coverage. Complete new typed operation families: Projects, Board, Sprint, Backlog, Project versions and Issue worklogs. Every other family remains an explicit next-build gap.

| API | Family | Current coverage | Gap and next verb | Build |
|---|---|---|---|---|
| REST v3 | Agents | Not yet implemented | Complete `agents` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Announcement banner | Not yet implemented | Complete `announcement_banner` lifecycle, all documented operations and parameters | 3 |
| REST v3 | App data policies | Not yet implemented | Complete `app_data_policies` lifecycle, all documented operations and parameters | 3 |
| REST v3 | App migration | Not yet implemented | Complete `app_migration` lifecycle, all documented operations and parameters | 3 |
| REST v3 | App properties | Not yet implemented | Complete `app_properties` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Application roles | Not yet implemented | Complete `application_roles` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Audit records | Not yet implemented | Complete `audit_records` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Avatars | Not yet implemented | Complete `avatars` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Classification levels | Not yet implemented | Complete `classification_levels` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Dashboards | Not yet implemented | Complete `dashboards` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Dynamic modules | Not yet implemented | Complete `dynamic_modules` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Field schemes | Not yet implemented | Complete `field_schemes` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Filter sharing | Not yet implemented | Complete `filter_sharing` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Filters | Not yet implemented | Complete `filters` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Group and user picker | Not yet implemented | Complete `group_and_user_picker` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Groups | Not yet implemented | Complete `groups` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Issue attachments | Partial legacy verbs; v3 gap | Complete `issue_attachments` lifecycle, all documented operations and parameters | 2 |
| REST v3 | Issue bulk operations | Not yet implemented | Complete `issue_bulk_operations` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Issue comment properties | Not yet implemented | Complete `issue_comment_properties` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Issue comments | Partial legacy verbs; v3 gap | Complete `issue_comments` lifecycle, all documented operations and parameters | 2 |
| REST v3 | Issue custom field associations | Not yet implemented | Complete `issue_custom_field_associations` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Issue custom field configuration (apps) | Not yet implemented | Complete `issue_custom_field_configuration_(apps)` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Issue custom field contexts | Not yet implemented | Complete `issue_custom_field_contexts` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Issue custom field options | Not yet implemented | Complete `issue_custom_field_options` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Issue custom field options (apps) | Not yet implemented | Complete `issue_custom_field_options_(apps)` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Issue custom field values (apps) | Not yet implemented | Complete `issue_custom_field_values_(apps)` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Issue field configurations | Not yet implemented | Complete `issue_field_configurations` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Issue fields | Not yet implemented | Complete `issue_fields` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Issue link types | Not yet implemented | Complete `issue_link_types` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Issue links | Not yet implemented | Complete `issue_links` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Issue navigator settings | Not yet implemented | Complete `issue_navigator_settings` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Issue notification schemes | Not yet implemented | Complete `issue_notification_schemes` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Issue panels | Not yet implemented | Complete `issue_panels` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Issue priorities | Not yet implemented | Complete `issue_priorities` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Issue properties | Not yet implemented | Complete `issue_properties` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Issue redaction | Not yet implemented | Complete `issue_redaction` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Issue remote links | Not yet implemented | Complete `issue_remote_links` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Issue resolutions | Not yet implemented | Complete `issue_resolutions` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Issue search | Partial legacy verbs; v3 gap | Complete `issue_search` lifecycle, all documented operations and parameters | 2 |
| REST v3 | Issue security level | Not yet implemented | Complete `issue_security_level` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Issue security schemes | Not yet implemented | Complete `issue_security_schemes` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Issue type properties | Not yet implemented | Complete `issue_type_properties` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Issue type schemes | Not yet implemented | Complete `issue_type_schemes` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Issue type screen schemes | Not yet implemented | Complete `issue_type_screen_schemes` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Issue types | Not yet implemented | Complete `issue_types` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Issue votes | Not yet implemented | Complete `issue_votes` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Issue watchers | Not yet implemented | Complete `issue_watchers` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Issue worklog properties | Not yet implemented | Complete `issue_worklog_properties` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Issue worklogs | `worklogs`: all 10 documented operations, including writes/deletes where provided | Live commissioning; other product families remain later builds | 1 |
| REST v3 | Issues | Partial legacy verbs; v3 gap | Complete `issues` lifecycle, all documented operations and parameters | 2 |
| REST v3 | JQL | Not yet implemented | Complete `jql` lifecycle, all documented operations and parameters | 3 |
| REST v3 | JQL functions (apps) | Not yet implemented | Complete `jql_functions_(apps)` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Jira expressions | Not yet implemented | Complete `jira_expressions` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Jira settings | Not yet implemented | Complete `jira_settings` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Labels | Not yet implemented | Complete `labels` lifecycle, all documented operations and parameters | 3 |
| REST v3 | License metrics | Not yet implemented | Complete `license_metrics` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Migration of Connect modules to Forge | Not yet implemented | Complete `migration_of_connect_modules_to_forge` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Myself | Not yet implemented | Complete `myself` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Permission schemes | Not yet implemented | Complete `permission_schemes` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Permissions | Not yet implemented | Complete `permissions` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Plans | Not yet implemented | Complete `plans` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Policies | Not yet implemented | Complete `policies` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Priority schemes | Not yet implemented | Complete `priority_schemes` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Project avatars | Not yet implemented | Complete `project_avatars` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Project categories | Not yet implemented | Complete `project_categories` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Project classification levels | Not yet implemented | Complete `project_classification_levels` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Project components | Not yet implemented | Complete `project_components` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Project email | Not yet implemented | Complete `project_email` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Project features | Not yet implemented | Complete `project_features` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Project key and name validation | Not yet implemented | Complete `project_key_and_name_validation` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Project permission schemes | Not yet implemented | Complete `project_permission_schemes` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Project properties | Not yet implemented | Complete `project_properties` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Project role actors | Not yet implemented | Complete `project_role_actors` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Project roles | Not yet implemented | Complete `project_roles` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Project templates | Not yet implemented | Complete `project_templates` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Project types | Not yet implemented | Complete `project_types` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Project versions | `versions`: all 15 documented operations, including writes/deletes where provided | Live commissioning; other product families remain later builds | 1 |
| REST v3 | Projects | `projects`: all 13 documented operations, including writes/deletes where provided | Live commissioning; other product families remain later builds | 1 |
| REST v3 | Screen schemes | Not yet implemented | Complete `screen_schemes` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Screen tab fields | Not yet implemented | Complete `screen_tab_fields` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Screen tabs | Not yet implemented | Complete `screen_tabs` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Screens | Not yet implemented | Complete `screens` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Server info | Not yet implemented | Complete `server_info` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Service Registry | Not yet implemented | Complete `service_registry` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Status | Not yet implemented | Complete `status` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Tasks | Not yet implemented | Complete `tasks` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Teams in plan | Not yet implemented | Complete `teams_in_plan` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Time tracking | Not yet implemented | Complete `time_tracking` lifecycle, all documented operations and parameters | 3 |
| REST v3 | UI modifications (apps) | Not yet implemented | Complete `ui_modifications_(apps)` lifecycle, all documented operations and parameters | 3 |
| REST v3 | User properties | Not yet implemented | Complete `user_properties` lifecycle, all documented operations and parameters | 3 |
| REST v3 | User search | Not yet implemented | Complete `user_search` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Users | Not yet implemented | Complete `users` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Webhooks | Not yet implemented | Complete `webhooks` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Workflow scheme drafts | Not yet implemented | Complete `workflow_scheme_drafts` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Workflow scheme project associations | Not yet implemented | Complete `workflow_scheme_project_associations` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Workflow schemes | Not yet implemented | Complete `workflow_schemes` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Workflow status categories | Not yet implemented | Complete `workflow_status_categories` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Workflow statuses | Not yet implemented | Complete `workflow_statuses` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Workflow transition rules | Not yet implemented | Complete `workflow_transition_rules` lifecycle, all documented operations and parameters | 3 |
| REST v3 | Workflows | Not yet implemented | Complete `workflows` lifecycle, all documented operations and parameters | 3 |
| Software | Backlog | `backlog`: all 2 documented operations, including writes/deletes where provided | Live commissioning; other product families remain later builds | 1 |
| Software | Board | `boards`: all 33 documented operations, including writes/deletes where provided | Live commissioning; other product families remain later builds | 1 |
| Software | Builds | Not yet implemented | Complete `builds` lifecycle, all documented operations and parameters | 3 |
| Software | Deployments | Not yet implemented | Complete `deployments` lifecycle, all documented operations and parameters | 3 |
| Software | DevOps Components | Not yet implemented | Complete `devops_components` lifecycle, all documented operations and parameters | 3 |
| Software | Development Information | Not yet implemented | Complete `development_information` lifecycle, all documented operations and parameters | 3 |
| Software | Epic | Not yet implemented | Complete `epic` lifecycle, all documented operations and parameters | 3 |
| Software | Feature Flags | Not yet implemented | Complete `feature_flags` lifecycle, all documented operations and parameters | 3 |
| Software | Issue | `agile_issues`: all 4 documented operations, including writes/deletes where provided | Live commissioning; other product families remain later builds | 1 |
| Software | Operations | Not yet implemented | Complete `operations` lifecycle, all documented operations and parameters | 3 |
| Software | Remote Links | Not yet implemented | Complete `remote_links` lifecycle, all documented operations and parameters | 3 |
| Software | Security Information | Not yet implemented | Complete `security_information` lifecycle, all documented operations and parameters | 3 |
| Software | Sprint | `sprints`: all 13 documented operations, including writes/deletes where provided | Live commissioning; other product families remain later builds | 1 |
| Service Management | Assets | Not implemented; live license not measured | Add typed lifecycle; verify site entitlement before live smoke | 4 |
| Service Management | Customer | Not implemented; live license not measured | Add typed lifecycle; verify site entitlement before live smoke | 4 |
| Service Management | Customer identities | Not implemented; live license not measured | Add typed lifecycle; verify site entitlement before live smoke | 4 |
| Service Management | Info | Not implemented; live license not measured | Add typed lifecycle; verify site entitlement before live smoke | 4 |
| Service Management | Insight | Not implemented; live license not measured | Add typed lifecycle; verify site entitlement before live smoke | 4 |
| Service Management | Knowledge base | Not implemented; live license not measured | Add typed lifecycle; verify site entitlement before live smoke | 4 |
| Service Management | Organization | Not implemented; live license not measured | Add typed lifecycle; verify site entitlement before live smoke | 4 |
| Service Management | Organization property | Not implemented; live license not measured | Add typed lifecycle; verify site entitlement before live smoke | 4 |
| Service Management | Request | Not implemented; live license not measured | Add typed lifecycle; verify site entitlement before live smoke | 4 |
| Service Management | Request approvals | Not implemented; live license not measured | Add typed lifecycle; verify site entitlement before live smoke | 4 |
| Service Management | Request attachments | Not implemented; live license not measured | Add typed lifecycle; verify site entitlement before live smoke | 4 |
| Service Management | Request comments | Not implemented; live license not measured | Add typed lifecycle; verify site entitlement before live smoke | 4 |
| Service Management | Request feedback | Not implemented; live license not measured | Add typed lifecycle; verify site entitlement before live smoke | 4 |
| Service Management | Request notification | Not implemented; live license not measured | Add typed lifecycle; verify site entitlement before live smoke | 4 |
| Service Management | Request participants | Not implemented; live license not measured | Add typed lifecycle; verify site entitlement before live smoke | 4 |
| Service Management | Request SLA | Not implemented; live license not measured | Add typed lifecycle; verify site entitlement before live smoke | 4 |
| Service Management | Request status | Not implemented; live license not measured | Add typed lifecycle; verify site entitlement before live smoke | 4 |
| Service Management | Request transitions | Not implemented; live license not measured | Add typed lifecycle; verify site entitlement before live smoke | 4 |
| Service Management | Request type | Not implemented; live license not measured | Add typed lifecycle; verify site entitlement before live smoke | 4 |
| Service Management | Service desk | Not implemented; live license not measured | Add typed lifecycle; verify site entitlement before live smoke | 4 |
| Service Management | Service desk customer | Not implemented; live license not measured | Add typed lifecycle; verify site entitlement before live smoke | 4 |
| Service Management | Service desk organization | Not implemented; live license not measured | Add typed lifecycle; verify site entitlement before live smoke | 4 |
| Service Management | Service desk queue | Not implemented; live license not measured | Add typed lifecycle; verify site entitlement before live smoke | 4 |
| Service Management | Service desk request type | Not implemented; live license not measured | Add typed lifecycle; verify site entitlement before live smoke | 4 |
| Service Management | Service desk temporary attachments | Not implemented; live license not measured | Add typed lifecycle; verify site entitlement before live smoke | 4 |

| Service Management index | Assets | Not implemented; entitlement not measured | Complete vendor-group lifecycle in build 4; subfamilies above give the next verbs | 4 |
| Service Management index | Customer | Not implemented; entitlement not measured | Complete vendor-group lifecycle in build 4; subfamilies above give the next verbs | 4 |
| Service Management index | Info | Not implemented; entitlement not measured | Complete vendor-group lifecycle in build 4; subfamilies above give the next verbs | 4 |
| Service Management index | Knowledgebase | Not implemented; entitlement not measured | Complete vendor-group lifecycle in build 4; subfamilies above give the next verbs | 4 |
| Service Management index | Organization | Not implemented; entitlement not measured | Complete vendor-group lifecycle in build 4; subfamilies above give the next verbs | 4 |
| Service Management index | Request | Not implemented; entitlement not measured | Complete vendor-group lifecycle in build 4; subfamilies above give the next verbs | 4 |
| Service Management index | Requesttype | Not implemented; entitlement not measured | Complete vendor-group lifecycle in build 4; subfamilies above give the next verbs | 4 |
| Service Management index | Servicedesk | Not implemented; entitlement not measured | Complete vendor-group lifecycle in build 4; subfamilies above give the next verbs | 4 |

## Numbered build order

1. Multi-site binding and API-token/OAuth 2.0 (3LO) lifecycles; Projects, Board, Sprint, Backlog, versions and worklogs. Each result identifies its site; unknown selectors fail clearly.
2. Complete issue CRUD/search, comments, attachments, transitions, links, watchers, votes and properties at v3; bulk operations, JQL/autocomplete and epics.
3. Administration and collaboration: users/groups, permissions, workflows/statuses, issue types/schemes, fields/contexts/options/screens, project settings/components/releases, filters/dashboards, audit, webhooks and every remaining platform/Software row above.
4. Service Management requests, approvals, queues, SLAs, customers, organizations, knowledge base and Assets. Record unlicensed skips only after measuring entitlement.
5. Confluence sibling: spaces, pages, blog posts, attachments, search and CQL; use its own product API, scopes and coverage table.

## Verification and lifecycle posture

Vendor-owned records use vendor permission, concurrency and history semantics. No invented local revision precondition is sent. Sprint state transitions and version release/undo use the vendor update operations; deleting an object is terminal where Atlassian defines it. Local binding correction/default changes and removal need explicit readback; removing a binding does not revoke the vendor grant. Tokens are vault-held and must never be returned.

Live API-token, two-site, OAuth and JSM license smokes remain unmeasured. Hermetic transcripts are separate evidence and cannot prove a registered site or licensed product.

## Advisory vendor schema corrections

Spec correction: Atlassian Software OpenAPI types getAllBoards.type and getAllSprints.state as empty objects, but the vendor parameter prose lists scrum/kanban/simple and future/active/closed values. Scalar, comma-separated and list forms are accepted and sent as comma-separated filters. Software fields item schemas are likewise empty objects while the prose describes field names; string/comma-separated/list-of-string forms are accepted. Evidence: https://developer.atlassian.com/cloud/jira/software/rest/api-group-board/ and https://developer.atlassian.com/cloud/jira/software/rest/api-group-sprint/ plus the pinned Software specification (sha256 4e108d54b99064475c6ba0f986cce46dcace81336e034b58a5400b93174b927a). These are explicit advisory corrections; the original inventory is preserved.

Platform specification sha256: `e75a52daf7eb0b44470c43649684d5843a70b372ea42762d2feb8e6fa60ffe2a`. Request-only schemas preserve all documented optional body members and open properties. Property operations accept arbitrary JSON through body_json; their path IDs follow each operation's vendor string/integer schema.

Service Management specification sha256: `c6fc7d933820e097e0a8b52cbefe12fd733cd593bf3e159ce3a1e5ba530bd547`. Exact vendor groups are included above alongside request/queue/SLA/customer/organization subfamilies.

Live commissioning attempt: the pre-existing registered test_connection returned a queued job, but the public job readback returned job=null and exposed no terminal vendor result. This is unmeasured connectivity, not a live pass. New two-site/OAuth and JSM entitlement smokes require commissioned credentials and remain unverified.

Results and log hygiene: Results are not scanned. The connector returns the vendor result as received; bulk results go to disk through `output_path`. Audit and log lines are built only from method, path without query values, status, duration and correlation id, so they never carry headers, bodies or credential values. Jira's own log lines name only the endpoint and outcome.
