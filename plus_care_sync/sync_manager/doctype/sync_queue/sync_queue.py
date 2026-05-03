# Copyright (c) 2024, Pluscare Team and contributors
# For license information, please see license.txt

import frappe
import json
from frappe.model.document import Document
from frappe import _
from frappe.utils import now_datetime


class SyncQueue(Document):
	def before_insert(self):
		"""Set default values"""
		if not self.queued_at:
			self.queued_at = now_datetime()

	@frappe.whitelist()
	def approve(self):
		"""Approve this item for publishing"""
		if self.status != "Pending":
			frappe.throw(_("Only pending items can be approved"))

		self.status = "Approved"
		self.reviewed_by = frappe.session.user
		self.reviewed_at = now_datetime()
		self.save()

		frappe.msgprint(_("Item approved. Click 'Publish' to create the record."), indicator="green", alert=True)

	@frappe.whitelist()
	def reject(self, reason=None):
		"""Reject this item"""
		if self.status not in ["Pending", "Approved"]:
			frappe.throw(_("This item cannot be rejected"))

		self.status = "Rejected"
		self.reviewed_by = frappe.session.user
		self.reviewed_at = now_datetime()
		if reason:
			self.rejection_reason = reason
		self.save()

		frappe.msgprint(_("Item rejected."), indicator="orange", alert=True)

	@frappe.whitelist()
	def retry(self):
		"""Reset a failed item back to Pending so it can be re-approved and re-published"""
		if self.status != "Failed":
			frappe.throw(_("Only failed items can be retried"))

		self.status = "Pending"
		self.reviewed_by = None
		self.reviewed_at = None
		self.save(ignore_permissions=True)
		frappe.db.commit()

		frappe.msgprint(_("Item reset to Pending. Approve and publish again."), indicator="blue", alert=True)

	@frappe.whitelist()
	def publish(self):
		"""Publish approved item - create/update the actual record"""
		if self.status != "Approved":
			frappe.throw(_("Only approved items can be published"))

		try:
			data = json.loads(self.full_data)

			if self.sync_direction == "Incoming (Live → Local)":
				# Create or update local record from remote data
				self._create_or_update_local(data)
			else:
				# Push local record to remote
				self._push_to_remote(data)

			self.status = "Published"
			self.save()

			# Log success
			frappe.get_doc({
				"doctype": "Sync Log",
				"sync_type": "Manual",
				"doctype_name": self.reference_doctype,
				"document_name": self.reference_name,
				"status": "Success",
				"records_synced": 1
			}).insert(ignore_permissions=True)

			frappe.msgprint(_("Record published successfully!"), indicator="green", alert=True)

		except Exception as e:
			self.status = "Failed"
			self.save()

			# Log error
			frappe.get_doc({
				"doctype": "Sync Log",
				"sync_type": "Error",
				"doctype_name": self.reference_doctype,
				"document_name": self.reference_name,
				"status": "Failed",
				"error_message": str(e)
			}).insert(ignore_permissions=True)

			frappe.throw(_("Failed to publish: {0}").format(str(e)))

	def _create_or_update_local(self, data):
		"""Create or update local record"""
		from frappe.utils.nestedset import rebuild_tree

		doctype = self.reference_doctype
		name = self.reference_name or data.get("name")

		# Remove fields that must not be copied from live.
		# lft/rgt/old_parent are nestedset positions local to each server.
		fields_to_remove = [
			"docstatus", "idx", "owner", "creation", "modified", "modified_by",
			"lft", "rgt", "old_parent"
		]
		for field in fields_to_remove:
			data.pop(field, None)

		meta = frappe.get_meta(doctype)
		is_tree = bool(meta.get("is_tree"))
		parent_field = f"parent_{frappe.scrub(doctype)}" if is_tree else None

		if is_tree and parent_field:
			self._sync_tree_doctype(doctype, name, data, parent_field)
			rebuild_tree(doctype, parent_field)
		else:
			if frappe.db.exists(doctype, name):
				doc = frappe.get_doc(doctype, name)
				doc.update(data)
				doc.flags.ignore_permissions = True
				doc.save()
			else:
				data["doctype"] = doctype
				if name:
					data["name"] = name
				doc = frappe.get_doc(data)
				doc.flags.ignore_permissions = True
				doc.insert()

		frappe.db.commit()

	def _sync_tree_doctype(self, doctype, name, data, parent_field):
		"""
		Sync a nestedset (tree) doctype record without triggering validate_loop errors.

		The problem: NestedSet.on_update() → update_nsm() → update_move_node() calls
		validate_loop() using the LOCAL lft/rgt values. When the local tree structure
		differs from live, a valid parent appears to be a descendant, causing:
		  - "Item cannot be added to its own descendants"  (on UPDATE via on_update hook)
		  - "Multiple root nodes not allowed"              (on INSERT via before_insert hook)

		ignore_validate=True only skips validate() — it does NOT skip on_update or
		before_insert, so it cannot fix these errors.

		Solution for UPDATE: write fields directly via frappe.db.set_value, bypassing
		all ORM hooks, then call rebuild_tree to recalculate lft/rgt from scratch.

		Solution for INSERT: ensure parent_field is populated (to pass the before_insert
		root-node check), then insert normally with ignore_validate + ignore_mandatory.
		After insert, update_nsm sees no parent change so update_move_node is not called.
		"""
		if frappe.db.exists(doctype, name):
			# Direct DB write — bypasses on_update → NestedSet.on_update → update_nsm
			# → update_move_node → validate_loop entirely.
			update_fields = {k: v for k, v in data.items() if k not in ("name", "doctype")}
			if update_fields:
				frappe.db.set_value(doctype, name, update_fields, update_modified=False)
		else:
			# Ensure parent is set — NestedSet.before_insert() throws
			# "Multiple root nodes not allowed" when parent_field is empty and a root exists.
			if not data.get(parent_field):
				existing_root = frappe.db.get_value(
					doctype, {parent_field: ("in", ["", None])}, "name"
				)
				if existing_root and name != existing_root:
					data[parent_field] = existing_root

			data["doctype"] = doctype
			if name:
				data["name"] = name
			doc = frappe.get_doc(data)
			doc.flags.ignore_permissions = True
			doc.flags.ignore_mandatory = True
			doc.flags.ignore_validate = True
			doc.insert()
			# After insert: update_nsm sees get_db_value(parent) == get(parent)
			# → no parent change → update_move_node is NOT called → no validate_loop error

	def _push_to_remote(self, data):
		"""Push record to remote server"""
		import requests

		settings = frappe.get_single("Sync Settings")

		if not settings.remote_url or not settings.api_key:
			frappe.throw(_("Remote server not configured in Sync Settings"))

		headers = {
			"Authorization": f"token {settings.api_key}:{settings.get_password('api_secret')}",
			"Content-Type": "application/json"
		}

		endpoint = f"{settings.remote_url}/api/resource/{self.reference_doctype}"

		# Check if exists on remote
		check_url = f"{endpoint}/{self.reference_name}"
		response = requests.get(check_url, headers=headers, timeout=30)

		if response.status_code == 200:
			# Update existing
			response = requests.put(check_url, json=data, headers=headers, timeout=30)
		else:
			# Create new
			response = requests.post(endpoint, json=data, headers=headers, timeout=30)

		if response.status_code not in [200, 201]:
			raise Exception(f"Remote API error: {response.text}")


@frappe.whitelist()
def approve_item(name):
	"""Approve a sync queue item"""
	doc = frappe.get_doc("Sync Queue", name)
	doc.approve()
	return {"status": "approved"}


@frappe.whitelist()
def reject_item(name, reason=None):
	"""Reject a sync queue item"""
	doc = frappe.get_doc("Sync Queue", name)
	doc.reject(reason)
	return {"status": "rejected"}


@frappe.whitelist()
def publish_item(name):
	"""Publish a sync queue item"""
	doc = frappe.get_doc("Sync Queue", name)
	doc.publish()
	return {"status": "published"}


@frappe.whitelist()
def bulk_approve(names=None, doc=None):
	"""Approve multiple items"""
	# Guard: called from a single-doc form by mistake — approve the single doc
	if names is None:
		if doc:
			doc_data = json.loads(doc) if isinstance(doc, str) else doc
			doc_name = doc_data.get("name")
			if doc_name:
				return approve_item(doc_name)
		frappe.throw(_("No items specified for bulk approve"))

	if isinstance(names, str):
		names = json.loads(names)

	approved = 0
	for name in names:
		try:
			doc = frappe.get_doc("Sync Queue", name)
			if doc.status == "Pending":
				doc.approve()
				approved += 1
		except Exception:
			pass

	frappe.msgprint(_("{0} items approved").format(approved), indicator="green", alert=True)
	return {"approved": approved}


@frappe.whitelist()
def bulk_publish(names=None, doc=None):
	"""Publish multiple approved items"""
	# Guard: called from a single-doc form by mistake — use publish_item instead
	if names is None:
		if doc:
			doc_data = json.loads(doc) if isinstance(doc, str) else doc
			doc_name = doc_data.get("name")
			if doc_name:
				return publish_item(doc_name)
		frappe.throw(_("No items specified for bulk publish"))

	if isinstance(names, str):
		names = json.loads(names)

	published = 0
	failed = 0

	for name in names:
		try:
			doc = frappe.get_doc("Sync Queue", name)
			if doc.status == "Approved":
				doc.publish()
				published += 1
		except Exception:
			failed += 1

	frappe.msgprint(
		_("{0} items published, {1} failed").format(published, failed),
		indicator="green" if failed == 0 else "orange",
		alert=True
	)
	return {"published": published, "failed": failed}


@frappe.whitelist()
def bulk_reject(names=None, reason=None, doc=None):
	"""Reject multiple items"""
	# Guard: called from a single-doc form by mistake — reject the single doc
	if names is None:
		if doc:
			doc_data = json.loads(doc) if isinstance(doc, str) else doc
			doc_name = doc_data.get("name")
			if doc_name:
				return reject_item(doc_name, reason)
		frappe.throw(_("No items specified for bulk reject"))

	if isinstance(names, str):
		names = json.loads(names)

	rejected = 0
	for name in names:
		try:
			doc = frappe.get_doc("Sync Queue", name)
			if doc.status in ["Pending", "Approved"]:
				doc.status = "Rejected"
				doc.reviewed_by = frappe.session.user
				doc.reviewed_at = frappe.utils.now_datetime()
				if reason:
					doc.rejection_reason = reason
				doc.save(ignore_permissions=True)
				rejected += 1
		except Exception:
			pass

	frappe.db.commit()
	frappe.msgprint(_("{0} items rejected").format(rejected), indicator="orange", alert=True)
	return {"rejected": rejected}


@frappe.whitelist()
def bulk_retry(names=None, doc=None):
	"""Reset failed items back to Pending"""
	if names is None:
		if doc:
			doc_data = json.loads(doc) if isinstance(doc, str) else doc
			doc_name = doc_data.get("name")
			if doc_name:
				names = [doc_name]
		if not names:
			frappe.throw(_("No items specified for retry"))

	if isinstance(names, str):
		names = json.loads(names)

	retried = 0
	for name in names:
		try:
			doc = frappe.get_doc("Sync Queue", name)
			if doc.status == "Failed":
				doc.status = "Pending"
				doc.reviewed_by = None
				doc.reviewed_at = None
				doc.save(ignore_permissions=True)
				retried += 1
		except Exception:
			pass

	frappe.db.commit()
	frappe.msgprint(_("{0} items reset to Pending").format(retried), indicator="blue", alert=True)
	return {"retried": retried}


@frappe.whitelist()
def get_pending_count():
	"""Get count of pending items"""
	return frappe.db.count("Sync Queue", {"status": "Pending"})
