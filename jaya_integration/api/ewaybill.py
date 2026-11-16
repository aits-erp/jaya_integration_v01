
import json
import re

import frappe


def parse_lr(lr):
    if isinstance(lr, str):
        try:
            lr = json.loads(lr)
        except json.JSONDecodeError:
            frappe.throw("Invalid LR JSON data")

    if not isinstance(lr, dict):
        frappe.throw("LR must be a valid JSON object")

    return lr


def validate_gstin(gstin):
    if not gstin or not re.fullmatch(
        r"[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]",
        gstin
    ):
        frappe.throw("Invalid GSTIN")


def validate_ewaybill_no(ewaybill_no):
    if not re.fullmatch(r"[0-9]{12}", str(ewaybill_no or "")):
        frappe.throw("Invalid e-way bill number")


@frappe.whitelist(methods=["POST"])
def generate(company, gstin, external_id, lr):
    lr = parse_lr(lr)
    validate_gstin(gstin)

    if not company or not external_id:
        frappe.throw("Company and External ID are required")

    frappe.throw("E-way bill generation provider is not connected yet")


@frappe.whitelist(methods=["POST"])
def fetch(company, gstin, external_id, ewaybill_no, lr=None):
    validate_gstin(gstin)
    validate_ewaybill_no(ewaybill_no)

    if not company or not external_id:
        frappe.throw("Company and External ID are required")

    if lr:
        lr = parse_lr(lr)

    frappe.throw("E-way bill retrieval provider is not connected yet")
