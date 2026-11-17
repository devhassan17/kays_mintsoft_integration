from unittest.mock import patch
import requests
from odoo.exceptions import UserError

from .common import KaysMintsoftTestCommon, MockHTTPResponse
from ..services.order_service import OrderService


class TestKaysOrderExport(KaysMintsoftTestCommon):
    """Tests 8–13 and 16–19: Order validation, submission, duplicate prevention, split orders, and eligibility."""

    @patch('requests.Session.request')
    def test_08_order_submission_success(self, mock_request):
        """Test 8: Verify end-to-end order submission to PUT /api/Order and response linking."""
        def side_effect(**kwargs):
            url = kwargs.get('url', '')
            method = kwargs.get('method', '')
            if method == 'GET' and ('/api/Order/Search' in url or '/api/Order/GetOrderId' in url):
                return MockHTTPResponse(200, [])
            if method == 'PUT' and url.endswith('/api/Order'):
                return MockHTTPResponse(200, [{
                    'OrderId': 91001,
                    'OrderNumber': kwargs['json']['OrderNumber'],
                    'Success': True,
                    'OrderStatusId': 1,
                    'Message': 'Order Created',
                    'OrderItems': [{
                        'OrderItemId': 77001,
                        'SKU': 'MOYEE-ESP-1KG',
                        'ProductId': 5001,
                        'Quantity': 2,
                    }],
                }])
            return MockHTTPResponse(200, [])

        mock_request.side_effect = side_effect
        sale_order = self._create_confirmed_sale_order(qty=2.0, confirm=True)

        self.assertEqual(sale_order.kays_export_status, 'sent')
        self.assertEqual(sale_order.kays_mintsoft_order_id, 91001)
        self.assertTrue(sale_order.kays_primary_order_id)
        self.assertEqual(sale_order.kays_primary_order_id.fulfilment_status, 'received')
        self.assertEqual(len(sale_order.kays_primary_order_id.line_ids), 1)
        self.assertEqual(sale_order.kays_primary_order_id.line_ids[0].mintsoft_order_item_id, 77001)

    def test_09_order_validation_failure_missing_address(self):
        """Test 9: Verify order validation catches missing shipping street/postcode/city before calling API."""
        self.instance_uk.enable_auto_order_export = False
        incomplete_partner = self.env['res.partner'].create({
            'name': 'Incomplete Address Customer',
            'street': False,
            'city': False,
            'zip': False,
            'country_id': False,
        })
        order = self.SaleOrder.with_company(self.company_uk).create({
            'partner_id': incomplete_partner.id,
            'partner_shipping_id': incomplete_partner.id,
            'company_id': self.company_uk.id,
            'warehouse_id': self.warehouse_uk.id,
            'order_line': [(0, 0, {
                'product_id': self.product_coffee.id,
                'product_uom_qty': 1.0,
                'price_unit': 24.50,
            })],
        })
        order.action_confirm()

        svc = OrderService(self.env)
        val_res = svc.validate_sale_order(order, self.instance_uk, is_manual=True)
        self.assertFalse(val_res['valid'])
        joined_errors = ' '.join(val_res['errors'])
        self.assertIn('Address1', joined_errors)
        self.assertIn('PostCode', joined_errors)

    @patch('requests.Session.request')
    def test_10_duplicate_order_prevention_remote_and_local(self, mock_request):
        """Test 10: Verify pre-export duplicate detection links existing Mintsoft order without calling PUT /api/Order."""
        self.instance_uk.enable_auto_order_export = False
        order = self._create_confirmed_sale_order(qty=1.0, confirm=True)

        put_calls = []

        def side_effect(**kwargs):
            url = kwargs.get('url', '')
            method = kwargs.get('method', '')
            if method == 'PUT':
                put_calls.append(url)
            if method == 'GET' and '/api/Order/Search' in url:
                return MockHTTPResponse(200, [{
                    'ID': 88442,
                    'OrderNumber': order.name,
                    'ExternalOrderReference': order.name,
                    'OrderStatusId': 2,
                    'OrderStatus': 'Processing',
                }])
            return MockHTTPResponse(200, [])

        mock_request.side_effect = side_effect
        kays_order = OrderService(self.env).export_sale_order(order, instance=self.instance_uk, is_manual=True)

        # Must have linked existing Mintsoft Order #88442 without sending a duplicate PUT /api/Order
        self.assertEqual(len(put_calls), 0)
        self.assertEqual(kays_order.mintsoft_order_id, 88442)
        self.assertEqual(kays_order.export_status, 'sent')

    @patch('requests.Session.request')
    def test_11_unknown_outcome_reconciliation(self, mock_request):
        """
        Test 11: Verify timeout during PUT /api/Order transitions order to unknown_outcome,
        blocks blind retry, and reconciles via GET /api/Order/Search.
        """
        self.instance_uk.enable_auto_order_export = False
        order = self._create_confirmed_sale_order(qty=1.0, confirm=True)

        def timeout_on_put(**kwargs):
            if kwargs.get('method') == 'PUT':
                raise requests.exceptions.Timeout("Gateway timed out after order payload sent")
            return MockHTTPResponse(200, [])

        mock_request.side_effect = timeout_on_put
        svc = OrderService(self.env)
        kays_order = svc.export_sale_order(order, instance=self.instance_uk, is_manual=True)

        self.assertEqual(kays_order.export_status, 'unknown_outcome')
        self.assertTrue(kays_order.requires_reconciliation)

        # Now reconcile against Mintsoft where the order was indeed created (ID 95500)
        def search_finds_order(**kwargs):
            if '/api/Order/Search' in kwargs.get('url', ''):
                return MockHTTPResponse(200, [{
                    'ID': 95500,
                    'OrderNumber': kays_order.external_order_reference,
                    'OrderStatusId': 1,
                    'OrderStatus': 'New',
                }])
            return MockHTTPResponse(200, [])

        mock_request.side_effect = search_finds_order
        reconciled = svc.reconcile_unknown_outcome_order(kays_order)
        self.assertTrue(reconciled)
        self.assertEqual(kays_order.export_status, 'sent')
        self.assertEqual(kays_order.mintsoft_order_id, 95500)
        self.assertFalse(kays_order.requires_reconciliation)

    @patch('requests.Session.request')
    def test_12_split_order_responses(self, mock_request):
        """Test 12: Verify PUT /api/Order returning multiple NewOrderResult objects creates linked split orders."""
        self.instance_uk.enable_auto_order_export = False
        order = self._create_confirmed_sale_order(qty=4.0, confirm=True)

        def split_response(**kwargs):
            if kwargs.get('method') == 'PUT' and kwargs.get('url', '').endswith('/api/Order'):
                return MockHTTPResponse(200, [
                    {
                        'OrderId': 96001,
                        'OrderNumber': f"{order.name}",
                        'Success': True,
                        'OrderStatusId': 1,
                        'OrderItems': [{'OrderItemId': 1, 'SKU': 'MOYEE-ESP-1KG', 'Quantity': 2}],
                    },
                    {
                        'OrderId': 96002,
                        'OrderNumber': f"{order.name}-SPLIT-2",
                        'Success': True,
                        'OrderStatusId': 1,
                        'OrderItems': [{'OrderItemId': 2, 'SKU': 'MOYEE-ESP-1KG', 'Quantity': 2}],
                    },
                ])
            return MockHTTPResponse(200, [])

        mock_request.side_effect = split_response
        primary_ko = OrderService(self.env).export_sale_order(order, instance=self.instance_uk, is_manual=True)

        self.assertEqual(primary_ko.mintsoft_order_id, 96001)
        self.assertEqual(len(primary_ko.child_split_order_ids), 1)
        split_child = primary_ko.child_split_order_ids[0]
        self.assertTrue(split_child.is_split_order)
        self.assertEqual(split_child.mintsoft_order_id, 96002)
        self.assertEqual(split_child.sale_order_id.id, order.id)
        self.assertEqual(len(order.kays_order_ids), 2)

    def test_13_missing_sku_failure(self):
        """Test 13: Verify unmapped product with unmapped_sku_policy='block_order' blocks export and logs missing SKU."""
        self.instance_uk.enable_auto_order_export = False
        self.instance_uk.unmapped_sku_policy = 'block_order'
        unmapped_product = self.Product.with_company(self.company_uk).create({
            'name': 'Unmapped Roast 250g',
            'default_code': 'UNMAPPED-SKU-999',
            'type': 'consu',
            'is_storable': True,
            'list_price': 12.0,
        })
        order = self._create_confirmed_sale_order(product=unmapped_product, qty=1.0, confirm=True)
        ko = OrderService(self.env).export_sale_order(order, instance=self.instance_uk, is_manual=True)

        self.assertEqual(ko.export_status, 'failed')
        self.assertIn('UNMAPPED-SKU-999', ko.last_error or '')

        missing_map = self.ProductMapping.search([
            ('instance_id', '=', self.instance_uk.id),
            ('product_id', '=', unmapped_product.id),
            ('sync_status', '=', 'missing'),
        ], limit=1)
        self.assertTrue(missing_map)

    @patch('requests.Session.request')
    def test_16_disabled_connection_never_exports(self, mock_request):
        """Test 16: Verify an inactive or integration-disabled connection blocks order export."""
        self.instance_uk.enable_integration = False
        order = self._create_confirmed_sale_order(qty=1.0, confirm=True)

        self.assertEqual(len(order.kays_order_ids), 0)
        self.assertEqual(mock_request.call_count, 0)

        svc = OrderService(self.env)
        val_res = svc.validate_sale_order(order, self.instance_uk, is_manual=True)
        self.assertFalse(val_res['valid'])
        self.assertTrue(any('Master switch' in e for e in val_res['errors']))

    @patch('requests.Session.request')
    def test_17_disabled_auto_export_allows_manual_export(self, mock_request):
        """Test 17: Verify disabling Automatic Order Export prevents auto-export on confirm while allowing manual export."""
        self.instance_uk.enable_auto_order_export = False
        self.instance_uk.enable_manual_order_export = True

        mock_request.return_value = MockHTTPResponse(200, [{
            'OrderId': 97001,
            'OrderNumber': 'MANUAL-OK',
            'Success': True,
            'OrderStatusId': 1,
        }])

        order = self._create_confirmed_sale_order(qty=1.0, confirm=True)
        # Auto export did not run on confirm
        self.assertEqual(len(order.kays_order_ids), 0)

        # Manual export succeeds
        order.action_kays_send_order()
        self.assertEqual(order.kays_export_status, 'sent')
        self.assertEqual(order.kays_mintsoft_order_id, 97001)

    def test_18_draft_order_blocked(self):
        """Test 18: Verify draft quotations cannot be exported to Mintsoft."""
        draft_order = self._create_confirmed_sale_order(qty=1.0, confirm=False)
        self.assertEqual(draft_order.state, 'draft')

        svc = OrderService(self.env)
        val_res = svc.validate_sale_order(draft_order, self.instance_uk, is_manual=True)
        self.assertFalse(val_res['valid'])
        self.assertTrue(any('confirmed' in e for e in val_res['errors']))

    def test_19_unpaid_order_blocked_when_payment_required(self):
        """Test 19: Verify unpaid order is blocked when payment_eligibility_policy requires paid invoice or approval."""
        self.instance_uk.enable_auto_order_export = False
        self.instance_uk.payment_eligibility_policy = 'invoice_paid'

        order = self._create_confirmed_sale_order(qty=1.0, confirm=True)
        svc = OrderService(self.env)
        pay_ok, reason = svc.check_payment_eligibility(order, self.instance_uk)
        self.assertFalse(pay_ok)
        self.assertIn('paid invoice', reason)

        # Approving via kays_payment_approved under 'payment_flag' policy unlocks export
        self.instance_uk.payment_eligibility_policy = 'payment_flag'
        self.assertFalse(svc.check_payment_eligibility(order, self.instance_uk)[0])
        order.kays_payment_approved = True
        self.assertTrue(svc.check_payment_eligibility(order, self.instance_uk)[0])

    @patch('requests.Session.request')
    def test_20_subscription_renewal_delivery_export(self, mock_request):
        """Test 20: Verify export of a recurring subscription delivery (stock.picking) to Mintsoft."""
        def side_effect(**kwargs):
            url = kwargs.get('url', '')
            method = kwargs.get('method', '')
            if method == 'GET' and ('/api/Order/Search' in url or '/api/Order/GetOrderId' in url):
                return MockHTTPResponse(200, [])
            if method == 'PUT' and url.endswith('/api/Order'):
                return MockHTTPResponse(200, [{
                    'OrderId': 98001,
                    'OrderNumber': kwargs['json']['OrderNumber'],
                    'Success': True,
                    'OrderStatusId': 1,
                    'Message': 'Subscription Delivery Created',
                    'OrderItems': [{
                        'OrderItemId': 88001,
                        'SKU': 'MOYEE-ESP-1KG',
                        'Quantity': 2,
                    }],
                }])
            return MockHTTPResponse(200, [])

        mock_request.side_effect = side_effect
        self.instance_uk.enable_auto_order_export = False
        self.instance_uk.enable_subscription_export = True

        sale_order = self._create_confirmed_sale_order(qty=2.0, confirm=True)
        picking = sale_order.picking_ids.filtered(lambda p: p.picking_type_id.code == 'outgoing')[:1]
        self.assertTrue(picking)

        svc = OrderService(self.env)
        kays_order = svc.export_subscription_delivery(picking, instance=self.instance_uk, is_manual=True)

        self.assertEqual(kays_order.export_status, 'sent')
        self.assertEqual(kays_order.mintsoft_order_id, 98001)
        self.assertTrue(kays_order.is_subscription_delivery)
        self.assertEqual(kays_order.picking_id, picking)
        self.assertEqual(picking.kays_order_id, kays_order)
        self.assertTrue(kays_order.external_order_reference.startswith(sale_order.name))

    @patch('requests.Session.request')
    def test_21_payment_trigger_exports_subscription_delivery(self, mock_request):
        """Test 21: Verify payment completion hook automatically triggers export of unexported subscription pickings."""
        def side_effect(**kwargs):
            url = kwargs.get('url', '')
            method = kwargs.get('method', '')
            if method == 'GET' and ('/api/Order/Search' in url or '/api/Order/GetOrderId' in url):
                return MockHTTPResponse(200, [])
            if method == 'PUT' and url.endswith('/api/Order'):
                return MockHTTPResponse(200, [{
                    'OrderId': 98002,
                    'OrderNumber': kwargs['json']['OrderNumber'],
                    'Success': True,
                    'OrderStatusId': 1,
                    'Message': 'Subscription Delivery Auto Created',
                }])
            return MockHTTPResponse(200, [])

        mock_request.side_effect = side_effect
        self.instance_uk.enable_auto_order_export = False
        self.instance_uk.enable_subscription_export = True

        sale_order = self._create_confirmed_sale_order(qty=2.0, confirm=True)
        picking = sale_order.picking_ids.filtered(lambda p: p.picking_type_id.code == 'outgoing')[:1]
        self.assertTrue(picking)
        self.assertFalse(picking.kays_order_id)

        payment_tx = self.env['payment.transaction'].create({
            'reference': f'TX-SUB-{sale_order.name}',
            'amount': sale_order.amount_total,
            'currency_id': sale_order.currency_id.id,
            'provider_id': self.env['payment.provider'].search([], limit=1).id or False,
            'partner_id': sale_order.partner_id.id,
            'sale_order_ids': [(6, 0, sale_order.ids)],
            'state': 'draft',
        })
        payment_tx.state = 'done'
        payment_tx._kays_trigger_post_payment_export()

        self.assertTrue(picking.kays_order_id)
        self.assertEqual(picking.kays_order_id.export_status, 'sent')
        self.assertEqual(picking.kays_order_id.mintsoft_order_id, 98002)
        self.assertTrue(picking.kays_order_id.is_subscription_delivery)
