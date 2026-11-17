"""Pure validation/mapping. No Frappe or network calls; reviewed values only.

Initial scope: domestic road movements against a goods invoice, transaction
types 1-4. Imports, exports, other document types and Part A require extensions.
"""

import hashlib
import json
import math
import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

GSTIN = re.compile(r"\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[A-Z0-9]")
UNITS = set(
	"BAG BAL BDL BKL BOU BOX BTL BUN CAN CBM CCM CMS CTN DOZ DRM GGK GMS GRS GYD KGS KLR KME LTR MLT MTR MTS NOS PAC PCS PRS QTL ROL SET SQF SQM SQY TBS TGM THD TON TUB UGS UNT YDS OTH".split()
)
STATES = set(range(1, 39))


def digest(value):
	return hashlib.sha256(
		json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
	).hexdigest()


def text(value):
	return str(value or "").strip()


def vehicle(value):
	return re.sub(r"[^A-Z0-9]", "", text(value).upper())


def number(value):
	if isinstance(value, bool) or value is None or value == "":
		return None
	try:
		result = float(str(value).replace(",", ""))
		return result if math.isfinite(result) else None
	except (TypeError, ValueError):
		return None


def money(value):
	return float(Decimal(str(value)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP))


def iso_date(value):
	try:
		return date.fromisoformat(text(value))
	except ValueError:
		return None


def build_payload(lr, gstin):
	errors = []
	invoice = lr.get("invoice") or {}
	data = invoice.get("ewaybillData") or {}
	payload = {}
	for key in (
		"supplyType",
		"subSupplyType",
		"transactionType",
		"fromAddr1",
		"fromAddr2",
		"fromPlace",
		"fromPincode",
		"fromStateCode",
		"actFromStateCode",
		"toAddr1",
		"toAddr2",
		"toPlace",
		"toPincode",
		"toStateCode",
		"actToStateCode",
		"transDistance",
		"vehicleType",
		"transDocNo",
		"transDocDate",
	):
		payload[key] = data.get(key, "")
	payload.update(
		userGstin=gstin,
		docType="INV",
		transMode="1",
		transporterId=gstin,
		docNo=text(invoice.get("boeInvoiceNo")),
		vehicleNo=vehicle((lr.get("header") or {}).get("vehicleNo")),
	)
	if data.get("docType") != "INV" or data.get("transMode") != "1":
		errors.append("Only goods invoices and road transport are supported in this release.")
	if payload["supplyType"] not in ("O", "I") or str(payload["subSupplyType"]) != "1":
		errors.append(
			"Select inward/outward supply and Supply subtype (1); other movement reasons need an adapter extension."
		)
	if str(payload["transactionType"]) not in ("1", "2", "3", "4"):
		errors.append("Select the bill-from/dispatch-from and bill-to/ship-to transaction type.")
	else:
		payload["transactionType"] = int(payload["transactionType"])
	if not re.fullmatch(r"[A-Za-z0-9/-]{1,16}", payload["docNo"]):
		errors.append("Invoice number must contain 1-16 letters, digits, / or -.")
	inv_date = iso_date(invoice.get("boeInvoiceDate"))
	if not inv_date or inv_date > date.today():
		errors.append("A valid invoice date, no later than today, is required.")
	payload["docDate"] = inv_date.strftime("%d/%m/%Y") if inv_date else ""
	for side, party_key in (("from", "consignor"), ("to", "consignee")):
		party = lr.get(party_key) or {}
		name = text(party.get("name"))
		gst = text(party.get("gstNumber")).upper()
		payload[side + "TrdName"] = name
		payload[side + "Gstin"] = gst
		if not name or len(name) > 100:
			errors.append(f"{party_key.title()} name is required (maximum 100 characters).")
		if not text(party.get("address")):
			errors.append(f"{party_key.title()} billing address is required on the LR.")
		if gst != "URP" and not GSTIN.fullmatch(gst):
			errors.append(f"{party_key.title()} GSTIN or explicit URP is required.")
		for suffix in ("Addr1", "Place"):
			value = text(payload[side + suffix])
			payload[side + suffix] = value
			if not value or len(value) > (120 if suffix == "Addr1" else 50):
				errors.append(
					f"{party_key.title()} physical address/place is required within provider length limits."
				)
		pin = text(payload[side + "Pincode"])
		if not re.fullmatch(r"[1-9]\d{5}", pin):
			errors.append(f"{party_key.title()} physical PIN must contain six digits.")
		else:
			payload[side + "Pincode"] = int(pin)
		for key in (side + "StateCode", "act" + side.title() + "StateCode"):
			value = number(payload[key])
			if value not in STATES:
				errors.append(f"{key}: valid Indian GST state code required.")
			else:
				payload[key] = int(value)
		if GSTIN.fullmatch(gst) and payload[side + "StateCode"] != int(gst[:2]):
			errors.append(f"{party_key.title()} billing state does not match its GSTIN.")
	if payload["transactionType"] in (1, 2) and payload["fromStateCode"] != payload["actFromStateCode"]:
		errors.append("Dispatch state differs from billing state: select dispatch-from transaction type.")
	if payload["transactionType"] in (1, 3) and payload["toStateCode"] != payload["actToStateCode"]:
		errors.append("Shipping state differs from billing state: select ship-to transaction type.")
	distance = number(payload["transDistance"])
	if distance is None or not 0 <= distance <= 4000:
		errors.append("Use 0 for automatic PIN-based distance, or enter a distance up to 4000 km.")
	else:
		payload["transDistance"] = distance
	if not re.fullmatch(r"[A-Z0-9]{7,15}", payload["vehicleNo"]):
		errors.append("A valid road vehicle number is required.")
	if payload["vehicleType"] not in ("R", "O"):
		errors.append("Select regular or over-dimensional vehicle type.")
	if payload["transDocDate"]:
		doc_date = iso_date(payload["transDocDate"])
		if not doc_date:
			errors.append("Transport document date must be YYYY-MM-DD.")
		else:
			payload["transDocDate"] = doc_date.strftime("%d/%m/%Y")
	totals = dict(totalValue=0, cgstValue=0, sgstValue=0, igstValue=0, cessValue=0, cessNonAdvolValue=0)
	items = data.get("itemList") or []
	payload["itemList"] = []
	if not isinstance(items, list) or not 1 <= len(items) <= 250:
		errors.append("Add 1-250 reviewed goods rows.")
		items = []
	for index, raw in enumerate(items, 1):
		if not isinstance(raw, dict):
			errors.append(f"Goods row {index} is invalid.")
			continue
		row = dict(
			itemNo=index,
			productName="",
			productDesc=text(raw.get("productDesc")),
			hsnCode=text(raw.get("hsnCode")),
			qtyUnit=text(raw.get("qtyUnit")).upper(),
		)
		if not row["productDesc"] or len(row["productDesc"]) > 300:
			errors.append(f"Goods row {index}: description is required (maximum 300 characters).")
		if not re.fullmatch(r"\d{4}|\d{6}|\d{8}", row["hsnCode"]) or row["hsnCode"].startswith("99"):
			errors.append(f"Goods row {index}: a goods HSN of 4, 6 or 8 digits is required.")
		if row["qtyUnit"] not in UNITS:
			errors.append(f"Goods row {index}: choose a valid NIC unit code.")
		for key in (
			"quantity",
			"taxableAmount",
			"cgstRate",
			"sgstRate",
			"igstRate",
			"cessRate",
			"cessNonAdvol",
		):
			value = number(raw.get(key))
			if (
				value is None
				or value < 0
				or (key == "quantity" and value == 0)
				or (key.endswith("Rate") and value > 100)
			):
				errors.append(
					f"Goods row {index}: enter a valid {key}; enter zero explicitly when applicable."
				)
				value = 0
			row[key] = value
		if row["igstRate"] and (row["cgstRate"] or row["sgstRate"]):
			errors.append(f"Goods row {index}: IGST and CGST/SGST cannot both apply.")
		if row["cgstRate"] != row["sgstRate"]:
			errors.append(f"Goods row {index}: CGST and SGST rates must match.")
		totals["totalValue"] += row["taxableAmount"]
		for key, rate in [
			("cgstValue", "cgstRate"),
			("sgstValue", "sgstRate"),
			("igstValue", "igstRate"),
			("cessValue", "cessRate"),
		]:
			totals[key] += money(row["taxableAmount"] * row[rate] / 100)
		totals["cessNonAdvolValue"] += money(row["quantity"] * row["cessNonAdvol"])
		payload["itemList"].append(row)
	for key, computed in totals.items():
		entered = number(data.get(key))
		if entered is None or entered < 0:
			errors.append(f"{key}: enter the reviewed invoice total, including explicit zero.")
		elif abs(entered - money(computed)) > 1:
			errors.append(f"{key}: invoice total differs from goods rows by more than one rupee.")
		payload[key] = entered if entered is not None else 0
	other = number(data.get("otherValue"))
	if other is None:
		errors.append("Enter other charges/rounding, including explicit zero.")
	payload["otherValue"] = other if other is not None else 0
	total = number(invoice.get("invoiceValue"))
	payload["totInvValue"] = total if total is not None else 0
	if (
		total is None
		or total <= 0
		or abs(sum(payload[k] for k in totals) + payload["otherValue"] - total) > 1
	):
		errors.append(
			"Invoice value must match taxable value, taxes, cess and other charges within one rupee."
		)
	return payload, list(dict.fromkeys(errors))
