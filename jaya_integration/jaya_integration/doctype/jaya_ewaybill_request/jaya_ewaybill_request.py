import frappe
from frappe.model.document import Document

from jaya_integration.services.request_write import allowed


class JayaEwaybillRequest(Document):
	def validate(self):
		if not allowed.get():
			frappe.throw("Prepare or update this request through the reviewed LR integration API.")

	def on_trash(self):
		frappe.throw("E-way bill request history cannot be deleted.")
