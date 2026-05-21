# Copyright (c) 2024, Pluscare Team and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
import requests
from frappe import _


class SyncSettings(Document):
	def validate(self):
		"""Validate sync settings before saving"""
		if self.enable_sync:
			# Ensure remote URL is provided
			if not self.remote_url:
				frappe.throw(_("Remote Server URL is required when sync is enabled"))

			# Ensure API credentials are provided
			if not self.api_key or not self.api_secret:
				frappe.throw(_("API Key and Secret are required when sync is enabled"))

			# Validate data selection
			if self.data_type == "Selective Modules":
				has_selection = any([
					self.sync_sales,
					self.sync_purchase,
					self.sync_stock,
					self.sync_accounting,
					self.sync_customers,
					self.sync_items,
					self.sync_hr,
					self.sync_printing,
					self.sync_erp_settings,
					self.sync_custom_doctypes
				])
				if not has_selection:
					frappe.throw(_("Please select at least one module or doctype to sync"))

	def on_update(self):
		"""Setup scheduled jobs when sync is enabled"""
		if self.enable_sync and self.sync_mode == "Automatic":
			self.setup_sync_scheduler()

	def setup_sync_scheduler(self):
		"""Setup background job for automatic sync"""
		frequency_map = {
			"Every 5 Minutes": "300",
			"Every 15 Minutes": "900",
			"Every 30 Minutes": "1800",
			"Every Hour": "3600",
			"Every 6 Hours": "21600",
			"Once Daily": "86400"
		}

		if self.sync_frequency in frequency_map:
			# Create a scheduled job (you can enhance this with frappe scheduler)
			frappe.enqueue(
				"plus_care_sync.sync_manager.doctype.sync_settings.sync_settings.auto_sync",
				queue="long",
				timeout=3600,
				is_async=True
			)

	@frappe.whitelist()
	def test_connection(self):
		"""Test connection to remote server"""
		if not self.remote_url or not self.api_key or not self.api_secret:
			frappe.throw(_("Please provide Remote URL, API Key, and API Secret"))

		try:
			# Get the actual password value (not encrypted)
			api_secret = self.get_password("api_secret")

			# Test connection by calling a simple API endpoint
			headers = {
				"Authorization": f"token {self.api_key}:{api_secret}"
			}

			response = requests.get(
				f"{self.remote_url}/api/method/frappe.auth.get_logged_user",
				headers=headers,
				timeout=10
			)

			if response.status_code == 200:
				self.connection_status = "Connected Successfully"
				frappe.msgprint(_("Connection successful!"), indicator="green", alert=True)
			else:
				self.connection_status = f"Connection Failed: {response.status_code}"
				frappe.msgprint(_("Connection failed. Please check your credentials."), indicator="red", alert=True)

		except Exception as e:
			self.connection_status = f"Error: {str(e)}"
			frappe.msgprint(_("Connection error: {0}").format(str(e)), indicator="red", alert=True)

		self.save()

	@frappe.whitelist()
	def reset_last_sync_time(self):
		"""Kept for backward compatibility — delegates to the module-level function."""
		reset_last_sync_time()

	@frappe.whitelist()
	def sync_now(self):
		"""Manually trigger sync"""
		if not self.enable_sync:
			frappe.throw(_("Please enable sync first"))

		# Update status
		self.sync_status = "Syncing"
		self.save()
		frappe.db.commit()

		# Enqueue sync job
		frappe.enqueue(
			"plus_care_sync.sync_manager.sync_engine.execute_sync",
			queue="long",
			timeout=3600,
			is_async=True
		)

		frappe.msgprint(_("Sync started in background. Check Sync Logs for progress."), indicator="blue", alert=True)

	@frappe.whitelist()
	def view_sync_logs(self):
		"""Redirect to sync logs"""
		frappe.set_route("List", "Sync Log")


@frappe.whitelist()
def reset_last_sync_time():
	"""Clear last sync time and any stuck Syncing status, then trigger a full re-sync.

	Standalone function so the JS can call it via its full module path, avoiding
	the run_doc_method route that requires a serialised document in the request.
	"""
	frappe.db.set_value("Sync Settings", "Sync Settings", {
		"last_sync_time": None,
		"sync_status": "Idle",
	})
	frappe.db.commit()

	frappe.enqueue(
		"plus_care_sync.sync_manager.sync_engine.execute_sync",
		queue="long",
		timeout=3600,
		is_async=True,
	)

	frappe.msgprint(
		_("Sync time cleared. Full re-sync started in background."),
		indicator="blue",
		alert=True
	)


@frappe.whitelist()
def auto_sync():
	"""Automatic sync function called by scheduler"""
	from datetime import datetime, timedelta  # noqa: F811 (shadows module-level import if any)

	settings = frappe.get_single("Sync Settings")

	if not settings.enable_sync:
		return

	if settings.sync_mode == "Automatic":
		frequency_map = {
			"Every 5 Minutes": 300,
			"Every 15 Minutes": 900,
			"Every 30 Minutes": 1800,
			"Every Hour": 3600,
			"Every 6 Hours": 21600,
			"Once Daily": 86400,
		}

		interval = frequency_map.get(settings.sync_frequency, 300)
		now = datetime.now()

		if settings.last_sync_time:
			last = settings.last_sync_time
			if isinstance(last, str):
				# Frappe may store datetimes with ISO 'T' separator — normalise to space
				last = last.replace("T", " ")
				last = datetime.strptime(last, "%Y-%m-%d %H:%M:%S.%f" if "." in last else "%Y-%m-%d %H:%M:%S")
			if (now - last).total_seconds() < interval:
				return  # Not enough time has passed since last sync

		# Distributed lock: prevents two scheduler workers that both passed the
		# elapsed-time check from running concurrently. TTL = interval so the lock
		# auto-expires even if the worker process dies mid-sync.
		lock_key = "plus_care_sync_auto_running"
		if frappe.cache().get_value(lock_key):
			return  # Another instance is already running
		frappe.cache().set_value(lock_key, True, expires_in_sec=interval)

		try:
			from plus_care_sync.sync_manager.sync_engine import execute_sync
			execute_sync()
			# Record when this auto-sync ran so the next invocation can check elapsed time
			frappe.db.set_value("Sync Settings", "Sync Settings", "last_sync_time", now, update_modified=False)
		finally:
			frappe.cache().delete_value(lock_key)

	elif settings.sync_mode == "Scheduled" and settings.scheduled_time:
		# Build today's scheduled datetime from the Time field value.
		# Frappe returns Time fields as datetime.timedelta, not a string.
		now = datetime.now()
		t = settings.scheduled_time
		if isinstance(t, timedelta):
			total_secs = int(t.total_seconds())
			h, rem = divmod(total_secs, 3600)
			m, s = divmod(rem, 60)
		else:
			# Fallback: parse string value
			parsed = datetime.strptime(str(t), "%H:%M:%S")
			h, m, s = parsed.hour, parsed.minute, parsed.second

		scheduled = now.replace(hour=h, minute=m, second=s, microsecond=0)

		# Only fire if within 150 seconds of scheduled time (half the 5-min cron interval)
		# to guarantee the sync runs at most once per scheduled slot.
		if abs((now - scheduled).total_seconds()) <= 150:
			from plus_care_sync.sync_manager.sync_engine import execute_sync
			execute_sync()
