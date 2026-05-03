// Copyright (c) 2024, Pluscare Team and contributors
// For license information, please see license.txt

frappe.ui.form.on('Sync Queue', {
	refresh: function(frm) {
		// Add action buttons based on status
		if (frm.doc.status === 'Pending') {
			frm.add_custom_button(__('Approve'), function() {
				frappe.call({
					method: 'approve',
					doc: frm.doc,
					callback: function(r) {
						frm.reload_doc();
					}
				});
			}, __('Actions')).addClass('btn-primary');

			frm.add_custom_button(__('Reject'), function() {
				frappe.prompt([
					{
						label: 'Reason for Rejection',
						fieldname: 'reason',
						fieldtype: 'Small Text'
					}
				], function(values) {
					frappe.call({
						method: 'reject',
						doc: frm.doc,
						args: {
							reason: values.reason
						},
						callback: function(r) {
							frm.reload_doc();
						}
					});
				}, __('Reject Item'), __('Reject'));
			}, __('Actions'));
		}

		if (frm.doc.status === 'Approved') {
			frm.add_custom_button(__('Publish Now'), function() {
				frappe.confirm(
					__('This will create/update the actual record. Continue?'),
					function() {
						frappe.call({
							method: 'publish',
							doc: frm.doc,
							callback: function(r) {
								frm.reload_doc();
							}
						});
					}
				);
			}, __('Actions')).addClass('btn-success');

			frm.add_custom_button(__('Reject'), function() {
				frappe.prompt([
					{
						label: 'Reason for Rejection',
						fieldname: 'reason',
						fieldtype: 'Small Text'
					}
				], function(values) {
					frappe.call({
						method: 'reject',
						doc: frm.doc,
						args: {
							reason: values.reason
						},
						callback: function(r) {
							frm.reload_doc();
						}
					});
				}, __('Reject Item'), __('Reject'));
			}, __('Actions'));
		}

		if (frm.doc.status === 'Failed') {
			frm.add_custom_button(__('Retry (Reset to Pending)'), function() {
				frappe.confirm(
					__('Reset this item back to Pending so it can be re-approved?'),
					function() {
						frappe.call({
							method: 'retry',
							doc: frm.doc,
							callback: function(r) {
								frm.reload_doc();
							}
						});
					}
				);
			}, __('Actions')).addClass('btn-warning');
		}

		// Show status indicator
		if (frm.doc.status === 'Pending') {
			frm.page.set_indicator(__('Pending Review'), 'orange');
		} else if (frm.doc.status === 'Approved') {
			frm.page.set_indicator(__('Ready to Publish'), 'blue');
		} else if (frm.doc.status === 'Published') {
			frm.page.set_indicator(__('Published'), 'green');
		} else if (frm.doc.status === 'Rejected') {
			frm.page.set_indicator(__('Rejected'), 'red');
		} else if (frm.doc.status === 'Failed') {
			frm.page.set_indicator(__('Failed'), 'red');
		}

		// Add link to view the actual document if published
		if (frm.doc.status === 'Published' && frm.doc.reference_name) {
			frm.add_custom_button(__('View Document'), function() {
				frappe.set_route('Form', frm.doc.reference_doctype, frm.doc.reference_name);
			});
		}
	}
});


// Form view only - List view is in sync_queue_list.js

function bulk_action_approve(listview) {
	let selected = listview.get_checked_items();
	if (selected.length === 0) {
		frappe.msgprint(__('Please select items to approve'));
		return;
	}

	let names = selected.map(item => item.name);
	frappe.confirm(
		__('Approve {0} selected items?', [selected.length]),
		function() {
			frappe.call({
				method: 'plus_care_sync.sync_manager.doctype.sync_queue.sync_queue.bulk_approve',
				args: { names: names },
				freeze: true,
				freeze_message: __('Approving...'),
				callback: function(r) {
					listview.refresh();
				}
			});
		}
	);
}

function bulk_action_reject(listview) {
	let selected = listview.get_checked_items();
	if (selected.length === 0) {
		frappe.msgprint(__('Please select items to reject'));
		return;
	}

	let names = selected.map(item => item.name);
	frappe.prompt([
		{
			label: 'Reason for Rejection',
			fieldname: 'reason',
			fieldtype: 'Small Text'
		}
	], function(values) {
		frappe.call({
			method: 'plus_care_sync.sync_manager.doctype.sync_queue.sync_queue.bulk_reject',
			args: { names: names, reason: values.reason },
			freeze: true,
			freeze_message: __('Rejecting...'),
			callback: function(r) {
				listview.refresh();
			}
		});
	}, __('Reject {0} Items', [selected.length]), __('Reject'));
}

function bulk_action_publish(listview) {
	let selected = listview.get_checked_items();
	if (selected.length === 0) {
		frappe.msgprint(__('Please select items to publish'));
		return;
	}

	// Filter only approved items
	let approved = selected.filter(item => item.status === 'Approved');

	if (approved.length === 0) {
		frappe.msgprint(__('No approved items selected. Please approve items first, then select them to publish.'));
		return;
	}

	let names = approved.map(item => item.name);
	frappe.confirm(
		__('Publish {0} approved items? This will create/update actual records.', [approved.length]),
		function() {
			frappe.call({
				method: 'plus_care_sync.sync_manager.doctype.sync_queue.sync_queue.bulk_publish',
				args: { names: names },
				freeze: true,
				freeze_message: __('Publishing...'),
				callback: function(r) {
					listview.refresh();
				}
			});
		}
	);
}
