# Copyright 2020 ACSONE SA
# Copyright 2021 Camptocamp SA
# @author Simone Orsi <simahawk@gmail.com>
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).

import base64
import logging
from ast import literal_eval
from collections import defaultdict

from odoo import _, api, exceptions, fields, models
from odoo.exceptions import AccessError

from ..utils import exchange_record_job_identity_exact, get_checksum

_logger = logging.getLogger(__name__)


class EDIExchangeRecord(models.Model):
    """
    Define an exchange record.
    """

    _name = "edi.exchange.record"
    _inherit = "mail.thread"
    _description = "EDI exchange Record"
    _order = "exchanged_on desc, id desc"
    _rec_name = "identifier"

    identifier = fields.Char(required=True, index=True, readonly=True, copy=False)
    external_identifier = fields.Char(index=True, readonly=True, copy=False)
    type_id = fields.Many2one(
        string="Exchange type",
        comodel_name="edi.exchange.type",
        required=True,
        ondelete="cascade",
        auto_join=True,
        index=True,
    )
    direction = fields.Selection(related="type_id.direction")
    backend_id = fields.Many2one(comodel_name="edi.backend", required=True)
    model = fields.Char(index=True, required=False, readonly=True)
    res_id = fields.Many2oneReference(
        string="Record",
        index=True,
        required=False,
        readonly=True,
        model_field="model",
        copy=False,
    )
    related_record_exists = fields.Boolean(compute="_compute_related_record_exists")
    related_name = fields.Char(compute="_compute_related_name", compute_sudo=True)
    exchange_file = fields.Binary(attachment=True, copy=False)
    exchange_filename = fields.Char(
        compute="_compute_exchange_filename", readonly=False, store=True
    )
    exchange_filechecksum = fields.Char(
        compute="_compute_exchange_filechecksum", store=True
    )
    exchanged_on = fields.Datetime(
        help="Sent or received on this date.",
        compute="_compute_exchanged_on",
        store=True,
        readonly=False,
    )
    edi_exchange_state = fields.Selection(
        string="Exchange state",
        readonly=True,
        copy=False,
        default="new",
        index=True,
        selection=[
            # Common states
            ("new", "New"),
            ("validate_error", "Error on validation"),
            # output exchange states
            ("output_pending", "Waiting to be sent"),
            ("output_error_on_send", "error on send"),
            ("output_sent", "Sent"),
            ("output_sent_and_processed", "Sent and processed"),
            ("output_sent_and_error", "Sent and error"),
            # input exchange states
            ("input_pending", "Waiting to be received"),
            ("input_received", "Received"),
            ("input_receive_error", "Error on reception"),
            ("input_processed", "Processed"),
            ("input_processed_error", "Error on process"),
        ],
    )
    exchange_error = fields.Text(string="Exchange error", readonly=True, copy=False)
    # Relations w/ other records
    parent_id = fields.Many2one(
        comodel_name="edi.exchange.record",
        help="Original exchange which originated this record",
    )
    related_exchange_ids = fields.One2many(
        string="Related records",
        comodel_name="edi.exchange.record",
        inverse_name="parent_id",
    )
    ack_expected = fields.Boolean(compute="_compute_ack_expected")
    # TODO: shall we add a constrain on the direction?
    # In theory if the record is outgoing the ack should be incoming and vice versa.
    ack_exchange_id = fields.Many2one(
        string="ACK exchange",
        comodel_name="edi.exchange.record",
        help="ACK generated for current exchange.",
        compute="_compute_ack_exchange_id",
        store=True,
    )
    ack_received_on = fields.Datetime(
        string="ACK received on", related="ack_exchange_id.exchanged_on"
    )
    retryable = fields.Boolean(
        compute="_compute_retryable",
        help="The record state can be rolled back manually in case of failure.",
    )
    related_queue_jobs_count = fields.Integer(
        compute="_compute_related_queue_jobs_count"
    )
    company_id = fields.Many2one("res.company", string="Company")

    _sql_constraints = [
        ("identifier_uniq", "unique(identifier)", "The identifier must be unique."),
        (
            "external_identifier_uniq",
            "unique(external_identifier, backend_id, type_id)",
            "The external_identifier must be unique for a type and a backend.",
        ),
    ]

    @api.depends("model", "res_id")
    def _compute_related_name(self):
        for rec in self:
            related_record = rec.record
            rec.related_name = related_record.display_name if related_record else ""

    @api.depends("model", "type_id")
    def _compute_exchange_filename(self):
        for rec in self:
            if not rec.type_id:
                continue
            if not rec.exchange_filename:
                rec.exchange_filename = rec.type_id._make_exchange_filename(rec)

    @api.depends("exchange_file")
    def _compute_exchange_filechecksum(self):
        for rec in self:
            content = rec.exchange_file or ""
            if not isinstance(content, bytes):
                content = content.encode()
            rec.exchange_filechecksum = get_checksum(content)

    @api.depends("edi_exchange_state")
    def _compute_exchanged_on(self):
        for rec in self:
            if rec.edi_exchange_state in ("input_received", "output_sent"):
                rec.exchanged_on = fields.Datetime.now()

    @api.constrains("edi_exchange_state")
    def _constrain_edi_exchange_state(self):
        for rec in self:
            if rec.edi_exchange_state in ("new", "validate_error"):
                continue
            if not rec.edi_exchange_state.startswith(rec.direction):
                raise exceptions.ValidationError(
                    _("Exchange state must respect direction!")
                )

    @api.depends("related_exchange_ids.type_id")
    def _compute_ack_exchange_id(self):
        for rec in self:
            rec.ack_exchange_id = rec._get_ack_record()

    def _get_ack_record(self):
        if not self.type_id.ack_type_id:
            return None
        return fields.first(
            self.related_exchange_ids.filtered(
                lambda x: x.type_id == self.type_id.ack_type_id
            ).sorted("id", reverse=True)
        )

    def _compute_ack_expected(self):
        for rec in self:
            rec.ack_expected = bool(self.type_id.ack_type_id)

    @api.depends("res_id", "model")
    def _compute_related_record_exists(self):
        for rec in self:
            rec.related_record_exists = bool(rec.record)

    def needs_ack(self):
        return self.type_id.ack_type_id and not self.ack_exchange_id

    _rollback_state_mapping = {
        # From: to
        "output_error_on_send": "output_pending",
        "output_sent_and_error": "output_pending",
        "input_receive_error": "input_pending",
        "input_processed_error": "input_received",
    }

    @api.depends("edi_exchange_state")
    def _compute_retryable(self):
        for rec in self:
            rec.retryable = rec.edi_exchange_state in self._rollback_state_mapping

    @property
    def record(self):
        # In some case the res_model (and res_id) could be empty so we have to load
        # data from parent
        if not self.model and not self.parent_id:
            return None
        if not self.model and self.parent_id:
            return self.parent_id.record
        return self.env[self.model].browse(self.res_id).exists()

    def _set_file_content(
        self, output_string, encoding="utf-8", field_name="exchange_file"
    ):
        """Handy method to no have to convert b64 back and forth."""
        self.ensure_one()
        if not isinstance(output_string, bytes):
            output_string = bytes(output_string, encoding)
        self[field_name] = base64.b64encode(output_string)

    def _get_file_content(
        self, field_name="exchange_file", binary=True, as_bytes=False
    ):
        """Handy method to not have to convert b64 back and forth."""
        self.ensure_one()
        encoding = self.type_id.encoding or "UTF-8"
        decoding_error_handler = self.type_id.encoding_in_error_handler or "strict"
        if not self[field_name]:
            return ""
        if binary:
            res = base64.b64decode(self[field_name])
            return (
                res.decode(encoding, errors=decoding_error_handler)
                if not as_bytes
                else res
            )
        return self[field_name]

    @api.depends("identifier", "res_id", "model", "type_id")
    def _compute_display_name(self):
        for rec in self:
            rec_name = (
                rec.record.display_name if rec.res_id and rec.model else rec.identifier
            )
            rec.display_name = f"[{rec.type_id.name}] {rec_name}"

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            vals["identifier"] = self._get_identifier()
        records = super().create(vals_list)
        for rec in records:
            if rec._quick_exec_enabled():
                rec._execute_next_action()
        return records

    @api.model
    def _get_identifier(self):
        return self.env["ir.sequence"].next_by_code("edi.exchange")

    def _quick_exec_enabled(self):
        if self.env.context.get("edi__skip_quick_exec"):
            return False
        return self.type_id.quick_exec and self.backend_id.active

    def _execute_next_action(self):
        # The backend already knows how to handle records
        # according to their direction and status.
        # Let it decide.
        if self.type_id.direction == "output":
            self.backend_id._check_output_exchange_sync(record_ids=self.ids)
        else:
            self.backend_id._check_input_exchange_sync(record_ids=self.ids)

    @api.constrains("backend_id", "type_id")
    def _constrain_backend(self):
        for rec in self:
            if rec.type_id.backend_id:
                if rec.type_id.backend_id != rec.backend_id:
                    raise exceptions.ValidationError(
                        _("Backend must match with exchange type's backend!")
                    )
            else:
                if rec.type_id.backend_type_id != rec.backend_id.backend_type_id:
                    raise exceptions.ValidationError(
                        _("Backend type must match with exchange type's backend type!")
                    )

    @property
    def _exchange_status_messages(self):
        return {
            # status: message
            "generate_ok": _("Exchange data generated"),
            "send_ok": _("Exchange sent"),
            "send_ko": _(
                "An error happened while sending. Please check exchange record info."
            ),
            "process_ok": _("Exchange processed successfully"),
            "process_ko": _("Exchange processed with errors"),
            "receive_ok": _("Exchange received successfully"),
            "receive_ko": _("Exchange not received"),
            "ack_received": _("ACK file received."),
            "ack_missing": _("ACK file is required for this exchange but not found."),
            "ack_received_error": _("ACK file received but contains errors."),
            "validate_ko": _("Exchange not valid"),
        }

    def _exchange_status_message(self, key):
        return self._exchange_status_messages[key]

    def action_exchange_generate(self, **kw):
        self.ensure_one()
        return self.backend_id.exchange_generate(self, **kw)

    def action_exchange_send(self):
        self.ensure_one()
        return self.backend_id.exchange_send(self)

    def action_exchange_process(self):
        self.ensure_one()
        return self.backend_id.exchange_process(self)

    def action_exchange_receive(self):
        self.ensure_one()
        return self.backend_id.exchange_receive(self)

    def exchange_create_ack_record(self, **kw):
        return self.exchange_create_child_record(
            exc_type=self.type_id.ack_type_id, **kw
        )

    def exchange_create_child_record(self, exc_type=None, **kw):
        exc_type = exc_type or self.type_id
        values = self._exchange_child_record_values()
        values.update(**kw)
        return self.backend_id.create_record(exc_type.code, values)

    def _exchange_child_record_values(self):
        return {
            "parent_id": self.id,
            "model": self.model,
            "res_id": self.res_id,
        }

    def action_retry(self):
        for rec in self:
            rec._retry_exchange_action()

    def _retry_exchange_action(self):
        """Move back to precedent state to retry exchange action if failed."""
        if not self.retryable:
            return False
        new_state = self._rollback_state_mapping[self.edi_exchange_state]
        fname = "edi_exchange_state"
        self[fname] = new_state
        display_state = self._fields[fname].convert_to_export(self[fname], self)
        self.message_post(
            body=_("Action retry: state moved back to '%s'") % display_state
        )
        if self._quick_exec_enabled():
            self._execute_next_action()
        return True

    def action_regenerate(self):
        for rec in self:
            rec.action_exchange_generate(force=True)

    def action_open_related_record(self):
        self.ensure_one()
        if not self.related_record_exists:
            return {}
        return self.record.get_formview_action()

    def _set_related_record(self, odoo_record):
        self.sudo().update({"model": odoo_record._name, "res_id": odoo_record.id})

    def action_open_related_exchanges(self):
        self.ensure_one()
        if not self.related_exchange_ids:
            return {}
        xmlid = "edi_oca.act_open_edi_exchange_record_view"
        action = self.env["ir.actions.act_window"]._for_xml_id(xmlid)
        action["domain"] = [("id", "in", self.related_exchange_ids.ids)]
        return action

    def notify_action_complete(self, action, message=None):
        """Notify current record that an edi action has been completed.

        Implementers should take care of calling this method
        if they work on records w/o calling edi_backend methods (eg: action_send).

        Implementers can hook to this method to do something after any action ends.
        """
        if message:
            self._notify_related_record(message)

        # Trigger generic action complete event on exchange record
        event_name = f"{action}_complete"
        self._trigger_edi_event(event_name)
        if self.related_record_exists:
            # Trigger specific event on related record
            self._trigger_edi_event(event_name, target=self.record)

    def _notify_related_record(self, message, level="info"):
        """Post notification on the original record."""
        if not self.related_record_exists or not hasattr(
            self.record, "message_post_with_source"
        ):
            return
        self.record.message_post_with_source(
            "edi_oca.message_edi_exchange_link",
            render_values={
                "backend": self.backend_id,
                "exchange_record": self,
                "message": message,
                "level": level,
            },
            subtype_id=self.env.ref("mail.mt_note").id,
        )

    def _trigger_edi_event_make_name(self, name, suffix=None):
        return "on_edi_exchange_{name}{suffix}".format(
            name=name,
            suffix=("_" + suffix) if suffix else "",
        )

    def _trigger_edi_event(self, name, suffix=None, target=None, **kw):
        """Trigger a component event linked to this backend and edi exchange."""
        name = self._trigger_edi_event_make_name(name, suffix=suffix)
        target = target or self
        target._event(name).notify(self, **kw)

    def _notify_done(self):
        self._notify_related_record(self._exchange_status_message("process_ok"))
        self._trigger_edi_event("done")

    def _notify_error(self, message_key):
        self._notify_related_record(
            self._exchange_status_message(message_key),
            level="error",
        )
        self._trigger_edi_event("error")

    def _notify_ack_received(self):
        self._notify_related_record(self._exchange_status_message("ack_received"))
        self._trigger_edi_event("done", suffix="ack_received")

    def _notify_ack_missing(self):
        self._notify_related_record(
            self._exchange_status_message("ack_missing"),
            level="warning",
        )
        self._trigger_edi_event("done", suffix="ack_missing")

    def _notify_ack_received_error(self):
        self._notify_related_record(
            self._exchange_status_message("ack_received_error"),
        )
        self._trigger_edi_event("done", suffix="ack_received_error")

    @api.model
    def _search(self, domain, offset=0, limit=None, order=None):
        query = super()._search(
            domain=domain,
            offset=offset,
            limit=limit,
            order=order,
        )
        if self.env.is_superuser():
            # restrictions do not apply for the superuser
            return query

        # TODO highlight orphaned EDI records in UI:
        #  - self.model + self.res_id are set
        #  - self.record returns empty recordset
        # Remark: self.record is @property, not field

        if query.is_empty():
            return query
        orig_ids = list(query)
        ids = set(orig_ids)
        result = []
        model_data = defaultdict(lambda: defaultdict(set))
        sub_query = """
            SELECT id, res_id, model
            FROM %(table)s
            WHERE id = ANY (%%(ids)s)
        """
        for sub_ids in self._cr.split_for_in_conditions(ids):
            self._cr.execute(
                sub_query % {"table": self._table},
                dict(ids=list(sub_ids)),
            )
            for eid, res_id, model in self._cr.fetchall():
                if not model:
                    result.append(eid)
                    continue
                model_data[model][res_id].add(eid)

        for model, targets in model_data.items():
            try:
                self.env[model].check_access("read")
            except AccessError:  # no read access rights
                continue
            recs = self.env[model].browse(list(targets))
            missing = recs - recs.exists()
            if missing:
                for res_id in missing.ids:
                    _logger.warning(
                        "Deleted record %s,%s is referenced by edi.exchange.record %s",
                        model,
                        res_id,
                        list(targets[res_id]),
                    )
                recs = recs - missing
            allowed = list(
                self.env[model]
                .with_context(active_test=False)
                ._search([("id", "in", recs.ids)])
            )
            if self.env.is_system():
                # Group "Settings" can list exchanges where record is deleted
                allowed.extend(missing.ids)
            for target_id in allowed:
                result += list(targets[target_id])
        if len(orig_ids) == limit and len(result) < len(orig_ids):
            extend_query = self._search(
                domain,
                offset=offset + len(orig_ids),
                limit=limit,
                order=order,
            )
            extend_ids = list(extend_query)
            result.extend(extend_ids[: limit - len(result)])

        # Restore original ordering
        result = [x for x in orig_ids if x in result]
        if set(orig_ids) != set(result):
            # Create a virgin query
            query = self.browse(result)._as_query()
        return query

    def read(self, fields=None, load="_classic_read"):
        """Override to explicitely call check_access_rule, that is not called
        by the ORM. It instead directly fetches ir.rules and apply them."""
        self.check_access("read")
        return super().read(fields=fields, load=load)

    def check_access(self, operation):
        """In order to check if we can access a record, we are checking if we can access
        the related document"""
        super().check_access(operation)
        if self.env.is_superuser():
            return
        default_checker = self.env["edi.exchange.consumer.mixin"].get_edi_access
        by_model_rec_ids = defaultdict(set)
        by_model_checker = {}
        for exc_rec in self.sudo():
            if not exc_rec.related_record_exists:
                continue
            by_model_rec_ids[exc_rec.model].add(exc_rec.res_id)
            if exc_rec.model not in by_model_checker:
                by_model_checker[exc_rec.model] = getattr(
                    self.env[exc_rec.model], "get_edi_access", default_checker
                )

        for model, rec_ids in by_model_rec_ids.items():
            records = self.env[model].browse(rec_ids).with_user(self._uid)
            checker = by_model_checker[model]
            for record in records:
                check_operation = checker(
                    [record.id], operation, model_name=record._name
                )
                record.check_access(check_operation)

    def write(self, vals):
        self.check_access("write")
        return super().write(vals)

    def _job_delay_params(self):
        params = {}
        channel = self.type_id.sudo().job_channel_id
        if channel:
            params["channel"] = channel.complete_name
        # Avoid generating the same job for the same record if existing
        params["identity_key"] = exchange_record_job_identity_exact
        return params

    def with_delay(self, **kw):
        params = self._job_delay_params()
        params.update(kw)
        return super().with_delay(**params)

    def delayable(self, **kw):
        params = self._job_delay_params()
        params.update(kw)
        return super().delayable(**params)

    def _job_retry_params(self):
        return {}

    def _compute_related_queue_jobs_count(self):
        for rec in self:
            # TODO: We should refactor the object field on queue_job to use jsonb field
            # so that we can search directly into it.
            rec.related_queue_jobs_count = rec.env["queue.job"].search_count(
                [("func_string", "like", str(rec))]
            )

    def action_view_related_queue_jobs(self):
        self.ensure_one()
        xmlid = "queue_job.action_queue_job"
        action = self.env["ir.actions.act_window"]._for_xml_id(xmlid)
        # Searching based on task name:
        # Ex: `edi.exchange.record(1,).action_exchange_send()`
        # TODO: We should refactor the object field on queue_job to use jsonb field
        # so that we can search directly into it.
        action["domain"] = [("func_string", "like", str(self))]
        # Purge default search filters from ctx to avoid hiding records
        ctx = action.get("context", {})
        if isinstance(ctx, str):
            ctx = literal_eval(ctx)
        # Update the current contexts
        ctx.update(self.env.context)
        action["context"] = {
            k: v for k, v in ctx.items() if not k.startswith("search_default_")
        }
        # Drop ID otherwise the context will be loaded from the action's record
        action.pop("id")
        return action
