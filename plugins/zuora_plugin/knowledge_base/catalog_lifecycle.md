# Legacy product catalog lifecycle

Article Layer: 1

Article Role: build-specification

Article Tags: domain:zuora, domain:connector, evidence-category:api-inventory

Embedding Description: Typed Zuora legacy catalog operations for products,
product rate plans, product rate plan charges, charge definitions and catalog
groups. Covers creation, current readback, correction, clearing, effective-date
retirement, membership reversal, deletion, request headers and pagination.

## Verified scope

These 28 verbs cover every operation in five sections of the official
[Zuora v1 reference](https://developer.zuora.com/v1-api-reference/api),
reference version 2026-11-06, retrieved 2026-11-07. The newer Commerce API is
a separate section; its metrics, features, entitlements, plans, catalog
queries and price book items remain on `request` for the next build.

| Vendor section | Verbs and operations |
| --- | --- |
| [Products](https://developer.zuora.com/v1-api-reference/api/products) (4) | `create_product`, `get_product`, `update_product`, `delete_product`: POST `/v1/object/product`, GET/PUT/DELETE `/v1/object/product/{product_id}` |
| [Product Rate Plans](https://developer.zuora.com/v1-api-reference/api/product-rate-plans) (7) | `list_product_rate_plans`: GET `/v1/products/{product_key}/product-rate-plans`; `get_product_rate_plan`: GET `/v1/product-rate-plans/{rate_plan_id}`; `get_product_rate_plans_by_external_id`: GET `/v1/product-rate-plans/external-id/{external_id}`; `create_product_rate_plan`, `get_product_rate_plan_object`, `update_product_rate_plan`, `delete_product_rate_plan`: object CRUD under `/v1/object/product-rate-plan` |
| [Product Rate Plan Charges](https://developer.zuora.com/v1-api-reference/api/product-rate-plan-charges) (5) | `get_product_rate_plan_charge`: GET `/v1/product-rate-plan-charges/{charge_key}`; `create_product_rate_plan_charge`, `get_product_rate_plan_charge_object`, `update_product_rate_plan_charge`, `delete_product_rate_plan_charge`: object CRUD under `/v1/object/product-rate-plan-charge` |
| [Product Charge Definitions](https://developer.zuora.com/v1-api-reference/api/product-charge-definitions) (7) | `create_product_charge_definition`, `list_product_charge_definitions`: POST/GET `/v1/product-charge-definitions`; `create_product_charge_definitions`, `update_product_charge_definitions`: POST/PUT `/bulk`; `get_product_charge_definition`, `update_product_charge_definition`, `delete_product_charge_definition`: GET/PUT/DELETE `/{definition_key}` |
| [Catalog Groups](https://developer.zuora.com/v1-api-reference/api/catalog-groups) (5) | `create_catalog_group`, `list_catalog_groups`: POST/GET `/v1/catalog-groups`; `get_catalog_group`, `update_catalog_group`, `delete_catalog_group`: GET/PUT/DELETE `/{catalog_group_key}` |

Products, rate plans and charges can also be listed through the existing
`query_objects` verb. The Products and Product Rate Plan Charges sections
publish no separate list operation. The external-id lookup and charge
definition list return their documented collections without continuation;
Zuora documents no page or cursor parameter for those two routes.

## Requests and results

Every mutation takes `body`, the vendor's JSON document, unchanged. All
documented fields, custom fields, empty strings, arrays, false values and
nulls reach Zuora. The plugin checks top-level required fields from the
published schema, while Zuora validates conditional fields and pricing rules:

- Product create: `Name`, `EffectiveStartDate`, `EffectiveEndDate`.
- Rate plan create: `Name`, `ProductId`.
- Charge create: `Name`, `ChargeType`, `BillCycleType`, `BillingPeriod`,
  `ChargeModel`, `ProductRatePlanChargeTierData`, `ProductRatePlanId`,
  `TriggerEvent`, `UseDiscountSpecificAccountingCode`. The object update
  schema marks the same fields required except `Name` and `ChargeType`.
- Other catalog bodies have no top-level required marker in the reference.
  This does not bypass vendor rules, such as identifying a charge by id or
  number for a charge definition or a grade for a Grading group membership.

The object GETs accept `fields`; object POSTs and PUTs accept
`reject_unknown_fields` (`rejectUnknownFields`). Lists accept `page_size`
and `page` (page requires page_size). Their completion has `pagination` with
`next_page` and the vendor `nextPage` link; pass the number back as `page`.
`list_product_rate_plans` and `get_product_rate_plan_charge` accept
`show_charge_definitions`. Charge definition get/list accept
`hide_inherited_values`, and the list accepts `charge` and `rateplan`.

Every route accepts its documented `zuora_version`, `zuora_entity_ids`,
`zuora_track_id` and, except charge definitions and the newer charge GET,
`zuora_org_ids`. POSTs accept `idempotency_key`. Object GET/POST/PUT accept
`x_zuora_wsdl_version`. The introductory note on the newer charge GET
requires that header above 155.0 although its header table omits it, so the
verb requires `x_zuora_wsdl_version`. The HTTP client negotiates response
compression and sends plain JSON; it does not label plain JSON as gzip.
Authorization always comes from the registered tenant credential.

Queued means scheduled for background execution. Completion carries the
HTTP status and the vendor body as received, not a claim that a catalogue mutation
succeeded merely because it was dispatched. HTTP 200 with `success: false`
or object `Success: false` raises `zuora.validation_failed`. A vendor 401
maps to authentication failure, a 403 to permission denied with its reason,
and a 409/state conflict to validation failure. Writes and deletes are not
automatically replayed by the worker. Pagination and envelope decisions are
derived from the raw response.

## Correction, retirement, reversal and history

| Resource | Lifecycle and terminal action |
| --- | --- |
| Product | Create, current read, correct mutable fields or dates, clear fields with the vendor-accepted empty value, delete. An effective end date ends availability; a later correction can restore availability subject to vendor rules. |
| Rate plan | Create, read as resource/object or by external id, list for product, correct dates/currencies/grade/external ids and optional fields, delete. Correct effective dates to expire or restore availability. |
| Charge | Create, current read in either documented shape, correct pricing/tiers/prepaid and optional fields, delete. Read current state before correction; the vendor enforces catalog references. |
| Charge definition | Create singly or in bulk (up to 1000), list by charge/rateplan, read, correct singly or in bulk including prices/dates/optional overrides, delete. The Attribute-based Pricing tenant feature must be enabled by Zuora. |
| Catalog group | Create Grading or Display, list/read, correct name/description, add/remove product-rate-plan membership and grades, clear mutable fields with an empty value, delete. Reverse membership by adding removed plans and removing added plans. The group type cannot change; this is an Early Adopter API controlled by the vendor. |

These sections publish no separate close/reopen, archive/restore, ETag,
`If-Match`, expected-version or historical-version endpoint. No such verb
or concurrency promise is invented. Current get/list and Object Query are
readback; vendor 409/reference/role refusals remain visible. Read before a
correction and re-read after a refusal. API version headers select schemas,
not optimistic locking. Effective-date corrections and membership reversal
are the applicable reversible actions; delete is terminal. Optional fields
are cleared through the same vendor update body, rather than a local erase
endpoint. Preserve vendor null/empty semantics and never infer that every
field is clearable.

The existing deferred job and `zuora.<operation>` audit line report the
attempted operation, status, duration and correlation id. They do not create a local mutable catalog mirror or claim to provide vendor
revision history. Historical audit retention follows the platform's existing
job/log policy; these sections provide no additional append-only vendor
history API.

`plugins/zuora_plugin/tests/smoke_catalog.py` covers all 28 wire contracts, three registration
artifacts, page continuation/end, correction/clearing, membership reversal,
effective-date retirement/restoration, bulk definition correction, deletes,
state/reference conflicts and 401/403. All transcripts use vendor
object or v1 envelopes through `httpx.MockTransport`; no tenant is contacted.

## Results and log hygiene

Results are not scanned. The connector returns the vendor result as received;
bulk results go to disk through `output_path`. Audit and log lines are built
only from method, path without query values, status, duration and correlation
id, so they never carry headers, bodies or credential values.
Raw vendor status, the v1 `success: false` envelope, pagination and job
identity are read from the response itself.
