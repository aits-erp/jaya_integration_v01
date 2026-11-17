import frappe


def after_install():
	if not frappe.db.exists("Role", "Jaya Ewaybill Integration"):
		frappe.get_doc(dict(doctype="Role", role_name="Jaya Ewaybill Integration")).insert(
			ignore_permissions=True
		)


def after_migrate():
	after_install()
