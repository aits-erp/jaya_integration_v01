import frappe
from frappe.model.document import Document

from jaya_integration.services.ewaybill_payload import GSTIN


class JayaIntegrationCompany(Document):
	def validate(self):
		self.gstin = str(self.gstin or "").strip().upper()
		if not GSTIN.fullmatch(self.gstin):
			frappe.throw("A valid generating GSTIN is required.")
		if not self.is_new():
			old = self.get_doc_before_save()
			if old and old.source_company_id != self.source_company_id:
				frappe.throw("Source company identity cannot be changed. Create a new mapping instead.")
