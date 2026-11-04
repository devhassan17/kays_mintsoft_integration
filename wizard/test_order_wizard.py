import json
import re
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

from ..services.mintsoft_client import MintsoftClient, MintsoftAPIError, sanitize_payload
from ..services.order_service import OrderService

PLACEHOLDER_PATTERNS = re.compile(
    r'\b(fake|placeholder|dummy|lorem|ipsum|asdf|qwerty|test\s*user|john\s*doe|jane\s*doe|example\.com|00000)\b',
    re.I,
)


class KaysProductionModeWizard(models.TransientModel):
    _name = 'kays.production.mode.wizard'
    _description = 'Confirm Activation of Production Mode on Kays Connection'

    instance_id = fields.Many2one(
        'kays.mintsoft.instance',
        string='Connection',
        required=True,
        readonly=True,
    )
    company_id = fields.Many2one(
        'res.company',
        related='instance_id.company_id',
        readonly=True,
    )
    confirm_credentials_verified = fields.Boolean(
        string='I have verified the API credentials, Warehouse ID, and SKU mappings in Dry Run / Test mode.',
        required=True,
    )
    confirm_live_fulfilment = fields.Boolean(
        string='I understand that enabling Production mode will submit live fulfilment orders to Kays Logistics.',
        required=True,
    )
    approval_notes = fields.Text(string='Activation Audit Notes', required=True)

    def action_confirm_production_mode(self):
        self.ensure_one()
        if not self.env.user.has_group('kays_mintsoft_integration.group_kays_admin'):
            raise UserError(_("Only Kays Integration Administrators may enable Production mode."))
        if not self.confirm_credentials_verified or not self.confirm_live_fulfilment:
            raise UserError(_("You must check both confirmation boxes to activate Production mode."))

        self.instance_id.sudo().write({
            'production_approved': True,
            'production_approved_by': self.env.user.id,
            'production_approved_date': fields.Datetime.now(),
            'environment': 'production',
        })
        self.instance_id.message_post(
            body=_(
                "<b>Production Mode Activated</b> by %(user)s.<br/>Notes: %(notes)s"
            ) % {'user': self.env.user.name, 'notes': self.approval_notes},
        )
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Production Mode Activated'),
                'message': _('Connection "%s" is now active in Production mode.') % self.instance_id.name,
                'type': 'success',
            },
        }


class KaysTestOrderWizard(models.TransientModel):
    _name = 'kays.test.order.wizard'
    _description = 'Kays Logistics Test Order & Payload Validation Wizard'

    instance_id = fields.Many2one(
        'kays.mintsoft.instance',
        string='Connection',
        required=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        related='instance_id.company_id',
        readonly=True,
    )
    environment = fields.Selection(
        related='instance_id.environment',
        string='Connection Environment',
        readonly=True,
    )
    execution_mode = fields.Selection(
        selection=[
            ('dry_run', 'Dry Run (Validate & Preview Only — No API Write)'),
            ('test_client', 'Test Client (Submit to Approved Test Client)'),
            ('production', 'Production (Live Order Submission — Admin Only)'),
        ],
        string='Execution Mode',
        default='dry_run',
        required=True,
    )
    source_mode = fields.Selection(
        selection=[
            ('existing_order', 'Use Existing Odoo Sales Order'),
            ('manual_payload', 'Create Test Payload Manually'),
        ],
        string='Order Source',
        default='existing_order',
        required=True,
    )
    sale_order_id = fields.Many2one(
        'sale.order',
        string='Source Sales Order',
        domain="[('company_id', '=', company_id)]",
    )

    # Target & Item Fields
    mintsoft_warehouse_id = fields.Integer(string='Warehouse ID', required=True, default=3)
    mintsoft_client_id = fields.Integer(string='Mintsoft Client ID', default=0)
    product_mapping_id = fields.Many2one(
        'kays.mintsoft.product.mapping',
        string='Mapped Product SKU',
        domain="[('instance_id', '=', instance_id)]",
    )
    test_sku = fields.Char(string='Test SKU')
    quantity = fields.Integer(string='Quantity', default=1, required=True)

    # Recipient & Destination Fields
    recipient_name = fields.Char(string='Recipient')
    address_line1 = fields.Char(string='Address')
    address_line2 = fields.Char(string='Address Line 2')
    city = fields.Char(string='City')
    country_id = fields.Many2one(
        'res.country',
        string='Country',
        default=lambda self: self.env.ref('base.uk', raise_if_not_found=False),
    )
    postcode = fields.Char(string='Postcode')
    email = fields.Char(string='Recipient Email')
    phone = fields.Char(string='Recipient Phone')

    # Courier & Reference
    carrier_mapping_id = fields.Many2one(
        'kays.mintsoft.carrier.mapping',
        string='Courier Mapping',
        domain="[('instance_id', '=', instance_id)]",
    )
    courier_service_id = fields.Integer(string='Mintsoft Courier Service ID')
    courier_service_name = fields.Char(string='Courier Service Name')
    reference = fields.Char(string='External Order Reference')
    shipping_notes = fields.Text(string='Shipping Notes')

    # Confirmation & Output Fields
    live_fulfilment_warning_acknowledged = fields.Boolean(
        string='I acknowledge that submitting in Test Client or Production mode operates on live Mintsoft infrastructure and may trigger physical warehouse fulfilment.',
        default=False,
    )
    validation_passed = fields.Boolean(string='Validation Passed', default=False, readonly=True)
    validation_summary = fields.Text(string='Validation Summary', readonly=True)
    payload_preview = fields.Text(string='Generated Payload Preview', readonly=True)
    submission_result = fields.Text(string='Submission Result', readonly=True)
    returned_mintsoft_order_id = fields.Integer(string='Returned Mintsoft Order ID', readonly=True)
    created_kays_order_id = fields.Many2one('kays.mintsoft.order', string='Created Kays Order', readonly=True)

    @api.onchange('instance_id')
    def _onchange_instance_id(self):
        for wiz in self:
            if wiz.instance_id:
                wiz.mintsoft_warehouse_id = wiz.instance_id.mintsoft_warehouse_id or 3
                wiz.mintsoft_client_id = wiz.instance_id.mintsoft_client_id or 0
                wiz.courier_service_id = wiz.instance_id.default_mintsoft_courier_service_id or 0
                wiz.courier_service_name = wiz.instance_id.default_mintsoft_courier_service_name or ''

    @api.onchange('sale_order_id')
    def _onchange_sale_order_id(self):
        """Automatically populate wizard fields when an existing Odoo Sales Order (e.g. S10216) is selected."""
        for wiz in self:
            if not wiz.sale_order_id:
                continue
            so = wiz.sale_order_id
            ship = so.partner_shipping_id or so.partner_id
            wiz.recipient_name = ship.name or ship.commercial_company_name or ''
            wiz.address_line1 = ship.street or ''
            wiz.address_line2 = ship.street2 or ''
            wiz.city = ship.city or ''
            wiz.postcode = ship.zip or ''
            wiz.country_id = ship.country_id
            wiz.email = ship.email or so.partner_id.email or ''
            wiz.phone = ship.phone or so.partner_id.phone or ''
            wiz.reference = OrderService.compute_external_reference(so, wiz.instance_id) if wiz.instance_id else so.name
            wiz.shipping_notes = so.kays_delivery_notes or ''

            # Populate first physical line SKU and quantity
            phys_lines = so.order_line.filtered(
                lambda l: not l.display_type and not getattr(l, 'is_delivery', False) and l.product_id and l.product_id.type != 'service'
            )
            if phys_lines:
                first_line = phys_lines[0]
                wiz.test_sku = first_line.product_id.default_code or ''
                wiz.quantity = max(1, int(round(first_line.product_uom_qty or 1)))
                if wiz.instance_id:
                    mapping = self.env['kays.mintsoft.product.mapping'].search([
                        ('instance_id', '=', wiz.instance_id.id),
                        ('product_id', '=', first_line.product_id.id),
                    ], limit=1)
                    if mapping:
                        wiz.product_mapping_id = mapping
                        wiz.test_sku = mapping.mintsoft_sku

    @api.onchange('product_mapping_id')
    def _onchange_product_mapping_id(self):
        for wiz in self:
            if wiz.product_mapping_id:
                wiz.test_sku = wiz.product_mapping_id.mintsoft_sku

    @api.onchange('carrier_mapping_id')
    def _onchange_carrier_mapping_id(self):
        for wiz in self:
            if wiz.carrier_mapping_id:
                wiz.courier_service_id = wiz.carrier_mapping_id.mintsoft_service_id
                wiz.courier_service_name = wiz.carrier_mapping_id.mintsoft_service_name

    # -------------------------------------------------------------------------
    # Internal Validation & Payload Helpers
    # -------------------------------------------------------------------------
    def _reopen_wizard(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Create Test Order'),
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def _validate_and_build(self):
        self.ensure_one()
        errors = []
        warnings = []

        if not self.instance_id.active or not self.instance_id.enable_integration:
            errors.append(_("Selected connection '%s' is disabled.") % self.instance_id.display_name)

        if self.execution_mode == 'production':
            if not self.env.user.has_group('kays_mintsoft_integration.group_kays_admin'):
                errors.append(_("Production test orders are restricted to Kays Integration Administrators."))
            if self.instance_id.environment != 'production' or not self.instance_id.sudo().production_approved:
                errors.append(_("Connection '%s' is not in an approved Production mode.") % self.instance_id.display_name)

        if self.execution_mode == 'test_client':
            if self.instance_id.environment == 'dry_run':
                errors.append(_("Switch connection out of Dry Run mode before submitting a live Test Client order."))

        if self.source_mode == 'existing_order':
            if not self.sale_order_id:
                errors.append(_("Select an existing Odoo Sales Order."))
            else:
                svc = OrderService(self.env)
                val_res = svc.validate_sale_order(self.sale_order_id, self.instance_id, is_manual=True)
                errors.extend(val_res['errors'])
                warnings.extend(val_res['warnings'])
                payload = svc.build_mintsoft_order_payload(self.sale_order_id, self.instance_id, validation_result=val_res)
                if self.reference:
                    payload['OrderNumber'] = self.reference.strip()[:50]
                    payload['ExternalOrderReference'] = self.reference.strip()[:50]
        else:
            # Manual payload validation
            sku = (self.test_sku or '').strip()
            if not sku:
                errors.append(_("Test SKU is required."))
            if not self.quantity or self.quantity <= 0:
                errors.append(_("Quantity must be a positive integer."))
            if not (self.recipient_name or '').strip():
                errors.append(_("Recipient name is required."))
            if not (self.address_line1 or '').strip():
                errors.append(_("Street address is required."))
            if not (self.city or '').strip():
                errors.append(_("City is required."))
            if not (self.postcode or '').strip():
                errors.append(_("Postcode is required."))
            if not self.country_id or not self.country_id.code:
                errors.append(_("Country with ISO code is required."))
            if not self.mintsoft_warehouse_id or self.mintsoft_warehouse_id <= 0:
                errors.append(_("Warehouse ID must be greater than 0."))
            if not (self.reference or '').strip():
                errors.append(_("Order Reference is required."))
            if not self.courier_service_id and not (self.courier_service_name or '').strip():
                if self.instance_id.unmapped_carrier_policy == 'block':
                    errors.append(_("Select a Courier Mapping or specify a Mintsoft Courier Service."))
                else:
                    warnings.append(_("No explicit courier selected; using connection fallback."))

            first_name, last_name = OrderService.split_person_name(self.recipient_name)
            ref = (self.reference or 'TEST-MANUAL-001').strip()
            if self.execution_mode == 'test_client':
                pref = (self.instance_id.test_order_prefix or 'TEST-').strip()
                if pref and not ref.startswith(pref):
                    ref = f"{pref}{ref}"

            item_entry = {
                'SKU': sku,
                'Quantity': int(self.quantity or 1),
                'UnitPrice': 0.0,
                'UnitPriceVat': 0.0,
                'Discount': 0.0,
                'Details': f"Test Order Item {sku}",
            }
            if self.product_mapping_id and self.product_mapping_id.mintsoft_product_id > 0:
                item_entry['ProductId'] = self.product_mapping_id.mintsoft_product_id

            payload = {
                'OrderNumber': ref[:50],
                'ExternalOrderReference': ref[:50],
                'FirstName': first_name,
                'LastName': last_name,
                'Address1': (self.address_line1 or '')[:100],
                'Address2': (self.address_line2 or '')[:100],
                'Town': (self.city or '')[:100],
                'PostCode': (self.postcode or '')[:20],
                'Country': (self.country_id.code if self.country_id else 'GB')[:2],
                'Email': (self.email or '')[:100],
                'Phone': (self.phone or '')[:50],
                'WarehouseId': self.mintsoft_warehouse_id,
                'Currency': (self.instance_id.currency_id.name if self.instance_id.currency_id else 'GBP'),
                'OrderValue': 0.0,
                'DeliveryNotes': (self.shipping_notes or '')[:500],
                'Tags': 'ODOO19,TEST_WIZARD',
                'OrderItems': [item_entry],
            }
            if self.mintsoft_client_id and self.mintsoft_client_id > 0:
                payload['ClientId'] = self.mintsoft_client_id
            if self.courier_service_id and self.courier_service_id > 0:
                payload['CourierServiceId'] = self.courier_service_id
            if self.courier_service_name:
                payload['CourierService'] = self.courier_service_name

        # In Production mode, strictly block fake or placeholder customer data
        if self.execution_mode == 'production':
            fields_to_check = [
                payload.get('FirstName', ''),
                payload.get('LastName', ''),
                payload.get('Address1', ''),
                payload.get('Town', ''),
                payload.get('PostCode', ''),
                payload.get('Email', ''),
            ]
            combined_text = ' '.join(str(x) for x in fields_to_check)
            if PLACEHOLDER_PATTERNS.search(combined_text):
                errors.append(
                    _("Fake or placeholder customer data is strictly prohibited when testing in Production mode.")
                )

        valid = len(errors) == 0
        summary_lines = [
            _("Validation Status: %s") % (_("PASSED") if valid else _("FAILED")),
            _("Execution Mode: %s") % self.execution_mode,
        ]
        if errors:
            summary_lines.append("\n" + _("Errors:"))
            summary_lines.extend(f"  - {e}" for e in errors)
        if warnings:
            summary_lines.append("\n" + _("Warnings:"))
            summary_lines.extend(f"  - {w}" for w in warnings)

        self.write({
            'validation_passed': valid,
            'validation_summary': "\n".join(summary_lines),
            'payload_preview': json.dumps(sanitize_payload(payload), indent=2),
        })
        return valid, payload, errors

    # -------------------------------------------------------------------------
    # Wizard Buttons (Part F - Section 11)
    # -------------------------------------------------------------------------
    def action_validate(self):
        self._validate_and_build()
        return self._reopen_wizard()

    def action_preview_payload(self):
        self._validate_and_build()
        return self._reopen_wizard()

    def action_test_api_connection(self):
        self.ensure_one()
        diag = MintsoftClient(self.instance_id).run_connection_diagnostics()
        self.validation_summary = diag['summary_text']
        return self._reopen_wizard()

    def action_check_sku(self):
        """Verify Test SKU against Mintsoft read-only GET /api/Product/Search & LookupProductId."""
        self.ensure_one()
        sku = (self.test_sku or '').strip()
        if not sku:
            raise UserError(_("Enter a Test SKU first."))
        client = MintsoftClient(self.instance_id)
        res = client.request(
            'GET',
            '/api/Product/Search',
            params={'Search': sku, 'IncludeBundles': True},
            operation='wizard_check_sku',
            allow_404=True,
        )
        matched = None
        if isinstance(res, list):
            for p in res:
                if isinstance(p, dict) and (p.get('SKU') or '').strip() == sku:
                    matched = p
                    break
        if matched:
            self.validation_summary = _(
                "SKU Check PASSED: Found Mintsoft Product ID %(id)s — '%(name)s' (Weight: %(w)s kg)."
            ) % {'id': matched.get('ID'), 'name': matched.get('Name'), 'w': matched.get('Weight')}
        else:
            self.validation_summary = _("SKU Check FAILED: SKU '%s' was not found in Mintsoft.") % sku
        return self._reopen_wizard()

    def action_check_courier(self):
        """Verify selected courier service against GET /api/Courier/Services."""
        self.ensure_one()
        client = MintsoftClient(self.instance_id)
        services = client.request('GET', '/api/Courier/Services', operation='wizard_check_courier')
        matched = None
        if isinstance(services, list):
            for s in services:
                if not isinstance(s, dict):
                    continue
                if (self.courier_service_id and int(s.get('ID') or 0) == self.courier_service_id) or (
                    self.courier_service_name and (s.get('Name') or '').strip().lower() == self.courier_service_name.strip().lower()
                ):
                    matched = s
                    break
        if matched:
            self.courier_service_id = int(matched.get('ID') or 0)
            self.courier_service_name = matched.get('Name') or ''
            self.validation_summary = _(
                "Courier Check PASSED: Mintsoft Service ID %(id)s — '%(name)s'."
            ) % {'id': self.courier_service_id, 'name': self.courier_service_name}
        else:
            self.validation_summary = _(
                "Courier Check FAILED: Courier Service (ID=%(id)s, Name='%(name)s') not found in Mintsoft."
            ) % {'id': self.courier_service_id, 'name': self.courier_service_name or ''}
        return self._reopen_wizard()

    def action_check_duplicate(self):
        """Check whether an order with the target reference already exists in Mintsoft."""
        self.ensure_one()
        _valid, payload, _errors = self._validate_and_build()
        ref = payload.get('OrderNumber') or self.reference or ''
        client = MintsoftClient(self.instance_id)
        matches = OrderService(self.env).check_remote_duplicate(client, self.instance_id, ref)
        if matches:
            first = matches[0]
            self.validation_summary = _(
                "Duplicate Check WARNING: Order '%(ref)s' already exists in Mintsoft (Mintsoft Order ID: %(id)s)."
            ) % {'ref': ref, 'id': first.get('ID') or first.get('OrderId')}
        else:
            self.validation_summary = _(
                "Duplicate Check PASSED: No existing order with reference '%s' found in Mintsoft."
            ) % ref
        return self._reopen_wizard()

    def action_submit_test_order(self):
        """
        Execute test order according to execution_mode:
        - dry_run: Never sends a write request; stores local validation & payload preview.
        - test_client / production: Requires confirmation checkbox and all mandatory checks passing before PUT /api/Order.
        """
        self.ensure_one()
        valid, payload, errors = self._validate_and_build()
        if not valid:
            raise UserError(_("Cannot submit test order until all validation errors are resolved:\n%s") % "\n".join(errors))

        if self.execution_mode == 'dry_run':
            self.submission_result = json.dumps({
                'execution_mode': 'dry_run',
                'write_sent': False,
                'status': 'validated_locally',
                'message': _('Dry Run completed successfully. No write request was sent to Mintsoft.'),
                'order_number': payload.get('OrderNumber'),
            }, indent=2)
            return self._reopen_wizard()

        # Live Test Client or Production submission requires explicit user acknowledgment
        if not self.live_fulfilment_warning_acknowledged:
            raise UserError(
                _("Please acknowledge the warning that submitting in %s mode operates on live Mintsoft infrastructure.")
                % self.execution_mode
            )

        client = MintsoftClient(self.instance_id)
        ref = payload.get('OrderNumber')
        dups = OrderService(self.env).check_remote_duplicate(client, self.instance_id, ref)
        if dups:
            raise UserError(
                _("Duplicate order '%(ref)s' already exists in Mintsoft (Order ID: %(id)s). Submission blocked.")
                % {'ref': ref, 'id': dups[0].get('ID') or dups[0].get('OrderId')}
            )

        if self.source_mode == 'existing_order' and self.sale_order_id:
            ko = OrderService(self.env).export_sale_order(self.sale_order_id, instance=self.instance_id, is_manual=True)
            self.write({
                'returned_mintsoft_order_id': ko.mintsoft_order_id,
                'created_kays_order_id': ko.id,
                'submission_result': ko.response_summary or ko.last_error or '',
            })
            return self._reopen_wizard()

        # Manual payload submission via PUT /api/Order
        res = client.request(
            'PUT',
            '/api/Order',
            json_payload=payload,
            operation='submit_test_order_wizard',
        )
        results_list = res if isinstance(res, list) else ([res] if isinstance(res, dict) else [])
        ms_order_id = 0
        if results_list and isinstance(results_list[0], dict):
            ms_order_id = int(results_list[0].get('OrderId') or 0)

        self.write({
            'returned_mintsoft_order_id': ms_order_id,
            'submission_result': json.dumps(sanitize_payload(res), indent=2),
        })
        return self._reopen_wizard()

    def action_view_result(self):
        self.ensure_one()
        if self.created_kays_order_id:
            return {
                'type': 'ir.actions.act_window',
                'name': _('Kays Order — %s') % self.created_kays_order_id.name,
                'res_model': 'kays.mintsoft.order',
                'res_id': self.created_kays_order_id.id,
                'view_mode': 'form',
                'target': 'current',
            }
        return self._reopen_wizard()
