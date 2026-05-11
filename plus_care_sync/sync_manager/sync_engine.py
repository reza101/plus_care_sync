# Copyright (c) 2024, Pluscare Team and contributors
# For license information, please see license.txt

import frappe
import requests
import json
from frappe import _
from datetime import datetime
from urllib.parse import quote

# Frappe modules that are pure infrastructure — they contain system config,
# audit logs, and schema definitions, not business data. Excluded from Full
# Database sync so we don't flood the queue with irrelevant noise.
_SYSTEM_MODULES = {
	"Core", "Custom", "Desk", "Printing", "Social", "Geo",
	"Data Migration", "Bot", "Integrations", "Patch",
}

# Sync order matters: foundational doctypes must arrive before the records that
# reference them.  Company is most critical — its abbr rename cascades to
# Warehouse, Account, and Cost Center names.  Everything not listed here syncs
# after all priority doctypes, in arbitrary order.
_SYNC_PRIORITY = [
	# ── Level 0: true foundations ──────────────────────────────────────────
	"Company",          # abbr drives Warehouse / Account / Cost Center names
	"Currency",         # referenced by Company, Price List, transactions
	"Country",          # referenced by Company, Address
	"UOM Category",     # parent of UOM
	"UOM",              # referenced by Item, Stock Entry
	# ── Level 1: master trees (parent nodes before child records) ──────────
	"Item Group",       # tree; Item.item_group → Item Group
	"Customer Group",   # tree; Customer.customer_group
	"Supplier Group",   # tree; Supplier.supplier_group
	"Territory",        # tree; Customer.territory, Sales Order.territory
	"Sales Person",     # tree; used in sales transactions
	"Department",       # tree; Employee.department
	"Cost Center",      # tree; depends on Company abbr
	"Account",          # tree; depends on Company abbr
	"Warehouse",        # tree; depends on Company abbr
	# ── Level 2: core masters ──────────────────────────────────────────────
	"Price List",       # referenced by Item Price, POS Profile
	"Item",             # referenced by all stock/sales/purchase doctypes
	"Item Price",       # depends on Item + Price List
	"Customer",         # referenced by sales transactions
	"Supplier",         # referenced by purchase transactions
	"Contact",          # links to Customer / Supplier
	"Address",          # links to Customer / Supplier
	"Employee",         # referenced by HR doctypes
	"Designation",      # referenced by Employee
	"Branch",           # referenced by Employee
	# ── Level 3: transaction support ───────────────────────────────────────
	"Payment Terms Template",
	"Terms and Conditions",
	"Tax Category",
	"Sales Taxes and Charges Template",
	"Purchase Taxes and Charges Template",
	"POS Profile",
	"Shipping Rule",
]

# Individual doctypes always excluded regardless of module — covers stragglers
# from modules that are partly business (e.g. Email, Workflow) and our own
# app internals that must never be pushed/pulled.
_EXCLUDED_DOCTYPES = {
	# This app's own internals
	"Sync Log", "Sync Queue", "Sync Settings", "Sync DocType",
	# Audit / system logs
	"Version", "Access Log", "Route History", "Error Log",
	"Error Snapshot", "Scheduled Job Log", "Activity Log",
	"Deleted Document", "Log Settings", "Background Job Log",
	# Schema / meta
	"DocType", "DocField", "DocPerm", "Custom DocPerm",
	"Property Setter", "Custom Field", "Client Script",
	"Server Script", "DocType Action", "DocType Link", "DocType State",
	# Job queue
	"RQ Job", "RQ Worker", "Scheduled Job Type",
	# Web / onboarding UI
	"Form Tour", "Onboarding Step", "Onboarding Permission",
	"Module Onboarding", "Web Template", "Web Template Field",
	"Workspace", "Workspace Link", "Workspace Chart",
	"Workspace Shortcut", "Workspace Quick List",
	# Misc system
	"Patch Log", "Process Subscription", "Translation",
	"Number Card", "Dashboard", "Dashboard Chart",
	"Notification", "Notification Log",
}


def _parse_dt(value):
	"""Coerce a datetime, date, or ISO string to a datetime object for safe comparison."""
	if value is None:
		return None
	if isinstance(value, datetime):
		return value
	# Normalise ISO 'T' separator to space so all formats below match consistently
	s = str(value).replace("T", " ")
	for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
		try:
			return datetime.strptime(s, fmt)
		except ValueError:
			continue
	return None


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
			# Exclude system/infrastructure modules and individual system doctypes
			# so only actual business data is synced.
			all_doctypes = frappe.get_all(
				"DocType",
				filters={"issingle": 0, "istable": 0},
				fields=["name", "module"],
			)
			doctypes = [
				d.name for d in all_doctypes
				if d.module not in _SYSTEM_MODULES
				and d.name not in _EXCLUDED_DOCTYPES
			]
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

		# Deduplicate then sort: priority doctypes first (in declared order),
		# everything else appended after in stable order.
		unique = list(dict.fromkeys(doctypes))  # preserves insertion order, drops dupes
		priority_set = {d: i for i, d in enumerate(_SYNC_PRIORITY)}
		high = [d for d in _SYNC_PRIORITY if d in set(unique)]
		rest = [d for d in unique if d not in priority_set]
		return high + rest

	def _iter_local_records(self, doctype, filters, batch_size, full_sync):
		"""Yield local records one page at a time.

		In incremental mode (full_sync=False) only one page is fetched.
		In full-sync mode pages are fetched lazily until exhausted so memory
		usage stays constant at one batch regardless of doctype size.
		"""
		start = 0
		while True:
			page = frappe.get_all(
				doctype,
				filters=filters,
				fields=["name", "modified"],
				order_by="modified desc",
				limit_start=start,
				limit_page_length=batch_size,
			)
			if not page:
				break
			yield from page
			if not full_sync or len(page) < batch_size:
				break
			start += batch_size

	def sync_doctype(self, doctype):
		"""Sync a specific doctype"""
		try:
			batch_size = int(self.settings.batch_size or 50)
			filters = {}
			if self.settings.last_sync_time:
				filters["modified"] = [">", self.settings.last_sync_time]

			# Full Database with no last_sync_time = initial full sync → page through all.
			# Incremental (last_sync_time set) → one batch is enough (only changed records).
			full_sync = (
				self.settings.data_type == "Full Database"
				and not self.settings.last_sync_time
			)

			synced_count = 0

			for record in self._iter_local_records(doctype, filters, batch_size, full_sync):
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

	def _iter_remote_records(self, endpoint, base_params, batch_size, full_sync):
		"""Yield remote records one page at a time.

		In incremental mode (full_sync=False) a single API call is made.
		In full-sync mode pages are fetched lazily with limit_start so only
		one batch lives in memory at a time regardless of how large the doctype is.
		"""
		start = 0
		while True:
			params = dict(base_params, limit_start=start)
			response = requests.get(endpoint, params=params, headers=self.get_headers(), timeout=30)
			if response.status_code != 200:
				raise Exception(f"Failed to pull from remote (HTTP {response.status_code}): {response.text}")
			page = response.json().get("data", [])
			if not page:
				break
			yield from page
			if not full_sync or len(page) < batch_size:
				break
			start += batch_size

	def pull_from_remote(self, doctype):
		"""Pull documents from remote server"""
		try:
			# URL encode doctype name (e.g., "Item Group" -> "Item%20Group")
			encoded_doctype = quote(doctype)
			endpoint = f"{self.remote_url}/api/resource/{encoded_doctype}"

			batch_size = int(self.settings.batch_size or 50)
			# Tree doctypes (Account, Warehouse, Cost Center, Item Group …) must be
			# fetched root-first so every parent exists before its children are inserted.
			meta = frappe.get_meta(doctype)
			order_by = "lft asc" if meta.get("is_tree") else "modified desc"
			base_params = {
				"fields": '["*"]',
				"limit_page_length": batch_size,
				"order_by": order_by
			}

			# FIX 2: Only pull records changed since last sync (incremental pull)
			if self.settings.last_sync_time:
				base_params["filters"] = json.dumps([["modified", ">", str(self.settings.last_sync_time)]])

			# Full Database with no last_sync_time = initial full sync → page through all.
			# When last_sync_time is not set it is an initial full sync regardless
			# of data_type — page through ALL remote records so no records are missed.
			# When last_sync_time is set (incremental) one batch is enough because
			# only recently changed records are returned.
			full_sync = not self.settings.last_sync_time

			# FIX 1: Skip records we already pushed this session (loop prevention)
			pushed = self._pushed_this_session.get(doctype, set())

			synced_count = 0
			needs_tree_rebuild = None
			found_any = False

			for record in self._iter_remote_records(endpoint, base_params, batch_size, full_sync):
				found_any = True
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

			if not found_any:
				self.log_sync_info(doctype, "No records found to sync. Total: 0")

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
		except Exception:
			frappe.log_error(frappe.get_traceback(), "Plus Care Sync - log_sync_info failed")

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
		except Exception:
			frappe.log_error(frappe.get_traceback(), "Plus Care Sync - log_fetch failed")

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
						r_dt = _parse_dt(remote_modified)
						l_dt = _parse_dt(local_modified)
						if r_dt and l_dt and r_dt <= l_dt:
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
							frappe.db.commit()
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
						frappe.db.commit()

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
						r_dt = _parse_dt(remote_modified)
						l_dt = _parse_dt(local_doc.modified)
						if r_dt and l_dt and r_dt > l_dt:
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
		except Exception:
			frappe.log_error(frappe.get_traceback(), "Plus Care Sync - log_sync_error failed")


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
