# Copyright (c) 2024, Pluscare Team and contributors
# For license information, please see license.txt

import frappe
import requests
import json
from frappe import _
from datetime import datetime
from urllib.parse import quote


class SyncEngine:
	"""Core sync engine for Plus Care Sync"""

	def __init__(self):
		self.settings = frappe.get_single("Sync Settings")
		self.remote_url = self.settings.remote_url
		self.api_key = self.settings.api_key
		self.api_secret = self.settings.get_password("api_secret")
		# Use queue for Manual mode, direct sync for Automatic
		self.use_queue = self.settings.sync_mode == "Manual"
		# FIX 1: Track outgoing names per doctype to prevent pulling them back
		# in the same bidirectional sync run (loop prevention)
		self._pushed_this_session = {}  # {doctype: set(names)}

	def get_headers(self):
		"""Get API headers for remote requests"""
		return {
			"Authorization": f"token {self.api_key}:{self.api_secret}",
			"Content-Type": "application/json"
		}

	def get_doctypes_to_sync(self):
		"""Get list of doctypes to sync based on settings"""
		doctypes = []

		if self.settings.data_type == "Full Database":
			# Get all doctypes (you may want to filter system doctypes)
			doctypes = frappe.get_all("DocType", filters={"issingle": 0, "istable": 0}, pluck="name")
		else:
			# Selective modules
			if self.settings.sync_sales:
				doctypes.extend(["Sales Order", "Sales Invoice", "Quotation"])

			if self.settings.sync_purchase:
				doctypes.extend(["Purchase Order", "Purchase Invoice", "Purchase Receipt"])

			if self.settings.sync_stock:
				doctypes.extend(["Stock Entry", "Delivery Note", "Material Request"])

			if self.settings.sync_accounting:
				doctypes.extend(["Payment Entry", "Journal Entry", "GL Entry"])

			if self.settings.sync_customers:
				doctypes.extend(["Customer", "Supplier", "Contact", "Address"])

			if self.settings.sync_items:
				doctypes.extend(["Item", "Item Group", "Item Price", "Price List"])

			if self.settings.sync_hr:
				doctypes.extend(["Employee", "Salary Slip", "Attendance", "Leave Application"])

			# Add custom doctypes
			if self.settings.sync_custom_doctypes:
				for row in self.settings.sync_custom_doctypes:
					if row.enabled:
						doctypes.append(row.doctype_name)

		return list(set(doctypes))  # Remove duplicates

	def sync_doctype(self, doctype):
		"""Sync a specific doctype"""
		try:
			batch_size = int(self.settings.batch_size or 50)
			filters = {}
			if self.settings.last_sync_time:
				filters["modified"] = [">", self.settings.last_sync_time]

			# Fetch up to batch_size records, newest first so recent changes are prioritized
			records = frappe.get_all(
				doctype,
				filters=filters,
				fields=["name", "modified"],
				order_by="modified desc",
				limit_page_length=batch_size
			)

			synced_count = 0

			for record in records:
				try:
					doc = frappe.get_doc(doctype, record.name)

					if self.settings.sync_direction in ["Local to Live (One Way)", "Bidirectional (Two Way)"]:
						if self.use_queue:
							self.add_to_queue(doctype, doc.as_dict(), "Outgoing (Local → Live)")
						else:
							self.push_to_remote(doc)

					# FIX 1: Mark this record as pushed so pull_from_remote skips it
					self._pushed_this_session.setdefault(doctype, set()).add(record.name)
					synced_count += 1

				except Exception as e:
					self.log_sync_error(doctype, record.name, str(e))

			return synced_count

		except Exception as e:
			self.log_sync_error(doctype, None, str(e))
			return 0

	def push_to_remote(self, doc):
		"""Push document to remote server"""
		try:
			# URL encode doctype and name for API call
			encoded_doctype = quote(doc.doctype)
			encoded_name = quote(doc.name)
			endpoint = f"{self.remote_url}/api/resource/{encoded_doctype}/{encoded_name}"

			# Check if document exists on remote
			response = requests.get(endpoint, headers=self.get_headers(), timeout=30)

			doc_dict = doc.as_dict()

			if response.status_code == 200:
				# Update existing document
				response = requests.put(
					endpoint,
					json=doc_dict,
					headers=self.get_headers(),
					timeout=30
				)
			else:
				# Create new document
				endpoint = f"{self.remote_url}/api/resource/{encoded_doctype}"
				response = requests.post(
					endpoint,
					json=doc_dict,
					headers=self.get_headers(),
					timeout=30
				)

			if response.status_code not in [200, 201]:
				raise Exception(f"Remote API error: {response.text}")

		except Exception as e:
			raise Exception(f"Failed to push to remote: {str(e)}")

	def pull_from_remote(self, doctype):
		"""Pull documents from remote server"""
		try:
			# URL encode doctype name (e.g., "Item Group" -> "Item%20Group")
			encoded_doctype = quote(doctype)
			endpoint = f"{self.remote_url}/api/resource/{encoded_doctype}"

			batch_size = int(self.settings.batch_size or 50)
			params = {
				"fields": '["*"]',
				"limit_page_length": batch_size,
				"order_by": "modified desc"
			}

			# FIX 2: Only pull records changed since last sync (incremental pull)
			if self.settings.last_sync_time:
				params["filters"] = json.dumps([["modified", ">", str(self.settings.last_sync_time)]])

			response = requests.get(
				endpoint,
				params=params,
				headers=self.get_headers(),
				timeout=30
			)

			if response.status_code != 200:
				raise Exception(f"Failed to pull from remote (HTTP {response.status_code}): {response.text}")

			data = response.json()
			records = data.get("data", [])

			if not records:
				self.log_sync_info(doctype, "No records found to sync. Total: 0")
				return 0

			# FIX 1: Skip records we already pushed this session (loop prevention)
			pushed = self._pushed_this_session.get(doctype, set())

			synced_count = 0
			needs_tree_rebuild = None  # (doctype, parent_field) if any tree records were written
			for record in records:
				if record.get("name") in pushed:
					continue  # We just pushed this — don't pull it back
				try:
					if self.use_queue:
						self.add_to_queue(doctype, record, "Incoming (Live → Local)")
					else:
						tree_info = self.update_local_record(doctype, record, skip_rebuild=True)
						if tree_info:
							needs_tree_rebuild = tree_info
					synced_count += 1
				except Exception as e:
					self.log_sync_error(doctype, record.get("name"), str(e))

			# Rebuild tree once after all records are written, not once per record
			if needs_tree_rebuild:
				from frappe.utils.nestedset import rebuild_tree
				rebuild_tree(needs_tree_rebuild[0], needs_tree_rebuild[1])
				frappe.db.commit()

			return synced_count

		except Exception as e:
			self.log_sync_error(doctype, None, str(e))
			return 0

	def log_sync_info(self, doctype, message):
		"""Log sync information"""
		try:
			frappe.get_doc({
				"doctype": "Sync Log",
				"sync_type": "Manual",
				"doctype_name": doctype,
				"status": "Success",
				"records_synced": 0,
				"sync_details": message
			}).insert(ignore_permissions=True)
			frappe.db.commit()
		except:
			pass

	def add_to_queue(self, doctype, data, direction):
		"""Add record to Sync Queue for review"""
		try:
			# Check if already in queue
			existing = frappe.db.exists("Sync Queue", {
				"reference_doctype": doctype,
				"reference_name": data.get("name"),
				"status": "Pending"
			})

			is_new = False
			if existing:
				# Update existing queue item
				queue_doc = frappe.get_doc("Sync Queue", existing)
				queue_doc.full_data = json.dumps(data, default=str, indent=2)
				queue_doc.save(ignore_permissions=True)
			else:
				is_new = True
				# Create preview of key fields
				preview_fields = ["name", "item_name", "item_code", "customer", "supplier",
								 "customer_name", "total", "grand_total", "status", "title"]
				preview = {k: v for k, v in data.items() if k in preview_fields and v}

				# Check for conflict
				has_conflict = False
				local_modified = None

				if frappe.db.exists(doctype, data.get("name")):
					local_doc = frappe.get_doc(doctype, data.get("name"))
					local_modified = local_doc.modified
					remote_modified = data.get("modified")
					if remote_modified and local_modified:
						has_conflict = True

				# Create queue item
				queue_doc = frappe.get_doc({
					"doctype": "Sync Queue",
					"reference_doctype": doctype,
					"reference_name": data.get("name"),
					"remote_name": data.get("name"),
					"sync_direction": direction,
					"status": "Pending",
					"data_preview": json.dumps(preview, default=str, indent=2),
					"full_data": json.dumps(data, default=str, indent=2),
					"has_conflict": has_conflict,
					"local_modified": local_modified,
					"remote_modified": data.get("modified")
				})
				queue_doc.insert(ignore_permissions=True)

			# Only log when a genuinely new item enters the queue.
			# Re-logging existing pending items on every poll floods the Sync Log.
			if is_new:
				self.log_fetch(doctype, data.get("name"), direction)

			frappe.db.commit()

		except Exception as e:
			self.log_sync_error(doctype, data.get("name"), f"Failed to queue: {str(e)}")

	def log_fetch(self, doctype, docname, direction):
		"""Log a newly queued record to Sync Log"""
		try:
			frappe.get_doc({
				"doctype": "Sync Log",
				"sync_type": "Fetched",
				"doctype_name": doctype,
				"document_name": docname,
				"status": "Queued",
				"records_synced": 1,
				"sync_details": f"Direction: {direction}\nAction: New item added to queue"
			}).insert(ignore_permissions=True)
		except:
			pass

	def _strip_unknown_fields(self, doctype, data):
		"""Return a copy of data containing only fields that exist in the local doctype meta.

		Prevents "Unknown field" errors when the remote server has custom fields or a
		newer schema that hasn't been applied locally yet.
		"""
		meta = frappe.get_meta(doctype)
		known_fields = {f.fieldname for f in meta.fields}
		# Always keep Frappe standard/system columns so inserts/updates stay valid.
		system_fields = {
			"name", "doctype", "owner", "creation", "modified",
			"modified_by", "docstatus", "idx", "parent", "parenttype",
			"parentfield",
		}
		allowed = known_fields | system_fields
		return {k: v for k, v in data.items() if k in allowed}

	def update_local_record(self, doctype, remote_data, skip_rebuild=False):
		"""Update local record with remote data.

		Returns (doctype, parent_field) when skip_rebuild=True and a tree record was
		written, so the caller can issue a single rebuild_tree after the batch.
		"""
		try:
			remote_data = dict(remote_data)

			for field in ("lft", "rgt", "old_parent"):
				remote_data.pop(field, None)

			# Drop any fields that don't exist in the local schema to avoid
			# "Unknown field" errors from custom fields or version mismatches.
			remote_data = self._strip_unknown_fields(doctype, remote_data)

			meta = frappe.get_meta(doctype)
			is_tree = bool(meta.get("is_tree"))
			parent_field = f"parent_{frappe.scrub(doctype)}" if is_tree else None
			name = remote_data.get("name")

			if is_tree and parent_field:
				# Determine if we should update based on conflict resolution
				should_update = True
				if frappe.db.exists(doctype, name):
					if self.settings.conflict_resolution == "Latest Timestamp Wins":
						local_modified = frappe.db.get_value(doctype, name, "modified")
						remote_modified = remote_data.get("modified")
						if remote_modified and str(remote_modified) <= str(local_modified):
							should_update = False
					elif self.settings.conflict_resolution == "Local Server Wins":
						# FIX 3: explicit local-wins — skip update
						should_update = False

				if should_update:
					if frappe.db.exists(doctype, name):
						# Direct DB write bypasses on_update → NestedSet.on_update →
						# update_nsm → update_move_node → validate_loop
						update_fields = {k: v for k, v in remote_data.items()
							if k not in ("name", "doctype")}
						if update_fields:
							frappe.db.set_value(doctype, name, update_fields, update_modified=False)
					else:
						if not remote_data.get(parent_field):
							existing_root = frappe.db.get_value(
								doctype, {parent_field: ("in", ["", None])}, "name"
							)
							if existing_root and name != existing_root:
								remote_data[parent_field] = existing_root

						remote_data["doctype"] = doctype
						doc = frappe.get_doc(remote_data)
						doc.flags.ignore_permissions = True
						doc.flags.ignore_mandatory = True
						doc.flags.ignore_validate = True
						doc.insert()

					if skip_rebuild:
						return (doctype, parent_field)

					from frappe.utils.nestedset import rebuild_tree
					rebuild_tree(doctype, parent_field)
					frappe.db.commit()

			else:
				if frappe.db.exists(doctype, name):
					local_doc = frappe.get_doc(doctype, name)

					should_update = False
					if self.settings.conflict_resolution == "Live Server Wins":
						should_update = True
					elif self.settings.conflict_resolution == "Latest Timestamp Wins":
						remote_modified = remote_data.get("modified")
						if remote_modified and remote_modified > str(local_doc.modified):
							should_update = True
					# FIX 3: "Local Server Wins" or unrecognized value — keep local, skip update
					# (should_update stays False — explicit, not a silent accident)

					if should_update:
						local_doc.update(remote_data)
						local_doc.save(ignore_permissions=True)
						frappe.db.commit()
				else:
					remote_data["doctype"] = doctype
					doc = frappe.get_doc(remote_data)
					doc.insert(ignore_permissions=True)
					frappe.db.commit()

		except Exception as e:
			raise Exception(f"Failed to update local record: {str(e)}")

	def log_sync_error(self, doctype, docname, error):
		"""Log sync errors"""
		try:
			frappe.get_doc({
				"doctype": "Sync Log",
				"sync_type": "Error",
				"doctype_name": doctype,
				"document_name": docname,
				"status": "Failed",
				"error_message": error
			}).insert(ignore_permissions=True)
			frappe.db.commit()
		except:
			pass


@frappe.whitelist()
def execute_sync():
	"""Main sync execution function"""
	try:
		settings = frappe.get_single("Sync Settings")

		if not settings.enable_sync:
			return {"status": "error", "message": "Sync is not enabled"}

		# Update status
		frappe.db.set_value("Sync Settings", "Sync Settings", "sync_status", "Syncing")
		frappe.db.commit()

		engine = SyncEngine()
		doctypes = engine.get_doctypes_to_sync()

		total_synced = 0

		for doctype in doctypes:
			try:
				# Sync based on direction
				sync_type = "Manual" if settings.sync_mode == "Manual" else "Automatic"

				if settings.sync_direction == "Local to Live (One Way)":
					pushed = engine.sync_doctype(doctype)
					total_synced += pushed
					frappe.get_doc({
						"doctype": "Sync Log",
						"sync_type": sync_type,
						"doctype_name": doctype,
						"status": "Success",
						"records_synced": pushed
					}).insert(ignore_permissions=True)

				elif settings.sync_direction == "Live to Local (One Way)":
					pulled = engine.pull_from_remote(doctype)
					total_synced += pulled
					frappe.get_doc({
						"doctype": "Sync Log",
						"sync_type": sync_type,
						"doctype_name": doctype,
						"status": "Success",
						"records_synced": pulled
					}).insert(ignore_permissions=True)

				elif settings.sync_direction == "Bidirectional (Two Way)":
					pushed = engine.sync_doctype(doctype)
					total_synced += pushed
					frappe.get_doc({
						"doctype": "Sync Log",
						"sync_type": sync_type,
						"doctype_name": doctype,
						"status": "Success",
						"records_synced": pushed,
						"sync_details": "Direction: Local → Live"
					}).insert(ignore_permissions=True)

					pulled = engine.pull_from_remote(doctype)
					total_synced += pulled
					frappe.get_doc({
						"doctype": "Sync Log",
						"sync_type": sync_type,
						"doctype_name": doctype,
						"status": "Success",
						"records_synced": pulled,
						"sync_details": "Direction: Live → Local"
					}).insert(ignore_permissions=True)

			except Exception as e:
				engine.log_sync_error(doctype, None, str(e))

		# Update sync status
		frappe.db.set_value("Sync Settings", "Sync Settings", {
			"sync_status": "Success",
			"last_sync_time": datetime.now(),
			"total_synced_records": total_synced
		})

		# Update pending queue count
		pending_count = frappe.db.count("Sync Queue", {"status": "Pending"})
		frappe.db.set_value("Sync Settings", "Sync Settings", "pending_sync_count", pending_count)

		frappe.db.commit()

		# Return message based on mode
		if settings.sync_mode == "Manual":
			return {
				"status": "success",
				"message": f"{total_synced} items added to Sync Queue for review",
				"pending_count": pending_count,
				"use_queue": True
			}
		else:
			return {
				"status": "success",
				"message": f"{total_synced} records synced directly",
				"use_queue": False
			}

	except Exception as e:
		frappe.db.set_value("Sync Settings", "Sync Settings", "sync_status", "Error")
		frappe.db.commit()
		frappe.log_error(f"Sync failed: {str(e)}", "Plus Care Sync Error")
		return {"status": "error", "message": str(e)}


@frappe.whitelist()
def test_fetch_items():
	"""Test function to debug fetching items from remote server"""
	try:
		settings = frappe.get_single("Sync Settings")

		if not settings.remote_url or not settings.api_key:
			return {"status": "error", "message": "Remote URL and API credentials not configured"}

		# Build request
		api_secret = settings.get_password("api_secret")
		headers = {
			"Authorization": f"token {settings.api_key}:{api_secret}",
			"Content-Type": "application/json"
		}

		# Test with Item doctype
		endpoint = f"{settings.remote_url}/api/resource/Item"
		params = {
			"fields": '["name", "item_code", "item_name", "modified"]',
			"limit_page_length": 5
		}

		response = requests.get(endpoint, params=params, headers=headers, timeout=30)

		result = {
			"status_code": response.status_code,
			"url": endpoint,
			"params": params,
		}

		if response.status_code == 200:
			data = response.json()
			result["success"] = True
			result["total_records"] = len(data.get("data", []))
			result["sample_data"] = data.get("data", [])[:3]  # First 3 records
			result["message"] = f"Found {len(data.get('data', []))} items"
		else:
			result["success"] = False
			result["error"] = response.text
			result["message"] = f"HTTP Error {response.status_code}"

		return result

	except Exception as e:
		return {"status": "error", "message": str(e), "success": False}
