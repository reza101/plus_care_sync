"""
Before-insert hook: automatically applies branch prefix to naming_series
of submittable documents created locally (not from sync).

Fires before Frappe generates the document name, so the generated name
will include the branch prefix (e.g. STE-A-2025-00001 instead of STE-2025-00001).
"""
import frappe


def apply_branch_prefix(doc, method=None):
    if doc.flags.get("from_sync"):
        return  # Synced docs keep their original name

    meta = frappe.get_meta(doc.doctype)
    if not meta.get("is_submittable"):
        return  # Only submittable docs need branch prefix

    naming_series = getattr(doc, "naming_series", None)
    if not naming_series:
        return

    branch_id = frappe.db.get_single_value("Sync Settings", "branch_id")
    if not branch_id:
        return

    marker = f"-{branch_id.upper()}-"
    if marker in naming_series:
        return  # Already prefixed

    # Insert branch_id after the first segment:
    # "STE-.YYYY.-.####"  →  "STE-A-.YYYY.-.####"
    parts = naming_series.split("-", 1)
    if len(parts) == 2:
        doc.naming_series = f"{parts[0]}-{branch_id.upper()}-{parts[1]}"
