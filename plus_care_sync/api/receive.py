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

Submittable documents are inserted as Draft then submitted so ERPNext
creates Stock Ledger Entries / GL Entries / Bin naturally.  If submit
fails (e.g. a dependency needed at the business-logic level does not yet
exist), the document is kept as Draft and a warning is logged.  The Draft
record is sufficient for _validate_links() checks in other documents that
reference it; the document will be escalated to Submitted on the next sync
run once its own dependencies arrive.
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

	Returns a status dict.  Never raises — insert failures are logged and
	re-raised so the sender records the failure; submit/cancel failures are
	logged but the draft is preserved so subsequent syncs can retry.
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
			doc.save(ignore_permissions=True)  # ignore_links via flags set by _set_sync_flags
			frappe.db.commit()
			return {"status": "updated", "name": name, "docstatus": 0}

		# Escalate docstatus on an existing document (0→1, 0→2, or 1→2).
		doc = frappe.get_doc(doctype, name)
		final_status = _safe_escalate(doc, current, target_docstatus)
		return {"status": "escalated", "name": name, "docstatus": final_status}

	# New document: insert as Draft first (permanently committed), then try
	# to escalate.  The Draft commit is intentionally separated from the
	# escalation so that a failed submit does NOT roll back the insert —
	# the record must exist for _validate_links() checks in other documents.
	draft = dict(data)
	draft["docstatus"] = 0
	doc = frappe.get_doc(draft)
	_set_sync_flags(doc)
	doc.insert(
		ignore_permissions=True,
		ignore_links=True,
		ignore_mandatory=True,
		set_name=name,          # bypass autoname (e.g. "hash") — preserve original branch name
		set_child_names=False,  # preserve child row names so voucher_detail_no stays consistent
	)
	frappe.db.commit()  # ← permanent: Frappe's error-handler rollback cannot undo this

	final_status = 0
	if target_docstatus >= 1:
		final_status = _safe_escalate(doc, 0, target_docstatus)

	return {"status": "created", "name": name, "docstatus": final_status}


def _set_sync_flags(doc):
	"""Flags that let a synced document bypass validation that assumes a fully
	populated, locally-consistent database."""
	doc.flags.from_sync = True
	doc.flags.ignore_links = True
	doc.flags.ignore_mandatory = True
	doc.flags.ignore_permissions = True
	doc.flags.ignore_validate = True


def _safe_escalate(doc, current, target):
	"""Try to escalate docstatus; on failure roll back and keep the document
	as a Draft so _validate_links() in other documents can still find it.

	Returns the actual docstatus after the operation.
	"""
	try:
		_escalate(doc, current, target)
		frappe.db.commit()
		return target
	except Exception as e:
		# Roll back only the failed escalation — the prior Draft commit is
		# permanent and unaffected by this rollback.
		try:
			frappe.db.rollback()
		except Exception:
			pass
		frappe.log_error(
			f"Plus Care Sync — could not escalate {doc.doctype}/{doc.name} "
			f"from {current} to {target}: {e}\n"
			"Document kept as Draft; will retry on next sync.",
			"Plus Care Sync",
		)
		return current  # stayed at current docstatus


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
