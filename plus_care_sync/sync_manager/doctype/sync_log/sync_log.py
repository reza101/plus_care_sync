# Copyright (c) 2024, Pluscare Team and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class SyncLog(Document):
	def before_insert(self):
		"""Generate sync ID before inserting"""
		if not self.sync_id:
			self.sync_id = f"SYNC-{now_datetime().strftime('%Y%m%d-%H%M%S')}-{frappe.generate_hash(length=6)}"
