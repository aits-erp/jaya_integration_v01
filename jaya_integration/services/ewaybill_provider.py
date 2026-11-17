"""India Compliance v15.31.4 adapter. No API calls at module import time."""

import base64
from datetime import datetime
from zoneinfo import ZoneInfo

import frappe

IST = ZoneInfo("Asia/Kolkata")


def timestamp(value):
	if not value:
		raise ValueError("Provider did not return validity; Part A-only bills are not supported.")
	try:
		parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
	except ValueError:
		parsed = None
		for fmt in ("%d/%m/%Y %I:%M:%S %p", "%d/%m/%Y %I:%M %p", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M"):
			try:
				parsed = datetime.strptime(str(value).strip(), fmt)
				break
			except ValueError:
				pass
		if parsed is None:
			raise ValueError("Unrecognised provider timestamp.")
	return parsed.replace(tzinfo=IST) if parsed.tzinfo is None else parsed.astimezone(IST)


def api(gstin):
	from india_compliance.gst_india.api_classes.nic.e_waybill import EWaybillAPI

	# create() can authenticate. Never call it in prepare/preview.
	return EWaybillAPI.create(company_gstin=gstin)


def checked(result):
	if (
		not isinstance(result, dict)
		or result.get("error_code")
		or result.get("errorCodes")
		or result.get("status") in (0, "0")
	):
		raise ValueError("Provider rejected the request. Review the restricted India Compliance API log.")
	return result


def pdf(request, details):
	log = frappe.get_doc(
		dict(
			doctype="e-Waybill Log",
			name=request.ewaybill_number,
			e_waybill_number=request.ewaybill_number,
			data=frappe.as_json(details),
			is_latest_data=1,
			is_cancelled=0,
			reference_doctype=request.doctype,
			reference_name=request.name,
			valid_upto=request.valid_until,
		)
	)
	# Scope and request permissions were checked by the caller. Render only this
	# verified in-memory log, without granting broad access to all IC logs.
	previous = frappe.flags.get("ignore_print_permissions")
	frappe.flags.ignore_print_permissions = True
	try:
		content = frappe.get_print(
			"e-Waybill Log", request.ewaybill_number, doc=log, no_letterhead=True, as_pdf=True
		)
	finally:
		frappe.flags.ignore_print_permissions = previous
	if not content.startswith(b"%PDF-") or len(content) > 5 * 1024 * 1024:
		raise ValueError("Invalid or oversized PDF.")
	from frappe.utils.file_manager import save_file

	attachment = save_file(
		f"ewaybill-{request.ewaybill_number}.pdf", content, request.doctype, request.name, is_private=1
	)
	request.pdf_file = attachment.file_url
	return base64.b64encode(content).decode("ascii")
