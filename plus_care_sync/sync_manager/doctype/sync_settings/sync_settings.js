// Copyright (c) 2024, Pluscare Team and contributors
// For license information, please see license.txt

frappe.ui.form.on('Sync Settings', {
	refresh: function(frm) {
		// Add custom buttons with better styling
		frm.page.set_primary_action(__('Save'), () => frm.save());

		// Add sync now button
		if (frm.doc.enable_sync) {
			frm.add_custom_button(__('Sync Now'), function() {
				let msg = frm.doc.sync_mode === 'Manual'
					? __('Fetch data and add to Review Queue?')
					: __('Start sync process now?');

				frappe.confirm(msg, function() {
					frappe.show_alert({message: __('Syncing...'), indicator: 'blue'});
					frappe.call({
						method: 'sync_now',
						doc: frm.doc,
						callback: function(r) {
							frm.reload_doc();
							// Show result message
							if (frm.doc.sync_mode === 'Manual') {
								frappe.msgprint({
									title: __('Sync Complete'),
									message: __('Items have been added to the Review Queue. Click "Review Queue" to review and publish them.'),
									indicator: 'blue',
									primary_action: {
										label: __('Open Review Queue'),
										action: function() {
											frappe.set_route('List', 'Sync Queue', {'status': 'Pending'});
										}
									}
								});
							}
						}
					});
				});
			}, __('Actions'));
		}

		// Add Review Queue button (prominent when manual mode)
		if (frm.doc.sync_mode === 'Manual') {
			frm.add_custom_button(__('Review Queue ({0})', [frm.doc.pending_sync_count || 0]), function() {
				frappe.set_route('List', 'Sync Queue', {'status': 'Pending'});
			}).addClass('btn-primary-dark');
		} else {
			frm.add_custom_button(__('Sync Queue'), function() {
				frappe.set_route('List', 'Sync Queue');
			}, __('View'));
		}

		// Add view logs button
		frm.add_custom_button(__('Sync Logs'), function() {
			frappe.set_route('List', 'Sync Log');
		}, __('View'));

		// Add Error Log button
		frm.add_custom_button(__('Error Log'), function() {
			frappe.set_route('List', 'Error Log', {'method': ['like', '%plus_care_sync%']});
		}, __('View'));

		// Add Test Fetch button for debugging
		if (frm.doc.remote_url && frm.doc.api_key) {
			frm.add_custom_button(__('Test Fetch Items'), function() {
				frappe.call({
					method: 'plus_care_sync.sync_manager.sync_engine.test_fetch_items',
					callback: function(r) {
						if (r.message) {
							let result = r.message;
							let msg = '';

							if (result.success) {
								msg = `<b>Success!</b><br>
									URL: ${result.url}<br>
									Status: ${result.status_code}<br>
									Records Found: ${result.total_records}<br><br>
									<b>Sample Data:</b><br>
									<pre>${JSON.stringify(result.sample_data, null, 2)}</pre>`;
							} else {
								msg = `<b>Failed!</b><br>
									URL: ${result.url}<br>
									Status: ${result.status_code}<br>
									Error: ${result.error || result.message}`;
							}

							frappe.msgprint({
								title: __('Test Fetch Result'),
								message: msg,
								indicator: result.success ? 'green' : 'red'
							});
						}
					}
				});
			}, __('Debug'));
		}

		// Auto-update pending count
		if (frm.doc.enable_sync) {
			update_pending_count(frm);

			// Refresh every 30 seconds
			setInterval(function() {
				update_pending_count(frm);
			}, 30000);
		}

		// Show workflow hint for manual mode
		if (frm.doc.sync_mode === 'Manual') {
			frm.set_intro(__('Manual Mode: Click "Sync Now" to fetch data, then review in Queue before publishing.'), 'blue');
		}
	},

	test_connection: function(frm) {
		if (!frm.doc.remote_url || !frm.doc.api_key || !frm.doc.api_secret) {
			frappe.msgprint(__('Please provide Remote URL, API Key, and API Secret'));
			return;
		}

		frappe.call({
			method: 'test_connection',
			doc: frm.doc,
			callback: function(r) {
				frm.reload_doc();
			}
		});
	},

	sync_now: function(frm) {
		if (!frm.doc.enable_sync) {
			frappe.msgprint(__('Please enable sync first'));
			return;
		}

		frappe.confirm(
			__('Start sync process now? This will sync data based on your configuration.'),
			function() {
				frappe.call({
					method: 'sync_now',
					doc: frm.doc,
					callback: function(r) {
						frm.reload_doc();
					}
				});
			}
		);
	},

	view_sync_logs: function(frm) {
		frappe.set_route('List', 'Sync Log');
	},

	reset_last_sync_time: function(frm) {
		frappe.confirm(
			__('Clear Last Sync Time? The next sync will pull all records from scratch (full re-sync).'),
			function() {
				frappe.call({
					method: 'reset_last_sync_time',
					doc: frm.doc,
					callback: function(r) {
						frm.reload_doc();
					}
				});
			}
		);
	},

	enable_sync: function(frm) {
		if (frm.doc.enable_sync) {
			frm.set_df_property('sync_mode', 'reqd', 1);
			frm.set_df_property('remote_url', 'reqd', 1);
			frm.set_df_property('api_key', 'reqd', 1);
			frm.set_df_property('api_secret', 'reqd', 1);
		} else {
			frm.set_df_property('sync_mode', 'reqd', 0);
			frm.set_df_property('remote_url', 'reqd', 0);
			frm.set_df_property('api_key', 'reqd', 0);
			frm.set_df_property('api_secret', 'reqd', 0);
		}
	},

	data_type: function(frm) {
		if (frm.doc.data_type === 'Full Database') {
			frappe.msgprint({
				title: __('Warning'),
				message: __('Full database sync may take a long time and consume significant bandwidth. Use with caution.'),
				indicator: 'orange'
			});
		}
	}
});

function update_pending_count(frm) {
	frappe.call({
		method: 'frappe.client.get_count',
		args: {
			doctype: 'Sync Queue',
			filters: {
				'status': 'Pending'
			}
		},
		callback: function(r) {
			if (r.message !== undefined) {
				frm.set_value('pending_sync_count', r.message);
			}
		}
	});
}
