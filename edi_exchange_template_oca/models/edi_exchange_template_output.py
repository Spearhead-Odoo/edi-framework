# Copyright 2020 ACSONE SA
# @author Simone Orsi <simahawk@gmail.com>
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).
import json

import lxml.etree as etree

from odoo import fields, models

from ..utils import xml_purge_nswrapper


class EDIExchangeOutputTemplate(models.Model):
    """Define an output template to generate outgoing records' content."""

    _name = "edi.exchange.template.output"
    _inherit = "edi.exchange.template.mixin"
    _description = "EDI Exchange Output Template"

    generator = fields.Selection(
        [("json", "JSON"), ("qweb", "Qweb Template"), ("report", "Report")],
        required=True,
        default="qweb",
    )
    output_type = fields.Char(required=True)
    # TODO: add a good domain (maybe add a new flag or category to ir.ui.view)
    # Options:
    # 1. add a flag "edi_template" to ir.ui.view
    # 2. set model="edi.exchange.template.output" on the view
    #    As templates are defined using `<template />` tag
    #    I'm not sure this is a good option.
    # 3. what else?
    template_id = fields.Many2one(
        string="Qweb Template",
        comodel_name="ir.ui.view",
        required=False,
        ondelete="restrict",
    )
    report_id = fields.Many2one(
        comodel_name="ir.actions.report",
        ondelete="restrict",
    )
    # TODO: find a way to prevent editing "master templates"
    # This would allow editing only a copy of the original template
    # so that you can always check or rollback to it.
    template_arch = fields.Text(
        string="QWeb arch",
        related="template_id.arch_db",
        readonly=False,
    )
    template_key = fields.Char(related="template_id.xml_id", string="Template key")
    prettify = fields.Boolean(help="Prettify output. Works for XML output only.")

    def _default_code_snippet_docs(self):
        return (
            super()._default_code_snippet_docs()
            + """
        * exchange_record (edi.exchange.record)
        * record (real odoo record related to this exchange)
        * backend (current backend)
        * template (current template record)
        * utc_now
        * date_to_string
        * render_edi_template
        * get_info_provider
        * info
        """
        )

    def exchange_generate(self, exchange_record, **kw):
        """Generate output for given record using related QWeb template."""
        method = "_generate_" + self.generator
        try:
            generator = getattr(self, method)
        except AttributeError:
            raise NotImplementedError(f"`{method}` not found") from AttributeError
        result = generator(exchange_record, **kw)
        return self._post_process_output(result)

    def _generate_qweb(self, exchange_record, **kw):
        tmpl = self.template_id
        values = self._get_render_values(exchange_record, **kw)
        return tmpl._render_template(tmpl.id, values)

    def _generate_report(self, exchange_record, **kw):
        report = self.report_id
        values = self._get_render_values(exchange_record, **kw)
        res_ids = values.get("res_ids", [])
        return self.env["ir.actions.report"]._render(report, res_ids, data=values)[0]

    def _generate_json(self, exchange_record, **kw):
        # We expect to have json data into `payload` key.
        # See validator `_evaluate_code_snippet_validate_json` in the mixin.
        result = self._get_render_values(exchange_record, **kw)
        return result

    def _get_render_values(self, exchange_record, **kw):
        """Collect values to render current template."""
        values = {
            "exchange_record": exchange_record,
            "record": exchange_record.record,
            "backend": exchange_record.backend_id,
            "template": self,
            "render_edi_template": self._render_template,
            "get_info_provider": self._get_info_provider,
            "info": {},
        }
        values.update(kw)
        values.update(self._time_utils())
        values.update(self._evaluate_code_snippet(**values))
        return values

    def _render_template(self, exchange_record, code, **kw):
        """Render another template matching `code`.

        This is very handy to render templates from other templates.
        """
        tmpl = self.search([("code", "=", code)], limit=1)
        tmpl.ensure_one()
        return tmpl.exchange_generate(exchange_record, **kw)

    def _post_process_output(self, output):
        """Post process generated output."""
        processor = getattr(
            self,
            f"_post_process_output_{self.generator}",
            lambda result: result,
        )
        return processor(output)

    def _post_process_output_qweb(self, output):
        if self.output_type == "xml":
            output = xml_purge_nswrapper(output)
            if self.prettify:
                output = self._prettify_xml(output)
        return output

    def _post_process_output_json(self, output):
        return json.dumps(output["payload"])

    def _prettify_xml(self, xml_string):
        return etree.tostring(etree.fromstring(xml_string), pretty_print=True)

    def _get_info_provider(self, exchange_record, work_ctx=None, usage=None, **kw):
        """Retrieve component providing info to render a template.

        TODO: improve this description.
        TODO: add tests
        """
        default_work_ctx = dict(
            exchange_record=exchange_record,
            record=exchange_record.record,
        )
        default_work_ctx.update(work_ctx or {})
        backend = exchange_record.backend_id
        model = exchange_record.model or backend._name
        usage_candidates = [usage or self.code + ".info"]
        provider = backend._find_component(
            model, usage_candidates, work_ctx=default_work_ctx, **kw
        )
        return provider
