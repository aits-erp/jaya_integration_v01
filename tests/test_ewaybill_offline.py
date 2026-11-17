"""Offline tests: no Frappe installation, credentials or network required."""

import copy
import importlib
import json
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jaya_integration.services.ewaybill_payload import build_payload

GST = "27AAAAA0000A1Z5"


def sample():
	return dict(
		lrNo="LR1",
		header=dict(vehicleNo="MH12AB1234"),
		consignor=dict(name="Supplier", address="Billing A", gstNumber=GST),
		consignee=dict(name="Recipient", address="Billing B", gstNumber=GST),
		invoice=dict(
			boeInvoiceNo="INV1",
			boeInvoiceDate="2026-01-01",
			invoiceValue="118",
			ewaybillData=dict(
				docType="INV",
				transMode="1",
				supplyType="O",
				subSupplyType="1",
				transactionType="1",
				fromAddr1="Dispatch A",
				fromPlace="Pune",
				fromPincode="411001",
				fromStateCode="27",
				actFromStateCode="27",
				toAddr1="Delivery B",
				toPlace="Mumbai",
				toPincode="400001",
				toStateCode="27",
				actToStateCode="27",
				transDistance="150",
				vehicleType="R",
				totalValue="100",
				cgstValue="9",
				sgstValue="9",
				igstValue="0",
				cessValue="0",
				cessNonAdvolValue="0",
				otherValue="0",
				itemList=[
					dict(
						productDesc="Goods",
						hsnCode="7308",
						quantity="1",
						qtyUnit="NOS",
						taxableAmount="100",
						cgstRate="9",
						sgstRate="9",
						igstRate="0",
						cessRate="0",
						cessNonAdvol="0",
					)
				],
			),
		),
	)


class PayloadTests(unittest.TestCase):
	def test_complete_and_urp(self):
		lr = sample()
		payload, errors = build_payload(lr, GST)
		self.assertEqual(errors, [])
		self.assertEqual(payload["fromGstin"], GST)
		self.assertEqual(payload["docDate"], "01/01/2026")
		lr["consignee"]["gstNumber"] = "URP"
		self.assertEqual(build_payload(lr, GST)[1], [])

	def test_zero_distance_requests_automatic_calculation(self):
		lr = sample()
		lr["invoice"]["ewaybillData"]["transDistance"] = "0"
		payload, errors = build_payload(lr, GST)
		self.assertEqual(errors, [])
		self.assertEqual(payload["transDistance"], 0)
		lr["invoice"]["ewaybillData"]["transDistance"] = ""
		self.assertTrue(build_payload(lr, GST)[1])

	def test_missing_parties_and_goods(self):
		lr = sample()
		lr["consignee"] = {}
		lr["invoice"]["ewaybillData"]["itemList"] = []
		errors = build_payload(lr, GST)[1]
		self.assertTrue(any("Consignee" in e for e in errors))
		self.assertTrue(any("goods rows" in e for e in errors))

	def test_totals_tax_and_explicit_zero(self):
		lr = sample()
		data = lr["invoice"]["ewaybillData"]
		data["totalValue"] = "105"
		data["itemList"][0]["igstRate"] = "18"
		del data["itemList"][0]["cessRate"]
		errors = build_payload(lr, GST)[1]
		self.assertTrue(any("totalValue" in e for e in errors))
		self.assertTrue(any("cannot both" in e for e in errors))
		self.assertTrue(any("cessRate" in e for e in errors))

	def test_unsupported_and_state_mismatch(self):
		lr = sample()
		data = lr["invoice"]["ewaybillData"]
		data["transMode"] = "2"
		data["fromStateCode"] = "29"
		self.assertGreaterEqual(len(build_payload(lr, GST)[1]), 2)


class Error(Exception):
	pass


class Doc(dict):
	def __getattr__(self, name):
		return self.get(name)

	def __setattr__(self, key, value):
		self[key] = value

	def check_permission(self, *args):
		pass

	def set(self, key, value):
		self[key] = value

	def save(self):
		from jaya_integration.services.request_write import allowed

		if not allowed.get():
			raise Error("uncontrolled write")
		if not self.get("name"):
			self["name"] = self["request_key"]
		self.records[self.name] = self
		return self


class WorkflowTests(unittest.TestCase):
	def setUp(self):
		self.records = {}
		self.calls = []
		self.fail_generate = False
		self.pdf_fail = False
		self.denied = False
		Doc.records = self.records
		fake = types.ModuleType("frappe")
		fake.session = types.SimpleNamespace(user="integration")
		fake.get_cached_doc = lambda name: types.SimpleNamespace(sandbox_mode=False)
		fake.PermissionError = Error
		fake.get_roles = lambda: ["Jaya Ewaybill Integration"]
		fake.whitelist = lambda **kw: lambda f: f
		fake.throw = lambda msg, *args: (_ for _ in ()).throw(Error(msg))
		fake.parse_json = json.loads
		fake.as_json = lambda value: json.dumps(value, default=str)
		fake.get_all = lambda *args, **kw: []
		self.mapping = Doc(company="ERP A", gstin=GST, source_company_code="NEW", enabled=1)

		def get_doc(kind, name=None):
			if isinstance(kind, dict):
				return Doc(kind)
			if kind == "Jaya Integration Company":
				return self.mapping
			if kind == "Company":
				if self.denied:
					raise Error("company access denied")
				return Doc(gstin=GST)
			return self.records[name]

		fake.get_doc = get_doc
		fake.db = types.SimpleNamespace(
			exists=lambda kind, name: name in self.records, sql=lambda *args: None, commit=lambda: None
		)
		sys.modules["frappe"] = fake
		utils = types.ModuleType("frappe.utils")
		utils.get_datetime = lambda value: value
		utils.now_datetime = datetime.now
		sys.modules["frappe.utils"] = utils
		for name in ("jaya_integration.api.ewaybill", "jaya_integration.services.ewaybill_provider"):
			sys.modules.pop(name, None)
		import jaya_integration.services as services

		if hasattr(services, "ewaybill_provider"):
			del services.ewaybill_provider
		self.m = importlib.import_module("jaya_integration.api.ewaybill")
		self.m.provider.api = lambda gstin: self.client()
		self.m.provider.pdf = lambda *args: self.pdf()
		self.args = dict(
			company="ERP A",
			gstin=GST,
			external_id="lr-ewaybill-" + "a" * 24,
			lr=sample(),
			source_company_id="new-company-id",
			source_company_code="NEW",
		)
		self.details = dict(
			ewbNo="123456789012",
			status="ACT",
			validUpto="01/01/2099 11:59:00 PM",
			docNo="INV1",
			docDate="01/01/2026",
			totInvValue=118,
			fromGstin=GST,
			toGstin=GST,
			VehiclListDetails=[dict(vehicleNo="MH12AB1234")],
		)

	def client(self):
		self.calls.append("client")
		return types.SimpleNamespace(generate_e_waybill=self.generate, get_e_waybill=self.fetch)

	def generate(self, payload):
		self.calls.append("generate")
		if self.fail_generate:
			raise TimeoutError()
		return dict(ewayBillNo="123456789012", validUpto="01/01/2099 11:59:00 PM")

	def fetch(self, number):
		self.calls.append("fetch")
		return self.details

	def pdf(self):
		if self.pdf_fail:
			raise Error("renderer unavailable")
		return "JVBERi0xLjQ="

	def prepared(self):
		return self.m.prepare(**self.args)

	def test_prepare_and_preview_do_not_contact_provider(self):
		first = self.prepared()
		second = self.m.preview(**self.args)
		self.assertTrue(first["canGenerate"])
		self.assertEqual(first["preparedVersion"], second["preparedVersion"])
		self.assertEqual(self.calls, [])
		self.assertEqual(len(self.records), 1)

	def test_changed_snapshot_rejects_confirmation(self):
		first = self.prepared()
		self.args["lr"]["header"]["vehicleNo"] = "MH12AB2222"
		with self.assertRaises(Error):
			self.m.generate(**self.args, prepared_version=first["preparedVersion"])
		self.assertEqual(self.calls, [])

	def test_invalid_draft_has_no_generation(self):
		self.args["lr"]["consignee"] = {}
		first = self.prepared()
		self.assertFalse(first["canGenerate"])
		self.assertEqual(first["state"], "Draft")
		with self.assertRaises(Error):
			self.m.generate(**self.args, prepared_version=first["preparedVersion"])
		self.assertEqual(self.calls, [])

	def test_success_and_retry_reuses_result(self):
		first = self.prepared()
		result = self.m.generate(**self.args, prepared_version=first["preparedVersion"])
		self.assertEqual(result["ewaybillNo"], "123456789012")
		again = self.m.generate(**self.args, prepared_version=first["preparedVersion"])
		self.assertEqual(again, result)
		self.assertEqual(self.calls.count("generate"), 1)

	def test_timeout_blocks_retry_then_fetch_reconciles(self):
		first = self.prepared()
		self.fail_generate = True
		with self.assertRaises(Error):
			self.m.generate(**self.args, prepared_version=first["preparedVersion"])
		with self.assertRaises(Error):
			self.m.generate(**self.args, prepared_version=first["preparedVersion"])
		self.assertEqual(self.calls.count("generate"), 1)
		result = self.m.fetch(**self.args, ewaybill_no="123456789012")
		self.assertEqual(result["ewaybillNo"], "123456789012")
		self.assertEqual(self.records[first["requestName"]].state, "Generated")

	def test_pdf_failure_retains_bill_and_fetch_recovers(self):
		first = self.prepared()
		self.pdf_fail = True
		result = self.m.generate(**self.args, prepared_version=first["preparedVersion"])
		self.assertTrue(result["pdfPending"])
		self.assertEqual(self.records[first["requestName"]].state, "Generated")
		self.pdf_fail = False
		self.assertIn("pdfBase64", self.m.fetch(**self.args, ewaybill_no="123456789012"))
		self.assertEqual(self.calls.count("generate"), 1)

	def test_mapping_disabled_wrong_company_and_permission(self):
		self.mapping.enabled = 0
		first = self.prepared()
		self.assertFalse(first["canGenerate"])
		with self.assertRaises(Error):
			self.m.generate(**self.args, prepared_version=first["preparedVersion"])
		self.mapping.company = "OTHER"
		with self.assertRaises(Error):
			self.prepared()
		self.mapping.company = "ERP A"
		self.denied = True
		with self.assertRaises(Error):
			self.prepared()
		self.assertEqual(self.calls, [])

	def test_reassigning_lr_cannot_create_another_request(self):
		self.prepared()
		self.mapping.company = "ERP B"
		self.args["company"] = "ERP B"
		with self.assertRaises(Error):
			self.prepared()
		self.assertEqual(len(self.records), 1)
		self.assertEqual(self.calls, [])

	def test_processing_cannot_regenerate_and_stale_can_fetch(self):
		first = self.prepared()
		request = self.records[first["requestName"]]
		request.state = "Processing"
		request.processing_started_at = datetime.now()
		with self.assertRaises(Error):
			self.m.generate(**self.args, prepared_version=first["preparedVersion"])
		with self.assertRaises(Error):
			self.m.fetch(**self.args, ewaybill_no="123456789012")
		request.processing_started_at = datetime.now() - timedelta(minutes=3)
		self.assertEqual(self.m.fetch(**self.args, ewaybill_no="123456789012")["ewaybillNo"], "123456789012")

	def test_fetch_rejects_mismatch(self):
		self.prepared()
		self.details["docNo"] = "OTHER"
		with self.assertRaises(Error):
			self.m.fetch(**self.args, ewaybill_no="123456789012")

	def test_provider_error_response_rejected(self):
		with self.assertRaises(ValueError):
			self.m.provider.checked({"error_code": "604"})

	def test_status_after_predispatch_authentication_failure(self):
		first = self.prepared()
		def auth_fail(gstin):
			raise Error("Authentication failed")
		self.m.provider.api = auth_fail
		with self.assertRaises(Error):
			self.m.generate(**self.args, prepared_version=first["preparedVersion"])
		args = {k: v for k, v in self.args.items() if k != "lr"}
		self.assertEqual(self.m.status(**args)["state"], "Ready")
		self.assertNotIn("generate", self.calls)

	def test_sandbox_zero_distance_is_blocked_in_preview(self):
		self.m.frappe.get_cached_doc = lambda name: types.SimpleNamespace(sandbox_mode=True)
		self.args["lr"]["invoice"]["ewaybillData"]["transDistance"] = "0"
		preview = self.m.preview(**self.args)
		self.assertFalse(preview["canGenerate"])
		self.assertTrue(any("sandbox" in warning for warning in preview["warnings"]))
		self.assertEqual(self.calls, [])

	def test_fetch_refreshes_edited_draft_snapshot(self):
		first = self.prepared()
		self.args["lr"]["invoice"]["boeInvoiceNo"] = "INV2"
		self.details["docNo"] = "INV2"
		for _ in range(2):
			self.assertEqual(self.m.fetch(**self.args, ewaybill_no="123456789012")["ewaybillNo"], "123456789012")
		request = self.records[first["requestName"]]
		self.assertEqual(request.invoice_number, "INV2")
		self.assertEqual(json.loads(request.source_json)["invoice"]["boeInvoiceNo"], "INV2")

	def test_actual_distance_is_persisted_and_returned(self):
		self.args["lr"]["invoice"]["ewaybillData"]["transDistance"] = "0"
		first = self.prepared()
		original = self.generate
		self.generate = lambda payload: {**original(payload), "distance": 175}
		result = self.m.generate(**self.args, prepared_version=first["preparedVersion"])
		self.assertEqual(result["distance"], 175)
		self.assertEqual(self.records[first["requestName"]].distance, 175)
		self.details["actualDist"] = 180
		result = self.m.fetch(**self.args, ewaybill_no="123456789012")
		self.assertEqual(result["distance"], 180)
		self.assertEqual(self.records[first["requestName"]].distance, 180)

	def test_missing_validity_does_not_report_success(self):
		with self.assertRaises(ValueError):
			self.m.provider.timestamp(None)


if __name__ == "__main__":
	unittest.main()
