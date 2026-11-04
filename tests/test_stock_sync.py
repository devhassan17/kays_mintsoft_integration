from unittest.mock import patch

from .common import KaysMintsoftTestCommon, MockHTTPResponse
from ..services.inventory_service import InventoryService


class TestKaysStockSync(KaysMintsoftTestCommon):
    """Tests 20–21: Stock synchronization, UoM conversion, reservation math, and threshold safeguards."""

    @patch('requests.Session.request')
    def test_20_stock_sync_uom_factor_and_inventory_adjustment(self, mock_request):
        """
        Test 20: Verify GET /api/Product/StockLevels fetches warehouse stock, applies UoM conversion factor,
        calculates discrepancy, and updates Odoo stock.quant when within configured thresholds.
        """
        self.instance_uk.stock_sync_mode = 'mintsoft_master'
        self.instance_uk.stock_threshold_units = 500.0
        self.instance_uk.stock_threshold_percent = 100.0
        self.mapping_coffee.uom_conversion_factor = 2.0  # 1 Mintsoft case = 2 Odoo kg bags

        mock_request.return_value = MockHTTPResponse(200, [{
            'ProductId': 5001,
            'SKU': 'MOYEE-ESP-1KG',
            'WarehouseId': 3,
            'Quantity': 25,
            'Allocated': 5,
            'OnOrder': 10,
        }])

        summary = InventoryService(self.env).sync_stock_levels(self.instance_uk, full_sync=True)
        self.assertEqual(summary['processed'], 1)
        self.assertEqual(summary['adjusted'], 1)
        self.assertEqual(summary['blocked_by_threshold'], 0)

        # Available in Mintsoft = 25 - 5 = 20 units * 2.0 UoM factor = 40.0 Odoo units
        self.assertEqual(self.mapping_coffee.mintsoft_available_qty, 40.0)
        self.assertEqual(self.mapping_coffee.mintsoft_on_hand_qty, 50.0)
        self.assertEqual(self.mapping_coffee.mintsoft_allocated_qty, 10.0)

        # Verify Odoo stock.quant at the mapped warehouse location was adjusted to 40.0
        quant = self.env['stock.quant'].search([
            ('product_id', '=', self.product_coffee.id),
            ('location_id', '=', self.warehouse_uk.lot_stock_id.id),
        ], limit=1)
        self.assertTrue(quant)
        self.assertEqual(quant.quantity, 40.0)

    @patch('requests.Session.request')
    def test_21_stock_discrepancy_threshold_blocks_unsafe_adjustment(self, mock_request):
        """
        Test 21: Verify stock discrepancy exceeding stock_threshold_units blocks automatic adjustment,
        creates an open kays.mintsoft.stock.discrepancy record, and allows manual manager approval.
        """
        self.instance_uk.stock_sync_mode = 'mintsoft_master'
        self.instance_uk.stock_threshold_units = 10.0
        self.instance_uk.stock_threshold_percent = 25.0
        self.mapping_coffee.uom_conversion_factor = 1.0

        mock_request.return_value = MockHTTPResponse(200, [{
            'ProductId': 5001,
            'SKU': 'MOYEE-ESP-1KG',
            'WarehouseId': 3,
            'Quantity': 250,
            'Allocated': 0,
            'OnOrder': 0,
        }])

        summary = InventoryService(self.env).sync_stock_levels(self.instance_uk, full_sync=True)
        self.assertEqual(summary['processed'], 1)
        self.assertEqual(summary['adjusted'], 0)
        self.assertEqual(summary['blocked_by_threshold'], 1)

        # Verify open discrepancy record was created
        disc = self.env['kays.mintsoft.stock.discrepancy'].search([
            ('instance_id', '=', self.instance_uk.id),
            ('product_id', '=', self.product_coffee.id),
            ('state', '=', 'open'),
        ], limit=1)
        self.assertTrue(disc)
        self.assertTrue(disc.exceeded_threshold)
        self.assertEqual(disc.mintsoft_available_qty, 250.0)

        # Manager approves the discrepancy -> stock adjustment is applied and state becomes 'adjusted'
        disc.action_approve_and_apply_adjustment()
        self.assertEqual(disc.state, 'adjusted')
        quant = self.env['stock.quant'].search([
            ('product_id', '=', self.product_coffee.id),
            ('location_id', '=', self.warehouse_uk.lot_stock_id.id),
        ], limit=1)
        self.assertEqual(quant.quantity, 250.0)
