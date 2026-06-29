"""
Receiver endpoint for push-direction sync.

A branch pushes documents to the central server by calling
``plus_care_sync.api.receive.upsert_document``.  Because branches sync
incrementally and partially, a pushed document often references other
documents that have not been synced yet — or that reference it back
(circular dependencies such as Purchase Invoice ⇄ Payment Entry).

The standard REST resource API always runs link validation, so those
references raise LinkValidationError and block the sync.  This receiver
inserts with ``ignore_links=True`` instead: missing references are
tolerated and arrive in subsequent syncs (eventual consistency — the
Availability + Partition-Tolerance tradeoff this sync system is built on).

Submittable documents are inserted as Draft then submitted, so ERPNext
creates Stock Ledger Entries / GL Entries / Bin naturally — mirroring the
pull-side _apply_submitted_doc logic.

Both branch and central run this same app, so this method is guaranteed
to exist on the receiving side.
"""
import json

import frappe


@frappe.whitelist()
def upsert_document(doctype, name, data, target_docstatus=0):
	"""Insert or update a document received from a branch.

	Args:
		doctype: Target doctype name.
		name: Document name (already branch-prefixed by the sender).
		data: Full document dict as JSON string (or dict).
		target_docstatus: Desired final docstatus (0 draft, 1 submitted, 2 cancelled).

	Returns a small status dict; raises on unrecoverable errors so the
	pushing side records the failure and retries on the next sync.
	"""
	if isinstance(data, str):
		data = json.loads(data)

	target_docstatus = int(target_docstatus or 0)
	data["doctype"] = doctype
	data["name"] = name

	exists = frappe.db.exists(doctype, name)

	if exists:
		current = int(frappe.db.get_value(doctype, name, "docstatus") or 0)

		# Already at or beyond the target state — nothing to do.
		if current >= target_docstatus and not (current == 0 and target_docstatus == 0):
			return {"status": "skipped", "name": name, "docstatus": current}

		if current == 0 and target_docstatus == 0:
			# Update an existing draft with the latest field values.
			doc = frappe.get_doc(doctype, name)
			doc.update({k: v for k, v in data.items()
						if k not in ("name", "doctype", "creation", "owner",
									 "modified", "modified_by", "docstatus")})
			_set_sync_flags(doc)
			doc.save(ignore_permissions=True)
			frappe.db.commit()
			return {"status": "updated", "name": name, "docstatus": 0}

		# Escalate docstatus on an existing document (0→1, 0→2, or 1→2).
		doc = frappe.get_doc(doctype, name)
		_escalate(doc, current, target_docstatus)
		frappe.db.commit()
		return {"status": "escalated", "name": name, "docstatus": doc.docstatus}

	# New document: always insert as Draft first so validation runs cleanly,
	# then transition to the target docstatus.
	draft = dict(data)
	draft["docstatus"] = 0
	doc = frappe.get_doc(draft)
	_set_sync_flags(doc)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()

	if target_docstatus >= 1:
		doc = frappe.get_doc(doctype, name)
		_escalate(doc, 0, target_docstatus)
		frappe.db.commit()

	return {"status": "created", "name": name, "docstatus": doc.docstatus}


def _set_sync_flags(doc):
	"""Flags that let a synced document bypass validation that assumes a fully
	populated, locally-consistent database."""
	doc.flags.from_sync = True
	doc.flags.ignore_links = True
	doc.flags.ignore_mandatory = True
	doc.flags.ignore_permissions = True


def _escalate(doc, current, target):
	"""Move a document from `current` docstatus to `target` (1 submit, 2 cancel)."""
	if current == 0 and target >= 1:
		_set_sync_flags(doc)
		doc.submit()
	if target == 2:
		# Re-fetch in case submit() changed state, then cancel.
		doc = frappe.get_doc(doc.doctype, doc.name)
		_set_sync_flags(doc)
		doc.cancel()
