import json
from unittest.mock import MagicMock
from odoo.tests.common import TransactionCase, tagged


class MockHTTPResponse:
    """Helper mock for requests.Response in Mintsoft unit tests."""

    def __init__(self, status_code=200, json_data=None, text='', headers=None):
        self.status_code = status_code
        self._json_data = json_data
        if text:
            self.text = text
        elif json_data is not None:
            self.text = json.dumps(json_data)
        else:
            self.text = ''
        self.content = self.text.encode('utf-8')
        self.headers = headers or {'Content-Type': 'application/json'}

    def json(self):
        if self._json_data is not None:
            return self._json_data
        return json.loads(self.text)


@tagged('post_install', '-at_install', 'kays_mintsoft')
class KaysMintsoftTestCommon(TransactionCase):
    """Base test fixture for Kays Logistics (Mintsoft) Odoo 19 Enterprise integration tests."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Company = cls.env['res.company']
        cls.Instance = cls.env['kays.mintsoft.instance']
        cls.Product = cls.env['product.product']
        cls.SaleOrder = cls.env['sale.order']
        cls.KaysOrder = cls.env['kays.mintsoft.order']
        cls.ProductMapping = cls.env['kays.mintsoft.product.mapping']
        cls.CarrierMapping = cls.env['kays.mintsoft.carrier.mapping']
        cls.SyncJob = cls.env['kays.mintsoft.sync.job']
        cls.WebhookEvent = cls.env['kays.mintsoft.webhook.event']

        cls.uk_country = cls.env.ref('base.uk', raise_if_not_found=False) or cls.env['res.country'].search([('code', '=', 'GB')], limit=1)
        cls.gbp_currency = cls.env.ref('base.GBP', raise_if_not_found=False) or cls.env.company.currency_id

        # Enabled Company: Moyee Coffee United Kingdom
        cls.company_uk = cls.Company.create({
            'name': 'Moyee Coffee United Kingdom',
            'currency_id': cls.gbp_currency.id,
            'kays_enabled': True,
        })

        # Disabled Company: Moyee Coffee Ireland
        cls.company_ie = cls.Company.create({
            'name': 'Moyee Coffee Ireland',
            'currency_id': cls.gbp_currency.id,
            'kays_enabled': False,
        })

        cls.warehouse_uk = cls.env['stock.warehouse'].search([('company_id', '=', cls.company_uk.id)], limit=1)
        if not cls.warehouse_uk:
            cls.warehouse_uk = cls.env['stock.warehouse'].with_company(cls.company_uk).create({
                'name': 'UK Main Warehouse',
                'code': 'UKWH',
                'company_id': cls.company_uk.id,
            })

        # Create active Kays Connection for UK Company (Test Client mode so write calls are exercised against mocks)
        cls.instance_uk = cls.Instance.with_company(cls.company_uk).create({
            'name': 'Moyee UK - Mintsoft Test Connection',
            'active': True,
            'company_id': cls.company_uk.id,
            'environment': 'test_client',
            'api_base_url': 'https://api.mintsoft.co.uk/api',
            'api_version': 'V1',
            'api_key': 'TEST_SYNTHETIC_API_KEY_DO_NOT_USE_IN_PROD_12345',
            'mintsoft_warehouse_id': 3,
            'mintsoft_warehouse_name': 'Main Warehouse',
            'mintsoft_client_id': 10,
            'odoo_warehouse_id': cls.warehouse_uk.id,
            'odoo_stock_location_id': cls.warehouse_uk.lot_stock_id.id,
            'currency_id': cls.gbp_currency.id,
            'enable_integration': True,
            'enable_auto_order_export': True,
            'enable_manual_order_export': True,
            'enable_product_import': True,
            'enable_product_export': True,
            'enable_inventory_sync': True,
            'enable_order_status_sync': True,
            'enable_tracking_sync': True,
            'enable_shipment_sync': True,
            'enable_cancellation_sync': True,
            'enable_order_amendments': True,
            'enable_returns_sync': True,
            'enable_webhooks': True,
            'enable_scheduled_polling': True,
            'enable_auto_retry': True,
            'enable_api_debug_logging': True,
            'payment_eligibility_policy': 'confirmed_only',
            'unmapped_sku_policy': 'block_order',
            'unmapped_carrier_policy': 'use_default',
            'default_mintsoft_courier_service_id': 12,
            'default_mintsoft_courier_service_name': 'DPD Next Day',
            'webhook_secret_token': 'TEST_WEBHOOK_SECRET_TOKEN_987654321',
        })
        cls.company_uk.kays_default_instance_id = cls.instance_uk.id

        # Customer with valid UK shipping address
        cls.partner_customer = cls.env['res.partner'].create({
            'name': 'Jane Coffee Lover',
            'street': '221B Baker Street',
            'street2': 'Suite 4',
            'city': 'London',
            'zip': 'NW1 6XE',
            'country_id': cls.uk_country.id if cls.uk_country else False,
            'email': 'jane@example.co.uk',
            'phone': '+442079460192',
        })

        # Storable product (Odoo 19: type='consu', is_storable=True)
        cls.product_coffee = cls.Product.with_company(cls.company_uk).create({
            'name': 'Moyee FairChain Espresso 1kg',
            'default_code': 'MOYEE-ESP-1KG',
            'type': 'consu',
            'is_storable': True,
            'list_price': 24.50,
            'weight': 1.0,
        })

        # Explicit SKU mapping
        cls.mapping_coffee = cls.ProductMapping.with_company(cls.company_uk).create({
            'company_id': cls.company_uk.id,
            'instance_id': cls.instance_uk.id,
            'product_id': cls.product_coffee.id,
            'odoo_sku': 'MOYEE-ESP-1KG',
            'mintsoft_sku': 'MOYEE-ESP-1KG',
            'mintsoft_product_id': 5001,
            'mintsoft_product_name': 'Moyee FairChain Espresso 1kg',
            'uom_conversion_factor': 1.0,
            'sync_status': 'mapped',
            'stock_sync_enabled': True,
        })

    def _create_confirmed_sale_order(self, company=None, product=None, qty=2.0, confirm=True):
        company = company or self.company_uk
        product = product or self.product_coffee
        wh = self.env['stock.warehouse'].search([('company_id', '=', company.id)], limit=1)
        vals = {
            'partner_id': self.partner_customer.id,
            'partner_shipping_id': self.partner_customer.id,
            'company_id': company.id,
            'warehouse_id': wh.id if wh else self.warehouse_uk.id,
            'order_line': [
                (0, 0, {
                    'product_id': product.id,
                    'product_uom_qty': qty,
                    'price_unit': 24.50,
                }),
            ],
        }
        order = self.SaleOrder.with_company(company).create(vals)
        if confirm:
            order.action_confirm()
        return order
