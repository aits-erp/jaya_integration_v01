# Jaya Integration

Frappe v15 custom app for transporter e-way bill requests from Jaya Logistics.

## What is implemented

- App-owned `Jaya Ewaybill Request` and `Jaya Ewaybill Item` DocTypes.
- `Jaya Integration Company` mappings for any number of source operating companies.
- Authenticated `prepare`, `preview`, `generate` and `fetch` APIs.
- Server-side field/tax validation, reviewed-version confirmation, company checks, row locking and duplicate protection.
- Persistent Processing/Uncertain states and Fetch reconciliation; private PDF attachment with a PDF-pending fallback.
- Offline tests with mocked Frappe/provider calls.

Next.js owns extraction, corrections and LR approval. This app owns request persistence and the provider call. Goods invoices belong to consignors; this app does not book them as the transporter's accounting invoices.

## Supported first-release scope

Domestic road supply movements against goods invoices, billing/dispatch/shipping transaction types 1-4, registered parties or explicitly entered URP. Requires a vehicle, goods/HSN/NIC units and explicit tax values. Distance 0 requests automatic calculation from dispatch/shipping PIN codes; enter a manual distance if NIC cannot calculate it. Sandbox requires a positive distance. Import/export, other movement reasons/documents, Part A-only, cancellation, extension and vehicle-update workflows are not implemented.

## Installation / upgrade

Use this existing repository; do not create another app. In a compatible development Bench:

```bash
bench get-app <THIS_REPOSITORY_URL> --branch <YOUR_BRANCH>
bench --site YOUR_SITE install-app jaya_integration
bench --site YOUR_SITE migrate
```

ERPNext and India Compliance must be installed. For an already-installed app, update its code and migrate. On Frappe Cloud, deploy the repository through your private bench and site app/deployment controls; local Windows project files are not automatically deployed.

The DocTypes are standard app-owned definitions under `jaya_integration/jaya_integration/doctype/`. Do not also run the old `setup.install()` helper from the logistics documentation: it creates a different database-custom prototype with the same name. Existing prototype data needs a deliberate migration before installing this schema.

The install/migrate hooks create the `Jaya Ewaybill Integration` role. Assign it to a dedicated API user and restrict that user's Company access. System Managers configure mappings. Normal users cannot edit request payload/state directly; they prepare reviewed data through the integration API. Request history cannot be deleted through Desk.

## Configure each operating company

For every existing or newly added Next.js subcompany:

1. Complete its legal profile and configure the matching ERPNext Company and linked GST address.
2. Add its authorised GSP credentials in GST Settings and complete India Compliance API setup.
3. Create `Jaya Integration Company`: source Mongo company ID, exact source code, ERPNext Company, GSTIN. Allow Generation defaults off.
4. Add the same mapping to Next.js `FRAPPE_OPERATING_COMPANY_MAP`. Mapping keys are dynamic; there is no fixed company count.
5. Grant the integration user Company access. Enable generation only after deployment checks pass.

The company saved on the LR controls scope. An unmapped company cannot fall back to another company's credentials. Request identity is unique per source LR, preventing company remapping from creating a second generation request.

## Next.js server settings

```ini
FRAPPE_URL=https://YOUR_SITE
FRAPPE_API_KEY=<integration user key>
FRAPPE_API_SECRET=<integration user secret>
FRAPPE_EWAYBILL_PREVIEW_METHOD=jaya_integration.api.ewaybill.preview
FRAPPE_EWAYBILL_GENERATE_METHOD=jaya_integration.api.ewaybill.generate
FRAPPE_EWAYBILL_FETCH_METHOD=jaya_integration.api.ewaybill.fetch
FRAPPE_EWAYBILL_STATUS_METHOD=jaya_integration.api.ewaybill.status
FRAPPE_EWAYBILL_ENABLED=false
```

Set the global flag to true and the selected Next.js map entry's `enabled` to true only when generation should be available. The Frappe mapping also has an independent Allow Generation switch. Fetch remains available for authorised retrieval/recovery. API secrets remain server-side and GSP credentials stay in ERPNext.

## API contract

Authenticated POST `/api/method/jaya_integration.api.ewaybill.<method>` using the dedicated user's API key/secret. No Guest access.

Common arguments:

- `company`: exact ERPNext Company name.
- `gstin`: its configured generating GSTIN.
- `source_company_id`, `source_company_code`: saved LR's operating company identity.
- `external_id`: `lr-ewaybill-<24-character Mongo LR ID>`.
- `lr`: object containing lrNo, header.vehicleNo, consignor/consignee name/address/gstNumber, invoice number/date/value and `invoice.ewaybillData`.

`invoice.ewaybillData` contains the reviewed NIC-style fields: docType INV, transMode 1, supplyType, subSupplyType 1, transactionType, physical from/to addresses/place/PIN, billing and physical state codes, vehicleType, transDistance, optional transport document number/ISO date, itemList and explicit invoice totals. The backend derives invoice identity, parties, vehicle and generating identity from the common source arguments; it does not trust duplicate values inside this data.

Goods rows contain productDesc, hsnCode, quantity, qtyUnit, taxableAmount, cgstRate, sgstRate, igstRate, cessRate and cessNonAdvol. Totals contain totalValue, cgstValue, sgstValue, igstValue, cessValue, cessNonAdvolValue and otherValue. Zero must be entered explicitly where applicable. No tax treatment is inferred from extracted text.

- `prepare` / `preview`: persist and validate; return requestName, preparedVersion, state, warnings, canGenerate and providerPayload. No API client is constructed and no external provider request occurs.
- `generate`: additionally requires `prepared_version` from the same reviewed snapshot. Successful repeats return the stored result; Processing/Uncertain blocks generation.
- `fetch`: additionally requires `ewaybill_no`. Validates invoice number/date/value, party GSTINs, active validity and latest vehicle. Does not require a complete generation payload for a customer-provided bill.

Frappe wraps the response in `message`. Generate/Fetch return ewaybillNo, expiryDate, requestName and pdfBase64 if available, otherwise pdfPending true. PDF is attached privately to the request. The original uploaded invoice is not transferred by this API.

## File responsibilities

| File | Purpose |
|---|---|
| `api/ewaybill.py` | Authentication, company scope, preparation, state transitions, row locks, generation and fetch |
| `services/ewaybill_payload.py` | Pure field mapping and validation |
| `services/ewaybill_provider.py` | Version-sensitive India Compliance client and print adapter |
| `services/request_write.py` | Server-only write context preventing direct payload/state modification |
| `jaya_integration/doctype/` | App-owned schema and controllers |
| `install.py`, `hooks.py` | Dependencies and role installation |
| `tests/test_ewaybill_offline.py` | Provider-free behaviour tests |

## Recovery

Generation commits Processing before dispatch and Generated plus number/validity before fetching/printing. A dispatch exception is conservatively Uncertain. Do not automatically generate again. Inspect the restricted India Compliance API log for the actual bill number and use Fetch with the original LR snapshot. A stale Processing request can be fetched after two minutes; wait for a known active request to finish. If no bill exists and the outcome remains uncertain, an authorised administrator must investigate before any separately implemented reset; no blind reset endpoint is provided.

A missing PDF does not erase the bill. Use Fetch to retry print retrieval. Changed source invoice/parties/vehicle cannot be used to reconcile an uncertain/generated request. Issued bill amendment workflows need separate implementation.

## Verification status and tests

Source compatibility was checked against public India Compliance **v15.31.4**: EWaybillAPI.create(company_gstin=...), generate_e_waybill(data), get_e_waybill(number), GSTIN credential lookup, and the log print path. Frappe target is **15.120.1**, ERPNext **15.121.2**. This is source verification only, not proof of site configuration or successful live provider access.

```bash
python tests/test_ewaybill_offline.py
```

Before enabling production: migrate and verify schema/role installation on a real Frappe site; test Company restrictions, concurrent requests, timestamps, API responses and private PDF access; then perform an authorised live Fetch and planned generation. No live site calls were made during implementation.

Sources: [India Compliance v15.31.4 API](https://github.com/resilient-tech/india-compliance/blob/v15.31.4/india_compliance/gst_india/api_classes/nic/e_waybill.py), [GST setup](https://docs.indiacompliance.app/docs/ewaybill-and-einvoice/gst_settings).

License: MIT.

Recovery update: the authenticated `status` method returns the locked persisted request state without provider calls. Next.js only clears an uncertain marker when Frappe confirms Draft/Ready; Processing/Uncertain and unreachable status remain blocked. Generated results can be recovered from stored metadata. Fetch refreshes editable draft snapshots before verification. Preview rejects distance 0 in sandbox; positive returned provider distance is stored and returned as `distance`.
