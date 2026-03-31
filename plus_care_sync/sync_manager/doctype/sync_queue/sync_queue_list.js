frappe.listview_settings['Sync Queue'] = {
	add_fields: ['status', 'reference_doctype', 'reference_name', 'sync_direction'],

	get_indicator: function(doc) {
		const status_map = {
			'Pending': ['Pending', 'orange'],
			'Approved': ['Approved', 'blue'],
			'Published': ['Published', 'green'],
			'Rejected': ['Rejected', 'gray'],
			'Failed': ['Failed', 'red']
		};
		return status_map[doc.status] || ['Unknown', 'gray'];
	},

	onload: function(listview) {
		// Add Approve to menu
		listview.page.add_menu_item(__('Approve Selected'), () => {
			const selected = listview.get_checked_items();
			if (!selected.length) {
				frappe.msgprint(__('Please select items first'));
				return;
			}
			const names = selected.map(d => d.name);
			frappe.confirm(__('Approve {0} items?', [names.length]), () => {
				frappe.call({
					method: 'plus_care_sync.sync_manager.doctype.sync_queue.sync_queue.bulk_approve',
					args: { names },
					freeze: true,
					callback: () => listview.refresh()
				});
			});
		});

		// Add Reject to menu
		listview.page.add_menu_item(__('Reject Selected'), () => {
			const selected = listview.get_checked_items();
			if (!selected.length) {
				frappe.msgprint(__('Please select items first'));
				return;
			}
			const names = selected.map(d => d.name);
			frappe.prompt({
				fieldname: 'reason',
				fieldtype: 'Small Text',
				label: 'Rejection Reason'
			}, (values) => {
				frappe.call({
					method: 'plus_care_sync.sync_manager.doctype.sync_queue.sync_queue.bulk_reject',
					args: { names, reason: values.reason },
					freeze: true,
					callback: () => listview.refresh()
				});
			}, __('Reject Items'));
		});

		// Publish button (secondary action - visible)
		listview.page.set_secondary_action(__('Publish'), () => {
			const selected = listview.get_checked_items();
			if (!selected.length) {
				frappe.msgprint(__('Please select items first'));
				return;
			}
			const approved = selected.filter(d => d.status === 'Approved');
			if (!approved.length) {
				frappe.msgprint(__('No approved items. Approve first, then select and publish.'));
				return;
			}
			const names = approved.map(d => d.name);
			frappe.confirm(__('Publish {0} items? This will create actual records.', [names.length]), () => {
				frappe.call({
					method: 'plus_care_sync.sync_manager.doctype.sync_queue.sync_queue.bulk_publish',
					args: { names },
					freeze: true,
					callback: () => listview.refresh()
				});
			});
		});
	}
};
