from unittest.mock import patch
from odoo.exceptions import UserError

from .common import KaysMintsoftTestCommon, MockHTTPResponse
from ..services.order_service import OrderService


class TestKaysMultiCompanyIsolation(KaysMintsoftTestCommon):
    """Tests 14, 15, and 28: Strict Odoo 19 multi-company isolation, routing, and disabled company safeguards."""

    def test_14_wrong_company_blocked(self):
        """Test 14: Verify an order belonging to Company B cannot be validated or exported via Company A's connection."""
        self.company_ie.kays_enabled = True
        order_ie = self._create_confirmed_sale_order(company=self.company_ie, qty=1.0, confirm=True)

        svc = OrderService(self.env)
        val_res = svc.validate_sale_order(order_ie, self.instance_uk, is_manual=True)
        self.assertFalse(val_res['valid'])
        self.assertTrue(any('does not match Connection company' in e for e in val_res['errors']))

    @patch('requests.Session.request')
    def test_15_disabled_company_never_exports(self, mock_request):
        """
        Test 15: Verify orders belonging to a disabled company (Moyee Coffee Ireland, kays_enabled=False)
        are NEVER exported automatically upon confirmation and make zero API requests.
        """
        self.company_ie.kays_enabled = False
        order_ie = self._create_confirmed_sale_order(company=self.company_ie, qty=3.0, confirm=True)

        self.assertFalse(order_ie.kays_enabled)
        self.assertEqual(order_ie.kays_export_status, 'not_applicable')
        self.assertEqual(len(order_ie.kays_order_ids), 0)
        self.assertEqual(mock_request.call_count, 0)

        # Manual export attempt on a disabled company must raise UserError
        with self.assertRaises(UserError):
            order_ie.action_kays_send_order()

    def test_28_multi_company_isolation_and_ambiguous_routing_detection(self):
        """
        Test 28: Verify company isolation across connections and mappings, and verify ambiguous
        multi-connection configuration is detected when no default or warehouse routing is set.
        """
        # Create a second active connection for UK company without setting a default connection
        instance_uk_secondary = self.Instance.with_company(self.company_uk).create({
            'name': 'Moyee UK - Secondary Warehouse Connection',
            'active': True,
            'company_id': self.company_uk.id,
            'environment': 'dry_run',
            'api_base_url': 'https://api.mintsoft.co.uk/api',
            'mintsoft_warehouse_id': 9,
            'mintsoft_warehouse_name': 'Secondary Warehouse',
            'enable_integration': True,
        })
        self.company_uk.kays_default_instance_id = False
        self.instance_uk.odoo_warehouse_id = False
        instance_uk_secondary.odoo_warehouse_id = False

        # Ambiguous routing must raise a clear UserError instead of guessing which connection to use
        with self.assertRaises(UserError) as ctx:
            self.company_uk._get_default_kays_instance()
        self.assertIn('Ambiguous Kays Logistics routing', str(ctx.exception))

        # Restoring explicit default connection resolves routing cleanly
        self.company_uk.kays_default_instance_id = self.instance_uk.id
        resolved = self.company_uk._get_default_kays_instance()
        self.assertEqual(resolved.id, self.instance_uk.id)
