"""Authenticated request workflow. No accounting invoices or import-time API calls."""

import hashlib
import re
from datetime import datetime

import frappe

from jaya_integration.services import ewaybill_provider as provider
from jaya_integration.services.ewaybill_payload import (
	GSTIN,
	build_payload,
	digest,
	iso_date,
	number,
	text,
	vehicle,
)
from jaya_integration.services.request_write import allowed

DOCTYPE = "Jaya Ewaybill Request"
ROLE = "Jaya Ewaybill Integration"


def obj(value):
	value = frappe.parse_json(value) if isinstance(value, str) else value
	if not isinstance(value, dict):
		frappe.throw("Expected a JSON object.")
	return value


def scope(company, gstin, source_company_id, source_company_code):
	if frappe.session.user == "Guest" or not set(frappe.get_roles()).intersection({ROLE, "System Manager"}):
		frappe.throw("E-way bill integration role required.", frappe.PermissionError)
	gstin = text(gstin).upper()
	if not GSTIN.fullmatch(gstin):
		frappe.throw("Invalid generating GSTIN.")
	mapping = frappe.get_doc("Jaya Integration Company", text(source_company_id))
	if (
		mapping.company != company
		or mapping.gstin != gstin
		or mapping.source_company_code != source_company_code
	):
		frappe.throw("Source operating company, ERPNext company and GSTIN mapping do not match.")
	doc = frappe.get_doc("Company", company)
	doc.check_permission("read")
	known = {text(doc.get("gstin")).upper()}
	addresses = frappe.get_all(
		"Dynamic Link",
		filters=dict(parenttype="Address", link_doctype="Company", link_name=company),
		pluck="parent",
	)
	for name in addresses:
		known.add(text(frappe.get_doc("Address", name).get("gstin")).upper())
	if gstin not in known:
		frappe.throw("GSTIN does not belong to this ERPNext company or its linked addresses.")
	return gstin, mapping


def request_name(external_id):
	if not re.fullmatch(r"lr-ewaybill-[a-fA-F0-9]{24}", str(external_id or "")):
		frappe.throw("Expected the saved LR Mongo ID in external_id.")
	return hashlib.sha256(external_id.lower().encode()).hexdigest()


def lock(name):
	frappe.db.sql("SELECT name FROM `tabJaya Ewaybill Request` WHERE name=%s FOR UPDATE", (name,))


def get_request(name, company, gstin, source_company_id):
	request = frappe.get_doc(DOCTYPE, name)
	request.check_permission("write")
	if (request.company, request.gstin, request.source_company_id) != (company, gstin, source_company_id):
		frappe.throw(
			"This LR already belongs to another company request. Reconcile it before changing company."
		)
	return request


def save(request):
	token = allowed.set(True)
	try:
		request.save()
	finally:
		allowed.reset(token)


def summary(request):
	return dict(
		requestName=request.name,
		preparedVersion=request.payload_hash,
		canGenerate=request.state == "Ready",
		state=request.state,
		warnings=frappe.parse_json(request.validation_json or "[]"),
		providerPayload=frappe.parse_json(request.payload_json or "{}"),
	)


def context(company, gstin, external_id, lr, source_company_id, source_company_code):
	gstin, mapping = scope(company, gstin, source_company_id, source_company_code)
	return gstin, mapping, obj(lr), request_name(external_id)


def version_for(company, gstin, source_company_id, lr, payload):
	return digest(
		dict(company=company, gstin=gstin, source_company_id=source_company_id, lr=lr, payload=payload)
	)


@frappe.whitelist(methods=["POST"])
def prepare(company, gstin, external_id, lr, source_company_id, source_company_code):
	gstin, mapping, lr, name = context(
		company, gstin, external_id, lr, source_company_id, source_company_code
	)
	payload, errors = build_payload(lr, gstin)
	if payload["transDistance"] == 0 and frappe.get_cached_doc("GST Settings").sandbox_mode:
		errors.append("India Compliance sandbox requires a manual distance between 1 and 4000 km.")
	if not mapping.enabled:
		errors.append("Generation is disabled for this operating company in Jaya Integration Company.")
	version = version_for(company, gstin, source_company_id, lr, payload)
	if frappe.db.exists(DOCTYPE, name):
		lock(name)
		request = get_request(name, company, gstin, source_company_id)
		if request.state in ("Processing", "Uncertain", "Generated"):
			if request.payload_hash != version:
				frappe.throw(
					"Previous request is processing, uncertain or generated. Fetch/reconcile before changing data."
				)
			return summary(request)
	else:
		request = frappe.get_doc(
			dict(
				doctype=DOCTYPE,
				request_key=name,
				external_id=external_id,
				company=company,
				gstin=gstin,
				source_company_id=source_company_id,
				source_company_code=source_company_code,
			)
		)
	invoice = lr.get("invoice") or {}
	request.update(
		dict(
			lr_no=lr.get("lrNo"),
			invoice_number=invoice.get("boeInvoiceNo"),
			invoice_date=iso_date(invoice.get("boeInvoiceDate")),
			invoice_value=number(invoice.get("invoiceValue")) or 0,
			consignor=payload.get("fromTrdName"),
			consignor_gstin=payload.get("fromGstin"),
			consignee=payload.get("toTrdName"),
			consignee_gstin=payload.get("toGstin"),
			dispatch_address=frappe.as_json(
				{
					k: payload[k]
					for k in ("fromAddr1", "fromAddr2", "fromPlace", "fromPincode", "actFromStateCode")
				}
			),
			shipping_address=frappe.as_json(
				{k: payload[k] for k in ("toAddr1", "toAddr2", "toPlace", "toPincode", "actToStateCode")}
			),
			vehicle_number=payload["vehicleNo"],
			distance=number(payload.get("transDistance")) or 0,
			source_json=frappe.as_json(lr),
			payload_json=frappe.as_json(payload),
			payload_hash=version,
			validation_json=frappe.as_json(errors),
			state="Draft" if errors else "Ready",
			last_error="",
		)
	)
	request.set(
		"items",
		[
			dict(
				description=r["productDesc"],
				hsn=r["hsnCode"],
				quantity=r["quantity"],
				unit=r["qtyUnit"],
				taxable_value=r["taxableAmount"],
				cgst_rate=r["cgstRate"],
				sgst_rate=r["sgstRate"],
				igst_rate=r["igstRate"],
				cess_rate=r["cessRate"],
				cess_non_advol=r["cessNonAdvol"],
			)
			for r in payload["itemList"]
		],
	)
	save(request)
	return summary(request)


@frappe.whitelist(methods=["POST"])
def preview(company, gstin, external_id, lr, source_company_id, source_company_code):
	# Neither prepare nor preview constructs the authenticating API client.
	return prepare(company, gstin, external_id, lr, source_company_id, source_company_code)


@frappe.whitelist(methods=["POST"])
def status(company, gstin, external_id, source_company_id, source_company_code):
	gstin, _mapping = scope(company, gstin, source_company_id, source_company_code)
	name = request_name(external_id)
	# Serialize with preparation/dispatch. Never clear a marker based on a stale read.
	lock(name)
	request = get_request(name, company, gstin, source_company_id)
	stored = frappe.parse_json(request.result_json or "{}")
	result = {key: stored[key] for key in ("ewaybillNo", "expiryDate", "distance") if key in stored}
	return dict(requestName=name, state=request.state, result=result)


def bill_number(value):
	value = str(value or "")
	if not re.fullmatch(r"\d{12}", value):
		raise ValueError("Provider did not return a 12-digit e-way bill number.")
	return value


def check_details(details, lr, number_value):
	if bill_number(details.get("ewbNo") or details.get("ewayBillNo")) != number_value:
		frappe.throw("Provider returned a different e-way bill.")
	if str(details.get("status") or "").upper() != "ACT":
		frappe.throw("The e-way bill is not active.")
	expiry = provider.timestamp(details.get("validUpto"))
	if expiry <= datetime.now(provider.IST):
		frappe.throw("The e-way bill has expired.")
	invoice = lr.get("invoice") or {}
	for key, expected in [
		("docNo", invoice.get("boeInvoiceNo")),
		("fromGstin", (lr.get("consignor") or {}).get("gstNumber")),
		("toGstin", (lr.get("consignee") or {}).get("gstNumber")),
	]:
		if not expected or text(details.get(key)).upper() != text(expected).upper():
			frappe.throw(f"Missing or mismatched {key}.")
	try:
		expected_date = datetime.strptime(invoice.get("boeInvoiceDate", ""), "%Y-%m-%d").date()
		actual_date = datetime.strptime(str(details.get("docDate", "")), "%d/%m/%Y").date()
	except ValueError:
		frappe.throw("Valid invoice dates are required for verification.")
	if actual_date != expected_date:
		frappe.throw("Invoice date does not match the LR.")
	expected, actual = number(invoice.get("invoiceValue")), number(details.get("totInvValue"))
	if expected is None or actual is None or abs(expected - actual) > 0.01:
		frappe.throw("Invoice value does not match the LR.")
	history = details.get("VehiclListDetails") or []
	expected_vehicle = vehicle((lr.get("header") or {}).get("vehicleNo"))
	if not history or not expected_vehicle or vehicle(history[0].get("vehicleNo")) != expected_vehicle:
		frappe.throw("Latest vehicle does not match the LR; Part A-only bills are unsupported.")
	return expiry


def result_with_pdf(request, details=None):
	result = obj(request.result_json)
	result.update(requestName=request.name)
	if details:
		actual_distance = number(details.get("actualDist"))
		if actual_distance is not None and 0 < actual_distance <= 4000:
			request.distance = actual_distance
		request.provider_data_json = frappe.as_json(details)
		try:
			result["pdfBase64"] = provider.pdf(request, details)
			result.pop("pdfPending", None)
		except Exception:
			result["pdfPending"] = True
			request.last_error = "Bill retained; PDF rendering failed. Use Fetch to retry."
	else:
		result["pdfPending"] = True
	if request.distance and 0 < request.distance <= 4000:
		result["distance"] = request.distance
	request.result_json = frappe.as_json(result)
	save(request)
	return result


@frappe.whitelist(methods=["POST"])
def generate(company, gstin, external_id, lr, source_company_id, source_company_code, prepared_version):
	gstin, mapping, lr, name = context(
		company, gstin, external_id, lr, source_company_id, source_company_code
	)
	if not mapping.enabled:
		frappe.throw("Generation is disabled for this operating company.")
	payload, errors = build_payload(lr, gstin)
	if errors:
		frappe.throw("Complete the reviewed invoice: " + "; ".join(errors))
	if payload["transDistance"] == 0 and frappe.get_cached_doc("GST Settings").sandbox_mode:
		frappe.throw("India Compliance sandbox requires a manual distance between 1 and 4000 km.")
	lock(name)
	request = get_request(name, company, gstin, source_company_id)
	version = version_for(company, gstin, source_company_id, lr, payload)
	if prepared_version != request.payload_hash or version != request.payload_hash:
		frappe.throw("The reviewed request changed. Prepare and preview it again.")
	if request.state == "Generated":
		return obj(request.result_json)
	if request.state != "Ready":
		frappe.throw("Request is not ready. Processing/uncertain requests must be reconciled.")
	client = provider.api(gstin)
	request.state = "Processing"
	request.processing_started_at = datetime.now(provider.IST).replace(tzinfo=None)
	save(request)
	frappe.db.commit()
	try:
		response = provider.checked(client.generate_e_waybill(payload))
		number_value = bill_number(response.get("ewayBillNo"))
		expiry = provider.timestamp(response.get("validUpto"))
		request.ewaybill_number = number_value
		actual_distance = number(response.get("distance"))
		if actual_distance is not None and 0 < actual_distance <= 4000:
			request.distance = actual_distance
		request.valid_until = expiry.replace(tzinfo=None)
		request.generated_at = (
			provider.timestamp(response.get("ewayBillDate")).replace(tzinfo=None)
			if response.get("ewayBillDate")
			else None
		)
		request.state = "Generated"
		request.result_json = frappe.as_json(
			dict(ewaybillNo=number_value, expiryDate=expiry.isoformat(), requestName=name, pdfPending=True, distance=request.distance)
		)
		save(request)
		frappe.db.commit()
	except Exception:
		request.state = "Uncertain"
		request.last_error = "Generation outcome requires reconciliation. Inspect the restricted India Compliance API log and Fetch the actual bill number."
		save(request)
		frappe.db.commit()
		frappe.throw(request.last_error)
	try:
		details = provider.checked(client.get_e_waybill(number_value))
		check_details(details, lr, number_value)
	except Exception:
		details = None
	return result_with_pdf(request, details)


@frappe.whitelist(methods=["POST"])
def fetch(company, gstin, external_id, ewaybill_no, lr, source_company_id, source_company_code):
	gstin, mapping, lr, name = context(
		company, gstin, external_id, lr, source_company_id, source_company_code
	)
	number_value = bill_number(ewaybill_no)
	if not frappe.db.exists(DOCTYPE, name):
		prepare(company, gstin, external_id, lr, source_company_id, source_company_code)
	lock(name)
	request = get_request(name, company, gstin, source_company_id)
	if request.state in ("Draft", "Ready"):
		# Fetch verifies the current LR, so its permanent snapshot and item rows
		# must also describe the current LR, not an older prepared draft.
		prepare(company, gstin, external_id, lr, source_company_id, source_company_code)
		request = get_request(name, company, gstin, source_company_id)
	if request.state == "Processing":
		from frappe.utils import get_datetime, now_datetime

		if (
			not request.processing_started_at
			or (now_datetime() - get_datetime(request.processing_started_at)).total_seconds() < 120
		):
			frappe.throw("Generation is processing. Wait at least two minutes before reconciliation.")
	if request.ewaybill_number and request.ewaybill_number != number_value:
		frappe.throw("This request already records a different e-way bill.")
	details = provider.checked(provider.api(gstin).get_e_waybill(number_value))
	expiry = check_details(details, lr, number_value)
	if request.state in ("Processing", "Uncertain", "Generated"):
		check_details(details, obj(request.source_json), number_value)
	request.ewaybill_number = number_value
	request.valid_until = expiry.replace(tzinfo=None)
	request.state = "Generated"
	request.last_error = ""
	request.result_json = frappe.as_json(
		dict(ewaybillNo=number_value, expiryDate=expiry.isoformat(), requestName=name, pdfPending=True)
	)
	save(request)
	frappe.db.commit()
	return result_with_pdf(request, details)
