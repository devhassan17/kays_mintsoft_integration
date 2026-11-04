from odoo import fields, models, _

from ..services.order_service import OrderService


class KaysRetryWizard(models.TransientModel):
    _name = 'kays.retry.wizard'
    _description = 'Kays Logistics Bulk Reconciliation & Retry Wizard'

    company_id = fields.Many2one(
        'res.company',
        string='Company',
        default=lambda self: self.env.company,
    )
    instance_id = fields.Many2one(
        'kays.mintsoft.instance',
        string='Connection (Optional Filter)',
        domain="[('company_id', '=', company_id)]",
    )
    target_type = fields.Selection(
        selection=[
            ('failed_jobs', 'Retry Failed & Scheduled Sync Jobs'),
            ('unknown_orders', 'Reconcile Unknown-Outcome Orders'),
            ('failed_orders', 'Retry Failed Order Exports'),
            ('failed_webhooks', 'Reprocess Failed Webhook Events'),
        ],
        string='Retry Target',
        default='failed_jobs',
        required=True,
    )
    include_dead_letter = fields.Boolean(
        string='Include Dead-Letter Jobs (Reset Retry Count)',
        default=False,
    )

    def action_run_retries(self):
        self.ensure_one()
        dom = []
        if self.company_id:
            dom.append(('company_id', '=', self.company_id.id))
        if self.instance_id:
            dom.append(('instance_id', '=', self.instance_id.id))

        count = 0
        if self.target_type == 'failed_jobs':
            states = ['failed', 'retry_scheduled', 'pending']
            if self.include_dead_letter:
                states.append('dead_letter')
            jobs = self.env['kays.mintsoft.sync.job'].search(dom + [('state', 'in', states)], limit=100)
            for job in jobs:
                if self.include_dead_letter and job.state == 'dead_letter':
                    job.retry_count = 0
                job.action_retry_job_manually()
                count += 1

        elif self.target_type == 'unknown_orders':
            orders = self.env['kays.mintsoft.order'].search(dom + [('export_status', '=', 'unknown_outcome')], limit=100)
            svc = OrderService(self.env)
            for ko in orders:
                svc.reconcile_unknown_outcome_order(ko)
                count += 1

        elif self.target_type == 'failed_orders':
            orders = self.env['kays.mintsoft.order'].search(dom + [('export_status', '=', 'failed')], limit=100)
            orders.action_retry_export()
            count = len(orders)

        elif self.target_type == 'failed_webhooks':
            events = self.env['kays.mintsoft.webhook.event'].search(dom + [('state', '=', 'failed')], limit=100)
            events.action_reprocess_webhook()
            count = len(events)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Bulk Retry Executed'),
                'message': _('Processed %d record(s).') % count,
                'type': 'success',
            },
        }
