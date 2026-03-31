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
		doctype = self.reference_doctype
		name = self.reference_name or data.get("name")

		# Remove fields that shouldn't be copied
		fields_to_remove = ["docstatus", "idx", "owner", "creation", "modified", "modified_by"]
		for field in fields_to_remove:
			data.pop(field, None)

		if frappe.db.exists(doctype, name):
			# Update existing
			doc = frappe.get_doc(doctype, name)
			doc.update(data)
			doc.flags.ignore_permissions = True
			doc.save()
		else:
			# Create new
			data["doctype"] = doctype
			if name:
				data["name"] = name
			doc = frappe.get_doc(data)
			doc.flags.ignore_permissions = True
			doc.insert()

		frappe.db.commit()

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
def bulk_approve(names):
	"""Approve multiple items"""
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
def bulk_publish(names):
	"""Publish multiple approved items"""
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
def bulk_reject(names, reason=None):
	"""Reject multiple items"""
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
def get_pending_count():
	"""Get count of pending items"""
	return frappe.db.count("Sync Queue", {"status": "Pending"})
