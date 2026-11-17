from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.tools.safe_eval import safe_eval


class ResCompany(models.Model):
    _inherit = 'res.company'

    kays_enabled = fields.Boolean(
        string='Enable Kays Logistics Integration',
        default=False,
        help='Enable Kays Logistics (Mintsoft) fulfilment integration for this company.',
    )
    kays_reference_prefix = fields.Char(
        string='Kays Order Reference Prefix',
        help='Company-specific prefix for external order references (e.g. MCUK).',
    )
    kays_default_instance_id = fields.Many2one(
        'kays.mintsoft.instance',
        string='Default Kays Connection',
        domain="[('company_id', '=', id)]",
    )
    kays_instance_ids = fields.One2many(
        'kays.mintsoft.instance',
        'company_id',
        string='Kays Connections',
    )
    kays_instance_count = fields.Integer(
        compute='_compute_kays_instance_info',
        string='Connections Count',
    )
    kays_ambiguous_routing_warning = fields.Boolean(
        compute='_compute_kays_instance_info',
        string='Ambiguous Connection Routing Warning',
    )

    @api.depends('kays_instance_ids.active', 'kays_instance_ids.is_default', 'kays_default_instance_id')
    def _compute_kays_instance_info(self):
        for comp in self:
            active_insts = comp.kays_instance_ids.filtered('active')
            comp.kays_instance_count = len(active_insts)
            if len(active_insts) <= 1:
                comp.kays_ambiguous_routing_warning = False
            else:
                defaults = active_insts.filtered('is_default')
                comp.kays_ambiguous_routing_warning = len(defaults) != 1 and not comp.kays_default_instance_id

    def _get_default_kays_instance(self, sale_order=None):
        """
        Resolve the unambiguous Kays connection instance for this company.
        Supports explicit routing domains when multiple connections exist for one company.
        """
        self.ensure_one()
        if not self.kays_enabled:
            return self.env['kays.mintsoft.instance']

        active_insts = self.env['kays.mintsoft.instance'].with_company(self).search([
            ('company_id', '=', self.id),
            ('active', '=', True),
            ('enable_integration', '=', True),
        ], order='is_default desc, sequence asc, id asc')

        if not active_insts:
            return self.env['kays.mintsoft.instance']
        if len(active_insts) == 1:
            return active_insts[0]

        # If sale_order is provided, evaluate routing_domain on connections that define one
        if sale_order:
            matched_by_domain = self.env['kays.mintsoft.instance']
            for inst in active_insts:
                dom_str = (inst.routing_domain or '').strip()
                if dom_str and dom_str != '[]':
                    try:
                        with self.env.cr.savepoint():
                            dom = safe_eval(dom_str)
                            if isinstance(dom, list) and sale_order.filtered_domain(dom):
                                matched_by_domain |= inst
                    except Exception:
                        continue
            if len(matched_by_domain) == 1:
                return matched_by_domain[0]
            if len(matched_by_domain) > 1:
                raise UserError(
                    _("Ambiguous Kays connection routing for company '%s': multiple connections matched order %s.")
                    % (self.display_name, sale_order.name)
                )

        if self.kays_default_instance_id and self.kays_default_instance_id in active_insts:
            return self.kays_default_instance_id

        defaults = active_insts.filtered('is_default')
        if len(defaults) == 1:
            return defaults[0]

        raise UserError(
            _("Multiple active Kays connections exist for company '%s' without a single Default Connection or unambiguous routing rule.")
            % self.display_name
        )

    def action_open_kays_connections(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Kays Connections — %s') % self.name,
            'res_model': 'kays.mintsoft.instance',
            'view_mode': 'list,form',
            'domain': [('company_id', '=', self.id)],
            'context': {'default_company_id': self.id},
        }
