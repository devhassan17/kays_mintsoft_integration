from odoo.exceptions import AccessError, UserError

from .common import KaysMintsoftTestCommon


class TestKaysSecurityAndIntegrity(KaysMintsoftTestCommon):
    """Tests 29–30: Security roles, credential protection, and module installation integrity."""

    def test_29_unauthorized_settings_and_credential_access_blocked(self):
        """
        Test 29: Verify Integration Viewer and Integration Manager cannot modify connections
        or enable Production mode without Integration Administrator privileges.
        """
        group_viewer = self.env.ref('kays_mintsoft_integration.group_kays_viewer')
        group_manager = self.env.ref('kays_mintsoft_integration.group_kays_manager')
        group_admin = self.env.ref('kays_mintsoft_integration.group_kays_admin')
        group_user = self.env.ref('base.group_user')

        viewer_user = self.env['res.users'].create({
            'name': 'Kays Viewer User',
            'login': 'kays_viewer_test@example.com',
            'company_id': self.company_uk.id,
            'company_ids': [(6, 0, [self.company_uk.id])],
            'group_ids': [(6, 0, [group_user.id, group_viewer.id])],
        })
        manager_user = self.env['res.users'].create({
            'name': 'Kays Manager User',
            'login': 'kays_manager_test@example.com',
            'company_id': self.company_uk.id,
            'company_ids': [(6, 0, [self.company_uk.id])],
            'group_ids': [(6, 0, [group_user.id, group_manager.id])],
        })

        # Viewer cannot write to kays.mintsoft.instance (ACL perm_write=0)
        with self.assertRaises(AccessError):
            self.instance_uk.with_user(viewer_user).write({'name': 'Unauthorized Rename'})

        # Manager cannot confirm the Production Mode Wizard (restricted to group_kays_admin)
        prod_wiz = self.env['kays.production.mode.wizard'].with_user( self.env.user ).create({
            'instance_id': self.instance_uk.id,
            'confirm_credentials_verified': True,
            'confirm_warehouse_verified': True,
            'confirm_live_dispatch_understood': True,
            'confirmation_phrase': 'ENABLE PRODUCTION',
        })
        with self.assertRaises((AccessError, UserError)):
            prod_wiz.with_user(manager_user).action_confirm_enable_production()

        # Verify hierarchy: Admin implies Manager implies Viewer
        self.assertIn(group_viewer, group_manager.implied_ids)
        self.assertIn(group_manager, group_admin.implied_ids)

    def test_30_module_installation_integrity_and_dashboard_telemetry(self):
        """
        Test 30: Verify all module models, Odoo 19 privilege groups, default status mappings,
        scheduled task configurations, and dashboard telemetry queries work cleanly.
        """
        expected_models = [
            'kays.mintsoft.instance',
            'kays.mintsoft.order',
            'kays.mintsoft.order.line',
            'kays.mintsoft.status.mapping',
            'kays.mintsoft.shipment',
            'kays.mintsoft.tracking.event',
            'kays.mintsoft.product.mapping',
            'kays.mintsoft.carrier.mapping',
            'kays.mintsoft.warehouse.mapping',
            'kays.mintsoft.stock.snapshot',
            'kays.mintsoft.stock.discrepancy',
            'kays.mintsoft.sync.job',
            'kays.mintsoft.job.schedule',
            'kays.mintsoft.log',
            'kays.mintsoft.webhook.event',
            'kays.mintsoft.return',
            'kays.mintsoft.return.line',
        ]
        for model_name in expected_models:
            self.assertIn(model_name, self.env, f"Model {model_name} must be registered in Odoo 19 registry")

        # Verify default status mappings and 10 scheduled job configs were loaded
        status_mappings = self.env['kays.mintsoft.status.mapping'].search([])
        self.assertGreaterEqual(len(status_mappings), 10)

        job_schedules = self.env['kays.mintsoft.job.schedule'].search([])
        self.assertEqual(len(job_schedules), 10)

        # Verify dashboard telemetry method returns all required KPIs and structures
        dash = self.env['kays.mintsoft.instance'].get_dashboard_data(
            company_id=self.company_uk.id,
            instance_id=self.instance_uk.id,
            date_range='30d',
        )
        self.assertIn('kpis', dash)
        self.assertIn('total_orders', dash['kpis'])
        self.assertIn('api_connection_status', dash['kpis'])
        self.assertIn('status_distribution', dash)
        self.assertIn('daily_exports', dash)
        self.assertIn('schedules', dash)
