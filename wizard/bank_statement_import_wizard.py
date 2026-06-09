# -*- coding: utf-8 -*-
import base64
import csv
import io
import re
from datetime import datetime
from odoo import api, fields, models, _
from odoo.exceptions import UserError

class BankStatementImportWizard(models.TransientModel):
    _name = 'bank.statement.import.wizard'
    _description = 'Import & Reconcile Nigerian Statements'

    file = fields.Binary(string='Statement File (CSV)', required=True)
    file_name = fields.Char(string='File Name')
    journal_id = fields.Many2one(
        'account.journal', 
        string='Bank Journal', 
        required=True, 
        domain=[('type', '=', 'bank')]
    )
    bank_type = fields.Selection([
        ('zenith', 'Zenith Bank'),
        ('gtbank', 'Guaranty Trust Bank (GTBank)'),
        ('generic', 'Generic / Standard CSV')
    ], string='Bank Format', default='generic', required=True)

    def action_import_and_reconcile(self):
        self.ensure_one()
        if not self.file_name or not self.file_name.endswith('.csv'):
            raise UserError(_("Please upload a valid CSV file."))

        # Decode the file
        try:
            csv_data = base64.b64decode(self.file)
            csv_text = csv_data.decode('utf-8-sig') # handle BOM if present
        except Exception as e:
            raise UserError(_("Failed to read file: %s") % str(e))

        # Parse CSV
        reader = csv.reader(io.StringIO(csv_text))
        headers = None
        rows = []
        for r in reader:
            if not r:
                continue
            if not headers:
                # Try to detect header row
                # Look for typical header terms (date, description, narration, reference, amount, debit, credit)
                row_joined = " ".join(r).lower()
                if any(x in row_joined for x in ['date', 'narration', 'description', 'reference', 'ref']):
                    headers = [h.strip().lower() for h in r]
                    continue
                else:
                    # Skip leading lines (e.g. account statement metadata)
                    continue
            rows.append(r)

        if not headers:
            raise UserError(_("Could not locate headers in the CSV file. Ensure it contains columns like 'Date', 'Description/Narration', 'Reference', and 'Amount' (or 'Debit'/'Credit')."))

        # Map headers
        header_map = {}
        for idx, h in enumerate(headers):
            if 'date' in h:
                header_map['date'] = idx
            elif 'narration' in h or 'description' in h or 'particulars' in h:
                header_map['label'] = idx
            elif 'ref' in h or 'document' in h or 'trans' in h:
                header_map['ref'] = idx
            elif 'debit' in h or 'withdrawal' in h:
                header_map['debit'] = idx
            elif 'credit' in h or 'deposit' in h:
                header_map['credit'] = idx
            elif 'amount' in h:
                header_map['amount'] = idx

        # If amount column not mapped but debit/credit are, we'll calculate amount
        if 'amount' not in header_map and ('debit' not in header_map or 'credit' not in header_map):
            # Fallback to positional columns if naming is non-standard
            header_map.setdefault('date', 0)
            header_map.setdefault('label', 1)
            header_map.setdefault('ref', 2)
            if len(headers) > 4:
                header_map.setdefault('debit', 3)
                header_map.setdefault('credit', 4)
            else:
                header_map.setdefault('amount', 3)

        lines_created = []
        
        # Create bank statement lines
        for r_idx, r in enumerate(rows):
            try:
                raw_date = r[header_map['date']].strip()
                label = r[header_map['label']].strip() if 'label' in header_map and header_map['label'] < len(r) else ''
                ref = r[header_map['ref']].strip() if 'ref' in header_map and header_map['ref'] < len(r) else ''
                
                # Parse amount
                amount = 0.0
                if 'amount' in header_map and header_map['amount'] < len(r):
                    raw_amount = r[header_map['amount']].strip().replace(',', '')
                    amount = float(raw_amount) if raw_amount else 0.0
                else:
                    raw_debit = r[header_map['debit']].strip().replace(',', '') if 'debit' in header_map and header_map['debit'] < len(r) else ''
                    raw_credit = r[header_map['credit']].strip().replace(',', '') if 'credit' in header_map and header_map['credit'] < len(r) else ''
                    debit = float(raw_debit) if raw_debit else 0.0
                    credit = float(raw_credit) if raw_credit else 0.0
                    if credit > 0:
                        amount = credit
                    elif debit > 0:
                        amount = -debit
                
                if not raw_date or (amount == 0.0 and not label):
                    continue

                # Parse date
                parsed_date = None
                date_formats = ['%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%m/%d/%Y', '%d-%b-%Y', '%d-%b-%y']
                for fmt in date_formats:
                    try:
                        parsed_date = datetime.strptime(raw_date, fmt).date()
                        break
                    except ValueError:
                        continue
                if not parsed_date:
                    parsed_date = fields.Date.context_today(self)

                # Check duplicates
                existing = self.env['account.bank.statement.line'].search([
                    ('journal_id', '=', self.journal_id.id),
                    ('date', '=', parsed_date),
                    ('payment_ref', '=', label),
                    ('amount', '=', amount),
                ], limit=1)
                
                if existing:
                    continue

                # Create statement line
                st_line = self.env['account.bank.statement.line'].create({
                    'journal_id': self.journal_id.id,
                    'date': parsed_date,
                    'payment_ref': label,
                    'ref': ref,
                    'amount': amount,
                })
                lines_created.append(st_line)
                
            except Exception:
                continue

        # Automated reconciliation
        reconciled_count = 0
        for line in lines_created:
            if line.is_reconciled:
                continue

            suspense_line = line.move_id.line_ids.filtered(
                lambda l: l.account_id == line.journal_id.suspense_account_id
            )
            if not suspense_line:
                continue

            matched = False
            
            # 2. Match by Reference (INV or BILL name in label/ref)
            search_str = f"{line.payment_ref} {line.ref or ''}"
            move_ref = re.search(r'(INV|BILL)/[0-9]{4}/[0-9]+', search_str)
            if move_ref:
                move_name = move_ref.group(0)
                invoice = self.env['account.move'].search([
                    ('name', '=', move_name),
                    ('state', '=', 'posted'),
                    ('payment_state', 'in', ['not_paid', 'partial']),
                    ('company_id', '=', self.journal_id.company_id.id)
                ], limit=1)
                
                if invoice:
                    counterpart_lines = invoice.line_ids.filtered(
                        lambda l: l.account_id.account_type in ['asset_receivable', 'liability_payable'] and not l.reconciled
                    )
                    if counterpart_lines:
                        receivable_payable_account = counterpart_lines[0].account_id
                        suspense_line.write({
                            'account_id': receivable_payable_account.id,
                            'partner_id': invoice.partner_id.id
                        })
                        (suspense_line | counterpart_lines).reconcile()
                        matched = True
                        reconciled_count += 1

            if matched:
                continue

            # 3. Match by Amount in Outstanding Accounts
            payment_account_ids = []
            if line.amount > 0:
                payment_account_ids = line.journal_id.inbound_payment_method_line_ids.payment_account_id.ids
            else:
                payment_account_ids = line.journal_id.outbound_payment_method_line_ids.payment_account_id.ids

            if payment_account_ids:
                outstanding_lines = self.env['account.move.line'].search([
                    ('account_id', 'in', payment_account_ids),
                    ('reconciled', '=', False),
                    ('credit' if line.amount > 0 else 'debit', '=', abs(line.amount)),
                    ('company_id', '=', self.journal_id.company_id.id)
                ])
                
                best_match = None
                for aml in outstanding_lines:
                    if aml.partner_id and aml.partner_id.name.lower() in line.payment_ref.lower():
                        best_match = aml
                        break
                
                if not best_match and outstanding_lines:
                    best_match = outstanding_lines[0]

                if best_match:
                    suspense_line.write({
                        'account_id': best_match.account_id.id,
                        'partner_id': best_match.partner_id.id or False
                    })
                    (suspense_line | best_match).reconcile()
                    matched = True
                    reconciled_count += 1

            if matched:
                continue

            # 4. Match by unique unpaid Invoice/Bill matching amount exactly
            if line.amount > 0:
                invoices = self.env['account.move'].search([
                    ('move_type', '=', 'out_invoice'),
                    ('state', '=', 'posted'),
                    ('payment_state', 'in', ['not_paid', 'partial']),
                    ('amount_residual', '=', line.amount),
                    ('company_id', '=', self.journal_id.company_id.id)
                ])
            else:
                invoices = self.env['account.move'].search([
                    ('move_type', '=', 'in_invoice'),
                    ('state', '=', 'posted'),
                    ('payment_state', 'in', ['not_paid', 'partial']),
                    ('amount_residual', '=', abs(line.amount)),
                    ('company_id', '=', self.journal_id.company_id.id)
                ])
                
            if len(invoices) == 1:
                invoice = invoices[0]
                counterpart_lines = invoice.line_ids.filtered(
                    lambda l: l.account_id.account_type in ['asset_receivable', 'liability_payable'] and not l.reconciled
                )
                if counterpart_lines:
                    suspense_line.write({
                        'account_id': counterpart_lines[0].account_id.id,
                        'partner_id': invoice.partner_id.id
                    })
                    (suspense_line | counterpart_lines).reconcile()
                    reconciled_count += 1

        # Return action to notify user of success
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Import Complete'),
                'message': _('Imported %s statement lines, successfully auto-reconciled %s lines.') % (len(lines_created), reconciled_count),
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            }
        }
