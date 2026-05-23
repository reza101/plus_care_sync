# Copyright (c) 2024, Pluscare Team and contributors
# For license information, please see license.txt

import os
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
	"Core", "Custom", "Desk", "Social", "Geo",
	"Data Migration", "Bot", "Integrations", "Patch",
}

# Doctypes that must always be synced even when their module is in _SYSTEM_MODULES.
# User lives in Core (a system module) but is needed for user/permission sync.
# Translation lives in Core but is needed for UI language sync.
_EXPLICIT_INCLUDE_DOCTYPES = {
	"User",
	"Translation",
	"Employee",
	"POS Profile",
	# Core module — stores per-user and global defaults (default_company, currency …)
	# Without this, Purchase Invoice / POS have no default company after a full sync.
	"DefaultValue",
	# Core module — needed as link targets for User
	"Role",
	"Role Profile",
	# Geo module — referenced by Company, Address, Customer, Supplier, and
	# almost every financial transaction; excluded by default but critical data
	"Currency",
	"Country",
	# Core module — optional language field on Customer, Supplier, Lead, Print Format
	"Language",
	# Desk module — stores per-user UI preferences (column configs, list settings)
	"DocType User Settings",
	# Config/schema doctypes that must be explicitly included because their modules
	# (Core, Custom, Desk) are in _SYSTEM_MODULES.
	"Property Setter",
	"Custom Field",
	"Client Script",
	"Server Script",
	"Scheduled Job Type",
	"Workspace",
	"Workspace Link",
	"Workspace Chart",
	"Workspace Shortcut",
	"Workspace Quick List",
	"Dashboard",
	"Dashboard Chart",
	"Number Card",
}

# Single doctypes that must never be synced — only this app's own config
# is excluded so that overwriting local connection settings doesn't break sync.
_SINGLE_SYNC_EXCLUDE = {
	"Sync Settings",          # this app's own config
	"Security Settings",      # does not exist in Frappe v15; live returns HTTP 500
	"System Health Report",   # all child tables are is_virtual=1 — no DB tables exist
	"Payment Reconciliation", # all child tables are is_virtual=1 — UI tool, not stored data
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
	"Department",       # tree; Employee.department; referenced by Sales Person
	"Sales Person",     # tree; used in sales transactions; links to Department
	"Cost Center",      # tree; depends on Company abbr
	"Account",          # tree; depends on Company abbr
	"Warehouse",        # tree; depends on Company abbr
	# ── Level 2: core masters ──────────────────────────────────────────────
	"Language",         # Core module but explicitly included; Customer/Supplier/Lead language field
	"Letter Head",      # required by invoices — must exist before transactions
	"Print Format",     # referenced by doctypes for default print format
	"Mode of Payment",  # referenced by POS Profile payments child table, Payment Entry
	"Price List",       # referenced by Item Price, POS Profile
	"Item",             # referenced by all stock/sales/purchase doctypes
	"Item Price",       # depends on Item + Price List
	"Customer",         # referenced by sales transactions
	"Supplier",         # referenced by purchase transactions
	"Contact",          # links to Customer / Supplier
	"Address",          # links to Customer / Supplier
	"Designation",      # referenced by Employee
	"Branch",           # referenced by Employee
	"Employee",         # references Designation, Branch; referenced by HR doctypes
	"Role",             # Core module but explicitly included; referenced by User (Has Role child table)
	"Role Profile",     # Core module but explicitly included; referenced by User.role_profile_name
	"User",             # Core module but explicitly included; linked to Employee and permissions
	# ── Level 3: transaction support ───────────────────────────────────────
	"Payment Terms Template",
	"Terms and Conditions",
	"Tax Category",
	"Sales Taxes and Charges Template",
	"Purchase Taxes and Charges Template",
	"POS Profile",
	"Shipping Rule",
	# ── Level 4: reference data ─────────────────────────────────────────────
	"Translation",      # Core module but explicitly included for UI language sync
	# ── Level 5: ERPNext extended masters ────────────────────────────────────
	"Holiday List",             # referenced by Employee, Leave Type
	"Bank",                     # referenced by Bank Account
	"Bank Account",             # referenced by Payment Entry, Journal Entry
	"Currency Exchange",        # referenced by transactions with multi-currency
	"Incoterm",                 # referenced by Sales/Purchase Order
	"Item Manufacturer",        # references Item; referenced by BOM
	"Product Bundle",           # references Item; used in Sales transactions
	"Putaway Rule",             # references Warehouse; used in Stock Entry
	"Quality Inspection Template",  # referenced by Quality Inspection
	"Quality Procedure",        # referenced by Quality Inspection
	"Asset Category",           # referenced by Asset
	"Location",                 # referenced by Asset
	"Warehouse Type",           # referenced by Warehouse
	"Cost of Poor Quality Report", # Quality module master
	"Lead Source",              # referenced by Lead, Opportunity
	"Opportunity Type",         # referenced by Opportunity
	"Sales Stage",              # referenced by Opportunity (CRM)
	"Industry Type",            # referenced by Lead, Customer
	# ── Level 6: ERPNext manufacturing masters ───────────────────────────────
	"Operation",                # referenced by BOM Operation, Workstation
	"Workstation Type",         # referenced by Workstation
	"Workstation",              # references Operation, Workstation Type; referenced by BOM, Job Card
	"Routing",                  # references Operation/Workstation; referenced by BOM, Work Order
	"BOM",                      # references Item, Routing; referenced by Work Order, Stock Entry
	# ── Level 7: ERPNext project & asset masters ─────────────────────────────
	"Project Type",             # referenced by Project
	"Project",                  # references Project Type; referenced by Task, Timesheet, Expense Claim
	"Asset Maintenance Template",   # referenced by Asset Maintenance
	# ── Level 8: ERPNext CRM pipeline ────────────────────────────────────────
	"Lead",                     # referenced by Opportunity, Quotation
	"Opportunity",              # references Lead; referenced by Quotation
	# ── Level 9: ERPNext sales & purchase orders ─────────────────────────────
	"Quotation",                # references Opportunity, Customer; referenced by Sales Order
	"Sales Order",              # references Customer, Quotation; referenced by Delivery Note, Sales Invoice
	"Purchase Order",           # references Supplier; referenced by Purchase Receipt, Purchase Invoice
	"Material Request",         # referenced by Purchase Order, Stock Entry
	"Supplier Quotation",       # references Supplier; referenced by Purchase Order
	"Subcontracting Order",     # references Purchase Order; referenced by Subcontracting Receipt
	# ── Level 9.5: Serial/Batch tracking — must precede all stock transactions ──
	"Serial and Batch Bundle",  # referenced by Stock Entry, Stock Reconciliation, SLE, invoices
	# ── Level 10: ERPNext fulfillment & stock ────────────────────────────────
	"Delivery Note",            # references Sales Order, Customer
	"Purchase Receipt",         # references Purchase Order, Supplier
	"Stock Entry",              # references Warehouse, Item, BOM (when type=Manufacture)
	"Stock Reconciliation",     # references Warehouse, Item
	"Landed Cost Voucher",      # references Purchase Receipt
	"Subcontracting Receipt",   # references Subcontracting Order
	"Packing Slip",             # references Delivery Note
	# ── Level 11: ERPNext invoicing & accounting ─────────────────────────────
	"Sales Invoice",            # references Sales Order, Customer
	"Purchase Invoice",         # references Purchase Order, Supplier
	"Payment Entry",            # references Bank Account, Mode of Payment
	"Journal Entry",            # references Account, Bank Account
	"Payment Request",          # references Sales Invoice / Purchase Invoice
	"Dunning",                  # references Sales Invoice
	"POS Invoice",              # references POS Profile, Customer
	"POS Opening Entry",        # references POS Profile
	"POS Closing Entry",        # references POS Opening Entry
	# ── Level 12: ERPNext manufacturing production ───────────────────────────
	"Work Order",               # references BOM, Item, Routing
	"Job Card",                 # references Work Order, Operation, Workstation
	"BOM Update Batch",         # references BOM
	# ── Level 13: ERPNext asset & maintenance transactions ───────────────────
	"Asset",                    # references Asset Category, Location, Supplier
	"Asset Maintenance",        # references Asset, Asset Maintenance Template
	"Asset Movement",           # references Asset, Location
	"Asset Capitalization",     # references Asset
	"Asset Repair",             # references Asset
	# ── Level 14: ERPNext support, quality & projects ────────────────────────
	"Issue",                    # references Customer
	"Quality Inspection",       # references Item, Quality Inspection Template
	"Task",                     # references Project
	"Timesheet",                # references Project, Employee
	# ── Level 15: HRMS — foundations (no HRMS-to-HRMS dependencies) ─────────
	"Employment Type",          # referenced by Employee
	"Employee Grade",           # referenced by Employee
	"Skill",                    # referenced by Employee Skill, Designation Skill
	"KRA",                      # referenced by Appraisal Goal, Appraisal Template Goal
	"Grievance Type",           # referenced by Employee Grievance
	"Identification Document Type",  # referenced by Employee documents
	"Shift Type",               # referenced by Shift Assignment, Employee Checkin
	"Leave Type",               # referenced by Leave Allocation, Leave Application, Leave Policy
	"Leave Block List",         # referenced by Leave Type
	"Expense Claim Type",       # referenced by Expense Claim Detail
	"Salary Component",         # referenced by Salary Structure, Salary Detail, Additional Salary
	"Income Tax Slab",          # referenced by Payroll Period
	"Appraisal Template",       # referenced by Appraisal, Appraisal Cycle
	"Interview Type",           # referenced by Interview Round
	"Job Opening Template",     # referenced by Job Opening
	"Job Offer Term Template",  # referenced by Job Offer Term
	"Training Program",         # referenced by Training Event
	"Appointment Letter Template",  # referenced by Appointment Letter
	"Employee Feedback Criteria",   # referenced by Employee Performance Feedback
	# ── Level 16: HRMS — secondary masters ──────────────────────────────────
	"Leave Policy",             # references Leave Type; referenced by Leave Policy Assignment
	"Leave Period",             # referenced by Leave Allocation, Leave Policy Assignment
	"Payroll Period",           # references Income Tax Slab; referenced by Payroll Entry, Salary Slip
	"Salary Structure",         # references Salary Component; referenced by Salary Structure Assignment, Salary Slip
	"Interview Round",          # references Interview Type; referenced by Interview
	"Appraisal Cycle",          # references Appraisal Template; referenced by Appraisal, Appraisee
	"Staffing Plan",            # referenced by Job Requisition
	"Shift Schedule",           # references Shift Type; referenced by Shift Schedule Assignment
	"Employee Onboarding Template",   # referenced by Employee Onboarding
	"Employee Separation Template",   # referenced by Employee Separation
	# ── Level 17: HRMS — job pipeline ───────────────────────────────────────
	"Job Opening",              # references Staffing Plan; referenced by Job Applicant, Job Requisition
	"Job Applicant",            # references Job Opening; referenced by Interview, Job Offer
	"Interview",                # references Job Applicant, Interview Round; referenced by Interview Feedback
	"Interview Feedback",       # references Interview
	"Job Offer",                # references Job Applicant; referenced by Employee Onboarding
	# ── Level 18: HRMS — HR transactions (depend on Employee + HRMS masters) ──
	"Attendance",               # references Employee, Shift Type
	"Attendance Request",       # references Employee
	"Employee Checkin",         # references Employee, Shift Type
	"Shift Assignment",         # references Employee, Shift Type
	"Shift Schedule Assignment",# references Employee, Shift Schedule
	"Shift Request",            # references Employee, Shift Type
	"Leave Allocation",         # references Employee, Leave Type, Leave Period
	"Leave Policy Assignment",  # references Employee, Leave Policy, Leave Period
	"Leave Application",        # references Employee, Leave Type
	"Compensatory Leave Request","Leave Encashment",
	"Leave Ledger Entry",
	"Expense Claim",            # references Employee, Expense Claim Type
	"Employee Advance",         # references Employee
	"Travel Request",           # references Employee
	"Employee Promotion",       # references Employee
	"Employee Transfer",        # references Employee
	"Employee Separation",      # references Employee, Employee Separation Template
	"Employee Onboarding",      # references Employee, Job Offer, Employee Onboarding Template
	"Employee Grievance",       # references Employee, Grievance Type
	"Employee Referral",        # references Employee
	"Employee Health Insurance","Employee Skill Map",
	"Exit Interview",
	# ── Level 19: HRMS — payroll transactions ───────────────────────────────
	"Salary Structure Assignment",  # references Employee, Salary Structure
	"Payroll Entry",            # references Payroll Period
	"Salary Slip",              # references Employee, Salary Structure, Payroll Entry
	"Additional Salary",        # references Employee, Salary Component
	"Employee Incentive",       # references Employee, Salary Component
	"Retention Bonus",          # references Employee, Salary Component
	"Employee Benefit Application","Employee Benefit Claim",
	"Gratuity",
	"Salary Withholding",
	"Full and Final Statement",
	"Employee Tax Exemption Declaration",
	"Employee Tax Exemption Proof Submission",
	# ── Level 20: HRMS — appraisal & training ───────────────────────────────
	"Appraisal",                # references Employee, Appraisal Cycle
	"Appraisee",                # references Employee, Appraisal Cycle
	"Employee Performance Feedback",  # references Employee, Appraisal
	"Training Event",           # references Training Program
	"Training Result",          # references Training Event
	"Vehicle Log",
	# ── Level 21: Plus Care Pharmacy — masters ──────────────────────────────
	"Loyalty Level",    # referenced by Loyalty Program, Loyalty Reward, Customer Mission
	"Loyalty Program",  # referenced by Loyalty Reward, Loyalty Point Entry
	"Delivery Zone",    # referenced by Delivery Order
	"Courier",          # referenced by Delivery Order
	# ── Level 22: Plus Care Pharmacy — secondary masters ────────────────────
	"Loyalty Reward",           # depends on Loyalty Program, Loyalty Level
	"Customer Mission",         # depends on Loyalty Level
	"Medical Prescription",     # depends on Customer, Item
	# ── Level 23: Plus Care Pharmacy — transactional doctypes ───────────────
	"Treatment Plan",           # depends on Customer, Medical Prescription
	"Customer Mission Progress", # depends on Customer, Customer Mission
	"Customer Reward",          # depends on Customer, Loyalty Reward
	"Loyalty Point Entry",      # depends on Customer, Loyalty Program
	"POS Shift",                # depends on POS Profile, Employee
	"POS Wallet Transaction",   # depends on Customer
	"Delivery Order",           # depends on Sales Order, Courier, Delivery Zone
]

# Config/schema doctypes that must always be pulled from live in Full Database
# mode regardless of the sync direction setting.  These hold configuration that
# should mirror live exactly (naming series, custom fields, scripts, layouts).
_ALWAYS_PULL_FROM_LIVE = {
	"Property Setter",
	"Custom Field",
	"Client Script",
	"Server Script",
	"Scheduled Job Type",
	"DocType User Settings",
	"Translation",
	"Notification",
	"Workspace", "Workspace Link", "Workspace Chart",
	"Workspace Shortcut", "Workspace Quick List",
	"Dashboard", "Dashboard Chart", "Number Card",
}

# Individual doctypes always excluded regardless of module — covers stragglers
# from modules that are partly business (e.g. Email, Workflow) and our own
# app internals that must never be pushed/pulled.
_EXCLUDED_DOCTYPES = {
	# This app's own internals
	"Sync Log", "Sync Queue", "Sync Settings", "Sync DocType",
	# Audit / system logs — site-specific noise, never sync
	"Version", "Access Log", "Route History", "Error Log",
	"Error Snapshot", "Scheduled Job Log", "Activity Log",
	"Deleted Document", "Log Settings", "Background Job Log",
	"Asset Activity", "Notification Log",
	# Core schema — syncing these would overwrite the local schema
	"DocType", "DocField", "DocPerm", "Custom DocPerm",
	"DocType Action", "DocType Link", "DocType State",
	# Job queue runtime — site-specific
	"RQ Job", "RQ Worker",
	# Onboarding UI — low value, no business data
	"Form Tour", "Onboarding Step", "Onboarding Permission", "Module Onboarding",
	# Site-specific state
	"Patch Log", "Process Subscription",
	# Stock engine internals — recalculated automatically, never sync directly
	"Bin",
	# File is handled exclusively by sync_files() which downloads actual binary
	# content. Letting the main loop process File creates DB records without
	# file content, then sync_files() skips them because db.exists() is True.
	"File",
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
			# so only actual business data is synced. Doctypes in _EXPLICIT_INCLUDE_DOCTYPES
			# bypass the module filter (e.g. User and Translation are in Core module).
			all_doctypes = frappe.get_all(
				"DocType",
				filters={"issingle": 0, "istable": 0},
				fields=["name", "module"],
			)
			doctypes = [
				d.name for d in all_doctypes
				if (d.module not in _SYSTEM_MODULES or d.name in _EXPLICIT_INCLUDE_DOCTYPES)
				and d.name not in _EXCLUDED_DOCTYPES
				and frappe.db.exists("DocType", d.name)
			]
		else:
			# Selective modules
			if self.settings.sync_sales:
				# Letter Head is required to print Sales Invoices — sync it automatically
				doctypes.extend(["Letter Head", "Sales Order", "Sales Invoice", "Quotation"])

			if self.settings.sync_purchase:
				# Letter Head is required to print Purchase Invoices — sync it automatically
				doctypes.extend(["Letter Head", "Purchase Order", "Purchase Invoice", "Purchase Receipt"])

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

			if self.settings.sync_printing:
				doctypes.extend(["Letter Head", "Print Format", "Print Style"])

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
			if not frappe.db.table_exists(doctype):
				return 0
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

			doc_dict = json.dumps(doc.as_dict(), default=str)

			if response.status_code == 200:
				# Update existing document
				response = requests.put(
					endpoint,
					data=doc_dict,
					headers=self.get_headers(),
					timeout=30
				)
			else:
				# Create new document
				endpoint = f"{self.remote_url}/api/resource/{encoded_doctype}"
				response = requests.post(
					endpoint,
					data=doc_dict,
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
			if response.status_code == 404:
				# Doctype doesn't exist on remote (custom app not installed there) — skip silently
				return
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
			if not frappe.db.table_exists(doctype):
				return 0
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

			# Frappe's list API omits child table rows entirely. For doctypes that
			# have child tables (e.g. Mode of Payment → accounts, POS Profile →
			# payments) we must fetch each document individually so that child rows
			# are included in the payload.
			has_child_tables = any(f.fieldtype == "Table" for f in meta.fields)

			synced_count = 0
			needs_tree_rebuild = None
			found_any = False

			for record in self._iter_remote_records(endpoint, base_params, batch_size, full_sync):
				found_any = True
				if record.get("name") in pushed:
					continue  # We just pushed this — don't pull it back
				try:
					if has_child_tables:
						encoded_name = quote(record["name"])
						doc_resp = requests.get(
							f"{self.remote_url}/api/resource/{encoded_doctype}/{encoded_name}",
							headers=self.get_headers(),
							timeout=30
						)
						if doc_resp.status_code == 200:
							record = doc_resp.json().get("data", record)
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
		newer schema that hasn't been applied locally yet. Recurses into child table rows.
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
		cleaned = {k: v for k, v in data.items() if k in allowed}

		# Strip unknown fields from child table rows too — the remote may have
		# custom or newer fields in child doctypes that don't exist locally.
		for field in meta.fields:
			if field.fieldtype == "Table" and field.fieldname in cleaned:
				child_doctype = field.options
				cleaned[field.fieldname] = [
					self._strip_unknown_fields(child_doctype, row)
					for row in (cleaned[field.fieldname] or [])
					if isinstance(row, dict)
				]

		return cleaned

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

			# Never overwrite records that must stay local (e.g. Administrator user).
			# These records are also protected from deletion by _PRESERVE_RECORDS.
			if name in _PRESERVE_RECORDS.get(doctype, []):
				return

			# Some doctypes declare is_tree=1 (e.g. Employee) but the actual DB
			# table was never migrated with NestedSet columns (lft, rgt, parent_*).
			# Verify the column exists before taking the tree code path; if not,
			# fall back to the standard document save.
			if is_tree and parent_field:
				db_cols = {row[0] for row in frappe.db.sql(f"SHOW COLUMNS FROM `tab{doctype}`")}
				if parent_field not in db_cols:
					is_tree = False
					parent_field = None

			if is_tree and parent_field:
				# Determine if we should update based on conflict resolution
				should_update = True
				if frappe.db.exists(doctype, name):
					if self.settings.data_type == "Full Database":
						should_update = self.settings.sync_direction == "Live to Local (One Way)"
					elif self.settings.conflict_resolution == "Latest Timestamp Wins":
						local_modified = frappe.db.get_value(doctype, name, "modified")
						remote_modified = remote_data.get("modified")
						r_dt = _parse_dt(remote_modified)
						l_dt = _parse_dt(local_modified)
						if r_dt and l_dt and r_dt <= l_dt:
							should_update = False
					elif self.settings.conflict_resolution == "Local Server Wins":
						should_update = False

				if should_update:
					if frappe.db.exists(doctype, name):
						# Direct DB write bypasses on_update → NestedSet.on_update →
						# update_nsm → update_move_node → validate_loop
						update_fields = {k: v for k, v in remote_data.items()
							if k not in ("name", "doctype")}
						if update_fields:
							frappe.db.set_value(doctype, name, update_fields, update_modified=False)
						# set_value silently drops `creation` — enforce via raw SQL.
						if remote_data.get("creation"):
							frappe.db.sql(
								f"UPDATE `tab{doctype}` SET creation = %s WHERE name = %s",
								(remote_data["creation"], name)
							)
						frappe.db.commit()
					else:
						# Use raw db_insert (via _write_doc_directly) instead of
						# doc.insert() to bypass ERPNext controller hooks — Account.validate(),
						# NestedSet.validate_loop(), etc. — that fail on an empty or
						# partially-populated tree. rebuild_tree() after the batch fixes
						# all lft/rgt values so the hierarchy is always consistent.
						self._write_doc_directly(doctype, name, remote_data, is_new=True)

					if skip_rebuild:
						return (doctype, parent_field)

					from frappe.utils.nestedset import rebuild_tree
					rebuild_tree(doctype, parent_field)
					frappe.db.commit()

			else:
				if frappe.db.exists(doctype, name):
					# Full Database mode overrides the conflict resolution setting:
					#   Live→Local  → live always wins (mirror live onto local)
					#   Local→Live  → local always wins (don't overwrite local data)
					if self.settings.data_type == "Full Database":
						should_update = self.settings.sync_direction == "Live to Local (One Way)"
					elif self.settings.conflict_resolution == "Live Server Wins":
						should_update = True
					elif self.settings.conflict_resolution == "Latest Timestamp Wins":
						remote_modified = remote_data.get("modified")
						local_modified = frappe.db.get_value(doctype, name, "modified")
						r_dt = _parse_dt(remote_modified)
						l_dt = _parse_dt(local_modified)
						should_update = bool(r_dt and l_dt and r_dt > l_dt)
					else:
						should_update = False

					if should_update:
						self._write_doc_directly(doctype, name, remote_data, is_new=False)
				else:
					self._write_doc_directly(doctype, name, remote_data, is_new=True)

		except Exception as e:
			raise Exception(f"Failed to update local record: {str(e)}")

	def _write_doc_directly(self, doctype, name, data, is_new):
		"""Write a document via raw DB operations, bypassing ALL Frappe hooks and validation.

		Uses pure SQL INSERT/UPDATE so that controller hooks (before_insert, after_insert,
		validate, etc.) never execute.  This is critical after clear_local_data() because
		ERPNext controllers (e.g. Company.after_insert creates chart-of-accounts,
		Account.validate checks parent existence) fail on an empty database.
		"""
		meta = frappe.get_meta(doctype)
		table_fields = {f.fieldname: f.options for f in meta.fields if f.fieldtype == "Table"}

		data = dict(data)
		# Pull child table rows out so they are handled separately
		child_data = {fn: (data.pop(fn, None) or []) for fn in table_fields}

		if is_new:
			# Restrict to columns that actually exist in the DB table so we never
			# hit "Unknown column" errors from version-mismatched or custom fields.
			db_cols = {row[0] for row in frappe.db.sql(f"SHOW COLUMNS FROM `tab{doctype}`")}
			row = {k: v for k, v in data.items() if k in db_cols}
			row.setdefault("name", name)
			row.setdefault("owner", frappe.session.user or "Administrator")
			row.setdefault("modified_by", frappe.session.user or "Administrator")
			row.setdefault("docstatus", 0)
			cols = list(row.keys())
			vals = [row[c] for c in cols]
			col_expr = ", ".join(f"`{c}`" for c in cols)
			placeholders = ", ".join(["%s"] * len(cols))
			frappe.db.sql(
				f"INSERT INTO `tab{doctype}` ({col_expr}) VALUES ({placeholders})",
				vals
			)
			# Force original timestamps — the INSERT may have applied DB defaults.
			if data.get("creation") or data.get("modified"):
				frappe.db.sql(
					f"UPDATE `tab{doctype}` SET creation = %s, modified = %s WHERE name = %s",
					(data.get("creation"), data.get("modified"), name)
				)
		else:
			top_fields = {k: v for k, v in data.items() if k not in ("name", "doctype")}
			if top_fields:
				frappe.db.set_value(doctype, name, top_fields, update_modified=False)
			# frappe.db.set_value may silently skip creation — enforce via raw SQL.
			if data.get("creation"):
				frappe.db.sql(
					f"UPDATE `tab{doctype}` SET creation = %s WHERE name = %s",
					(data["creation"], name)
				)

		# Replace child table rows via direct SQL (no document object — no hooks)
		for fieldname, child_doctype in table_fields.items():
			rows = child_data.get(fieldname) or []
			frappe.db.delete(child_doctype, {
				"parent": name,
				"parenttype": doctype,
				"parentfield": fieldname,
			})
			child_db_cols = {row[0] for row in frappe.db.sql(f"SHOW COLUMNS FROM `tab{child_doctype}`")}
			for idx, row in enumerate(rows, 1):
				row = dict(row)
				row.update({
					"parent": name,
					"parenttype": doctype,
					"parentfield": fieldname,
					"idx": idx,
				})
				if not row.get("name"):
					row["name"] = frappe.generate_hash(length=10)
				child_row = {k: v for k, v in row.items() if k in child_db_cols}
				child_row.setdefault("name", frappe.generate_hash(length=10))
				c_cols = list(child_row.keys())
				c_vals = [child_row[c] for c in c_cols]
				c_col_expr = ", ".join(f"`{c}`" for c in c_cols)
				c_placeholders = ", ".join(["%s"] * len(c_cols))
				frappe.db.sql(
					f"INSERT INTO `tab{child_doctype}` ({c_col_expr}) VALUES ({c_placeholders})",
					c_vals
				)

		frappe.db.commit()

	def push_single_to_remote(self, doctype):
		"""Push a Single doctype to the remote server."""
		try:
			doc = frappe.get_single(doctype)
			data = doc.as_dict()
			encoded = quote(doctype)
			endpoint = f"{self.remote_url}/api/resource/{encoded}/{encoded}"
			response = requests.put(
				endpoint,
				json=data,
				headers=self.get_headers(),
				timeout=30
			)
			if response.status_code not in [200, 201]:
				raise Exception(f"Remote API error: {response.text}")
		except Exception as e:
			raise Exception(f"Failed to push single doctype {doctype} to remote: {str(e)}")

	def pull_single_from_remote(self, doctype):
		"""Pull a Single doctype from the remote server and apply locally."""
		try:
			encoded = quote(doctype)
			endpoint = f"{self.remote_url}/api/resource/{encoded}/{encoded}"
			response = requests.get(endpoint, headers=self.get_headers(), timeout=30)
			if response.status_code != 200:
				raise Exception(f"Remote API error (HTTP {response.status_code}): {response.text}")
			remote_data = response.json().get("data", {})
			if not remote_data:
				return
			remote_data = self._strip_unknown_fields(doctype, remote_data)
			skip_fields = {"name", "doctype", "modified", "modified_by", "creation", "owner", "docstatus"}

			meta = frappe.get_meta(doctype)
			# Include Table MultiSelect — stored in child tables just like Table fields
			table_fields = {f.fieldname: f.options for f in meta.fields
							if f.fieldtype in ("Table", "Table MultiSelect")}

			# Separate scalar fields (→ tabSingles) from child table fields (→ child tables).
			# Also skip any value that is a list/dict — those are unmapped child data coming
			# from the remote and would cause a SQL type error if written as a scalar.
			scalar_fields = {k: v for k, v in remote_data.items()
							 if k not in skip_fields and k not in table_fields
							 and not isinstance(v, (list, dict))}
			child_table_data = {fn: remote_data[fn] for fn in table_fields if fn in remote_data}

			if not scalar_fields and not child_table_data:
				return

			# Clear only after confirming valid data exists — deleting before this
			# check left tabSingles empty when remote returned no writable fields.
			frappe.db.sql("DELETE FROM `tabSingles` WHERE doctype = %s", doctype)

			# frappe.db.get_singles_dict() returns only tabSingles rows. Without a
			# 'name' row, JS stores the doc at locals[doctype][undefined] instead of
			# locals[doctype][doctype_name], making frappe.model.get_doc() return
			# undefined and crashing the form toolbar.
			frappe.db.set_value(doctype, None, "name", doctype, update_modified=False)

			for fieldname, value in scalar_fields.items():
				try:
					frappe.db.set_value(doctype, None, fieldname, value, update_modified=False)
				except Exception as e:
					self.log_sync_error(doctype, doctype, f"field '{fieldname}': {str(e)}")

			for fieldname, child_doctype in table_fields.items():
				if fieldname not in child_table_data:
					continue
				rows = child_table_data[fieldname] or []
				try:
					if not frappe.db.table_exists(child_doctype):
						# Virtual child table (is_virtual=1) — no DB table; skip silently
						continue
					frappe.db.delete(child_doctype, {
						"parent": doctype,
						"parenttype": doctype,
						"parentfield": fieldname,
					})
					child_db_cols = {r[0] for r in frappe.db.sql(f"SHOW COLUMNS FROM `tab{child_doctype}`")}
					for idx, row in enumerate(rows, 1):
						row = dict(row)
						row.update({"parent": doctype, "parenttype": doctype,
									"parentfield": fieldname, "idx": idx})
						if not row.get("name"):
							row["name"] = frappe.generate_hash(length=10)
						child_row = {k: v for k, v in row.items() if k in child_db_cols}
						child_row.setdefault("name", frappe.generate_hash(length=10))
						c_cols = list(child_row.keys())
						c_col_expr = ", ".join(f"`{c}`" for c in c_cols)
						c_placeholders = ", ".join(["%s"] * len(c_cols))
						frappe.db.sql(
							f"INSERT INTO `tab{child_doctype}` ({c_col_expr}) VALUES ({c_placeholders})",
							[child_row[c] for c in c_cols]
						)
				except Exception as e:
					self.log_sync_error(doctype, doctype, f"child table '{fieldname}': {str(e)}")

			frappe.db.commit()
		except Exception as e:
			raise Exception(f"Failed to pull single doctype {doctype} from remote: {str(e)}")

	def sync_erp_settings(self):
		"""Sync all Single doctypes (settings, defaults, etc.). Returns count of successful syncs."""
		all_singles = frappe.get_all("DocType", filters={"issingle": 1}, fields=["name"])
		direction = self.settings.sync_direction
		synced = 0
		for d in all_singles:
			doctype = d.name
			if doctype in _SINGLE_SYNC_EXCLUDE:
				continue
			try:
				if direction in ("Local to Live (One Way)", "Bidirectional (Two Way)"):
					self.push_single_to_remote(doctype)
				if direction in ("Live to Local (One Way)", "Bidirectional (Two Way)"):
					self.pull_single_from_remote(doctype)
				synced += 1
			except Exception as e:
				self.log_sync_error(doctype, doctype, str(e))
		return synced

	def sync_naming_series(self):
		"""Pull tabSeries counters from the remote server using the standard
		Frappe REST API — no custom endpoint required on the live server.

		Only runs in Live→Local or Bidirectional direction so local counters
		never overwrite the live server's authoritative sequence numbers.
		"""
		direction = self.settings.sync_direction
		if direction not in ("Live to Local (One Way)", "Bidirectional (Two Way)"):
			return 0
		try:
			# Series is a standard Frappe doctype backed by tabSeries —
			# accessible on any Frappe site without additional apps.
			endpoint = f"{self.remote_url}/api/resource/Series"
			batch_size = 500
			start = 0
			total = 0
			while True:
				response = requests.get(
					endpoint,
					params={
						"fields": '["name","current"]',
						"limit_page_length": batch_size,
						"limit_start": start,
					},
					headers=self.get_headers(),
					timeout=30,
				)
				# Series is a raw table — not REST-accessible on all Frappe versions.
				# Skip gracefully; counter sync requires plus_care_sync on live server.
				if response.status_code in (404, 417):
					self.log_sync_info("tabSeries", "Skipped: Series is not accessible via REST API on live server.")
					return 0
				if response.status_code != 200:
					raise Exception(f"HTTP {response.status_code}: {response.text[:200]}")
				rows = response.json().get("data", [])
				if not rows:
					break
				for row in rows:
					name = row.get("name")
					current = row.get("current")
					if not name:
						continue
					exists = frappe.db.sql("SELECT name FROM `tabSeries` WHERE name = %s", name)
					if exists:
						frappe.db.sql("UPDATE `tabSeries` SET current = %s WHERE name = %s", (current, name))
					else:
						frappe.db.sql("INSERT INTO `tabSeries` (name, current) VALUES (%s, %s)", (name, current))
				total += len(rows)
				if len(rows) < batch_size:
					break
				start += batch_size
			frappe.db.commit()
			return total
		except Exception as e:
			self.log_sync_error("tabSeries", None, str(e))
			return 0

	# Records that must never be deleted during a full clear — removing them
	# would break the local site's authentication and admin access.
	_PRESERVE_RECORDS = {
		"User": ["Administrator", "Guest"],
	}

	def clear_local_data(self, doctypes):
		"""Delete all local records for the given doctypes before a full Live→Local sync.

		Runs with FOREIGN_KEY_CHECKS=0 so deletion order does not matter.
		Child table rows are deleted per-parent-doctype to avoid orphans.
		Records listed in _PRESERVE_RECORDS are kept so the local site stays functional.
		"""
		try:
			frappe.db.sql("SET FOREIGN_KEY_CHECKS = 0")
			for doctype in doctypes:
				try:
					preserve = self._PRESERVE_RECORDS.get(doctype, [])
					meta = frappe.get_meta(doctype)
					for field in meta.fields:
						if field.fieldtype == "Table" and field.options:
							if preserve:
								placeholders = ", ".join(["%s"] * len(preserve))
								frappe.db.sql(
									f"DELETE FROM `tab{field.options}` WHERE parenttype = %s"
									f" AND parent NOT IN ({placeholders})",
									[doctype] + preserve,
								)
							else:
								frappe.db.sql(
									f"DELETE FROM `tab{field.options}` WHERE parenttype = %s",
									doctype,
								)
					if preserve:
						placeholders = ", ".join(["%s"] * len(preserve))
						frappe.db.sql(
							f"DELETE FROM `tab{doctype}` WHERE name NOT IN ({placeholders})",
							preserve,
						)
					else:
						frappe.db.sql(f"DELETE FROM `tab{doctype}`")
				except Exception as e:
					frappe.log_error(
						f"clear_local_data: failed to clear {doctype}: {str(e)}",
						"Plus Care Sync"
					)
		finally:
			frappe.db.sql("SET FOREIGN_KEY_CHECKS = 1")
			frappe.db.commit()

		self.log_sync_info(
			"clear_local_data",
			f"Cleared {len(doctypes)} doctypes before full Live to Local sync."
		)

	def sync_files(self, doctypes_synced):
		"""Sync File records (attachments + item images) for all doctypes that were synced."""
		direction = self.settings.sync_direction
		try:
			if direction in ("Live to Local (One Way)", "Bidirectional (Two Way)"):
				self._pull_files_from_remote(doctypes_synced)
			if direction in ("Local to Live (One Way)", "Bidirectional (Two Way)"):
				self._push_files_to_remote(doctypes_synced)
		except Exception as e:
			self.log_sync_error("File", None, str(e))

	def _pull_files_from_remote(self, doctypes_synced):
		"""Download all File records from remote (attachments, standalone, folders)."""
		try:
			# Full Database initial sync (no last_sync_time): clear existing File
			# records first so we get an exact mirror of live. System folders are
			# preserved because Frappe creates them automatically on every site.
			if self.settings.data_type == "Full Database" and not self.settings.last_sync_time:
				preserve_names = ", ".join(f"'{f}'" for f in self._SYSTEM_FOLDERS)
				frappe.db.sql(f"DELETE FROM `tabFile` WHERE name NOT IN ({preserve_names})")
				frappe.db.commit()

			endpoint = f"{self.remote_url}/api/resource/File"
			batch_size = int(self.settings.batch_size or 50)

			# No attached_to_doctype filter — pull every File record so standalone
			# files, folders, and externally-linked files all land locally.
			filters = []
			if self.settings.last_sync_time:
				filters.append(["modified", ">", str(self.settings.last_sync_time)])

			start = 0
			while True:
				params = {
					"fields": '["name","file_name","file_url","folder","attached_to_doctype","attached_to_name","is_private","is_folder","modified"]',
					"limit_page_length": batch_size,
					"limit_start": start,
				}
				if filters:
					params["filters"] = json.dumps(filters)
				response = requests.get(endpoint, params=params, headers=self.get_headers(), timeout=30)
				if response.status_code != 200:
					break
				records = response.json().get("data", [])
				if not records:
					break
				for file_record in records:
					try:
						self._save_remote_file_locally(file_record)
					except Exception as e:
						self.log_sync_error("File", file_record.get("name"), str(e))
				if len(records) < batch_size:
					break
				start += batch_size
		except Exception as e:
			self.log_sync_error("File", None, f"Pull files failed: {str(e)}")

	# System folders that exist on every Frappe site — never re-create them.
	_SYSTEM_FOLDERS = {"Home", "Home/Attachments", "Home/Attachments/"}

	def _save_remote_file_locally(self, file_record):
		"""Save one remote File record locally — handles folders, external URLs, and stored files."""
		file_name = file_record.get("file_name", "")
		file_url = file_record.get("file_url", "")
		is_folder = int(file_record.get("is_folder") or 0)
		folder = file_record.get("folder") or "Home"
		remote_name = file_record.get("name", "")

		if not file_name:
			return

		# Skip standard system folders that exist on every site.
		if is_folder and remote_name in self._SYSTEM_FOLDERS:
			return

		# Skip if already exists locally by primary key.
		if remote_name and frappe.db.exists("File", remote_name):
			return

		# Fallback duplicate check by file_name + folder + context.
		existing = frappe.db.get_value("File", {
			"file_name": file_name,
			"folder": folder,
			"attached_to_doctype": file_record.get("attached_to_doctype") or "",
			"attached_to_name": file_record.get("attached_to_name") or "",
		}, "name")
		if existing:
			return

		base = {
			"doctype": "File",
			"file_name": file_name,
			"folder": folder,
			"attached_to_doctype": file_record.get("attached_to_doctype") or "",
			"attached_to_name": file_record.get("attached_to_name") or "",
			"is_private": int(file_record.get("is_private") or 0),
			"is_folder": is_folder,
		}

		if is_folder:
			# Ensure parent folder exists before creating child folder.
			if folder and folder not in self._SYSTEM_FOLDERS and not frappe.db.exists("File", folder):
				return  # parent not yet synced; skip — it will be retried next sync
			file_doc = frappe.get_doc(base)
			file_doc.flags.ignore_links = True
			file_doc.insert(ignore_permissions=True)
			frappe.db.commit()
			return

		if not file_url:
			return

		# Build full download URL: prepend remote origin for server-relative paths,
		# use as-is for absolute external URLs (http/https).
		download_url = (
			self.remote_url.rstrip("/") + file_url
			if file_url.startswith("/")
			else file_url
		)

		resp = requests.get(download_url, headers=self.get_headers(), timeout=60)
		if resp.status_code != 200:
			return

		base["content"] = resp.content
		file_doc = frappe.get_doc(base)
		file_doc.flags.ignore_links = True
		file_doc.insert(ignore_permissions=True)
		frappe.db.commit()

	def _push_files_to_remote(self, doctypes_synced):
		"""Upload all local File records and binary content to remote."""
		try:
			batch_size = int(self.settings.batch_size or 50)
			# No attached_to_doctype filter — push every local file including
			# standalone files and folders so the remote File Manager stays in sync.
			filters = {"is_folder": 0}
			if self.settings.last_sync_time:
				filters["modified"] = [">", self.settings.last_sync_time]

			start = 0
			while True:
				file_records = frappe.get_all(
					"File",
					filters=filters,
					fields=["name", "file_name", "file_url", "attached_to_doctype",
							"attached_to_name", "is_private"],
					limit_page_length=batch_size,
					limit_start=start,
				)
				if not file_records:
					break
				for file_record in file_records:
					try:
						self._upload_file_to_remote(file_record)
					except Exception as e:
						self.log_sync_error("File", file_record.get("name"), str(e))
				if len(file_records) < batch_size:
					break
				start += batch_size
		except Exception as e:
			self.log_sync_error("File", None, f"Push files failed: {str(e)}")

	def _upload_file_to_remote(self, file_record):
		"""Upload one local file to the remote Frappe server."""
		file_url = file_record.get("file_url", "")
		if not file_url:
			return

		site_path = frappe.get_site_path()
		if file_url.startswith("/files/"):
			file_path = os.path.join(site_path, "public", file_url.lstrip("/"))
		elif file_url.startswith("/private/files/"):
			file_path = os.path.join(site_path, file_url.lstrip("/"))
		else:
			return

		if not os.path.exists(file_path):
			return

		# Skip if the remote already has a file with this name attached to the same doc
		check_resp = requests.get(
			f"{self.remote_url.rstrip('/')}/api/resource/File",
			params={
				"filters": json.dumps([
					["file_name", "=", file_record.get("file_name")],
					["attached_to_doctype", "=", file_record.get("attached_to_doctype", "")],
					["attached_to_name", "=", file_record.get("attached_to_name", "")],
				]),
				"fields": '["name"]',
				"limit_page_length": 1,
			},
			headers=self.get_headers(),
			timeout=30,
		)
		if check_resp.status_code == 200 and check_resp.json().get("data"):
			return

		with open(file_path, "rb") as f:
			file_content = f.read()

		upload_url = f"{self.remote_url.rstrip('/')}/api/method/upload_file"
		headers = {"Authorization": f"token {self.api_key}:{self.api_secret}"}
		response = requests.post(
			upload_url,
			files={"file": (file_record.get("file_name"), file_content)},
			data={
				"is_private": str(int(file_record.get("is_private", 0))),
				"attached_to_doctype": file_record.get("attached_to_doctype", ""),
				"attached_to_name": file_record.get("attached_to_name", ""),
			},
			headers=headers,
			timeout=60,
		)
		if response.status_code not in [200, 201]:
			raise Exception(f"Upload failed: {response.text[:200]}")

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

		if settings.sync_status == "Syncing":
			return {"status": "error", "message": "Sync already in progress"}

		# Update status
		frappe.db.set_value("Sync Settings", "Sync Settings", "sync_status", "Syncing")
		frappe.db.commit()

		engine = SyncEngine()
		doctypes = engine.get_doctypes_to_sync()

		# Full Database + Live→Local + no last_sync_time (initial/reset full sync):
		# wipe all local data first so the local DB becomes an exact mirror of live.
		# Skip on incremental runs (last_sync_time is set) — clearing then only
		# pulling recently-changed records would discard all older data.
		if (
			settings.data_type == "Full Database"
			and settings.sync_direction == "Live to Local (One Way)"
			and not settings.last_sync_time
		):
			engine.clear_local_data(doctypes)

		total_synced = 0

		for doctype in doctypes:
			try:
				sync_type = "Manual" if settings.sync_mode == "Manual" else "Automatic"
				is_full = settings.data_type == "Full Database"
				force_pull = is_full and doctype in _ALWAYS_PULL_FROM_LIVE

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

					# Config doctypes always pulled from live even in Local→Live mode
					if force_pull:
						pulled = engine.pull_from_remote(doctype)
						total_synced += pulled
						frappe.get_doc({
							"doctype": "Sync Log",
							"sync_type": sync_type,
							"doctype_name": doctype,
							"status": "Success",
							"records_synced": pulled,
							"sync_details": "Config: always pulled from live"
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

				# Commit after every doctype so logs are visible immediately
				# and a later failure does not roll back earlier work.
				frappe.db.set_value(
					"Sync Settings", "Sync Settings",
					"total_synced_records", total_synced,
					update_modified=False
				)
				frappe.db.commit()

			except Exception as e:
				engine.log_sync_error(doctype, None, str(e))

		# Sync Singles AFTER all regular doctypes so link targets (e.g. Print Style
		# referenced by Print Settings) already exist when Singles are written.
		if settings.sync_erp_settings or settings.data_type == "Full Database":
			settings_synced = engine.sync_erp_settings()
			total_synced += settings_synced
			sync_type = "Manual" if settings.sync_mode == "Manual" else "Automatic"
			frappe.get_doc({
				"doctype": "Sync Log",
				"sync_type": sync_type,
				"doctype_name": "ERP Settings",
				"status": "Success",
				"records_synced": settings_synced,
				"sync_details": "Single/Settings doctypes synced"
			}).insert(ignore_permissions=True)
			frappe.db.commit()

		# Sync attachments and item images after all document doctypes are done
		try:
			engine.sync_files(doctypes)
			sync_type = "Manual" if settings.sync_mode == "Manual" else "Automatic"
			frappe.get_doc({
				"doctype": "Sync Log",
				"sync_type": sync_type,
				"doctype_name": "File",
				"status": "Success",
				"sync_details": "Attachments and item images synced"
			}).insert(ignore_permissions=True)
			frappe.db.commit()
		except Exception as e:
			engine.log_sync_error("File", None, str(e))

		# Sync naming series counters (tabSeries) — live → local only
		try:
			series_count = engine.sync_naming_series()
			if series_count:
				sync_type = "Manual" if settings.sync_mode == "Manual" else "Automatic"
				frappe.get_doc({
					"doctype": "Sync Log",
					"sync_type": sync_type,
					"doctype_name": "tabSeries",
					"status": "Success",
					"records_synced": series_count,
					"sync_details": "Naming series counters synced from live"
				}).insert(ignore_permissions=True)
				frappe.db.commit()
		except Exception as e:
			engine.log_sync_error("tabSeries", None, str(e))

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

		# Raw SQL inserts bypass all Frappe hooks that normally invalidate the
		# Redis document/defaults cache. Clear the entire site cache so the next
		# page load (POS, Purchase Invoice, etc.) reads fresh data from the DB.
		frappe.clear_cache()

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
def get_series_data():
	"""Return all naming series counters from tabSeries. Called by the sync engine on the local side."""
	return frappe.db.sql("SELECT name, current FROM `tabSeries`", as_dict=True)


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


@frappe.whitelist()
def debug_settings_sync():
	"""Debug why ERP settings are not syncing — tests each Single doctype individually."""
	settings = frappe.get_single("Sync Settings")
	api_secret = settings.get_password("api_secret")
	headers = {
		"Authorization": f"token {settings.api_key}:{api_secret}",
		"Content-Type": "application/json"
	}
	all_singles = [d.name for d in frappe.get_all("DocType", filters={"issingle": 1}, fields=["name"])
				   if d.name not in _SINGLE_SYNC_EXCLUDE]
	results = {}
	for doctype in all_singles:
		encoded = quote(doctype)
		url = f"{settings.remote_url.rstrip('/')}/api/resource/{encoded}/{encoded}"
		try:
			r = requests.get(url, headers=headers, timeout=15)
			data = r.json() if r.status_code == 200 else {}
			results[doctype] = {
				"http_status": r.status_code,
				"has_data": bool(data.get("data")),
				"error": r.text[:300] if r.status_code != 200 else None
			}
		except Exception as e:
			results[doctype] = {"http_status": "exception", "error": str(e)}
	return results
