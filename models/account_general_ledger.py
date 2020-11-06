# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from openerp import models, fields, api, _
from openerp.tools.misc import formatLang
from datetime import datetime, timedelta
import locale


class report_account_general_ledger(models.AbstractModel):
    _name = "account.general.ledger"
    _description = "General Ledger Report"
    dateGlobal = ""

    def _format(self, value, currency=False):
        if self.env.context.get('no_format'):
            return value
        currency_id = currency or self.env.user.company_id.currency_id
        if currency_id.is_zero(value):
            # don't print -0.0 in reports
            value = abs(value)
        res = formatLang(self.env, value, currency_obj=currency_id)
        return res

    @api.model
    def get_lines(self, context_id, line_id=None):
        if type(context_id) == int:
            context_id = self.env['account.context.general.ledger'].search([['id', '=', context_id]])
        new_context = dict(self.env.context)
        new_context.update({
            'date_from': context_id.date_from,
            'date_to': context_id.date_to,
            'state': context_id.all_entries and 'all' or 'posted',
            'cash_basis': context_id.cash_basis,
            'context_id': context_id,
            'company_ids': context_id.company_ids.ids,
        })
        return self.with_context(new_context)._lines(line_id)

    def do_query_unaffected_earnings(self, line_id):
        ''' Compute the sum of ending balances for all accounts that are of a type that does not bring forward the balance in new fiscal years.
            This is needed to balance the trial balance and the general ledger reports (to have total credit = total debit)
        '''

        select = '''
        SELECT COALESCE(SUM("account_move_line".balance), 0),
               COALESCE(SUM("account_move_line".amount_currency), 0),
               COALESCE(SUM("account_move_line".debit), 0),
               COALESCE(SUM("account_move_line".credit), 0)'''
        if self.env.context.get('cash_basis'):
            select = select.replace('debit', 'debit_cash_basis').replace('credit', 'credit_cash_basis')
        select += " FROM %s WHERE %s"
        tables, where_clause, where_params = self.env['account.move.line']._query_get(domain=[('user_type_id.include_initial_balance', '=', False)])
        query = select % (tables, where_clause)
        self.env.cr.execute(query, where_params)
        res = self.env.cr.fetchone()
        return {'balance': res[0], 'amount_currency': res[1], 'debit': res[2], 'credit': res[3]}

    def do_query(self, line_id):
        select = ',COALESCE(SUM(\"account_move_line\".debit-\"account_move_line\".credit), 0),SUM(\"account_move_line\".amount_currency),SUM(\"account_move_line\".debit),SUM(\"account_move_line\".credit)'
        if self.env.context.get('cash_basis'):
            select = select.replace('debit', 'debit_cash_basis').replace('credit', 'credit_cash_basis')
        sql = "SELECT \"account_move_line\".account_id%s FROM %s WHERE %s%s GROUP BY \"account_move_line\".account_id"
        tables, where_clause, where_params = self.env['account.move.line']._query_get()
        line_clause = line_id and ' AND \"account_move_line\".account_id = ' + str(line_id) or ''
        query = sql % (select, tables, where_clause, line_clause)
        self.env.cr.execute(query, where_params)
        results = self.env.cr.fetchall()
        results = dict([(k[0], {'balance': k[1], 'amount_currency': k[2], 'debit': k[3], 'credit': k[4]}) for k in results])
        return results

    def group_by_account_id(self, line_id):
        accounts = {}
        results = self.do_query(line_id)
        initial_bal_date_to = datetime.strptime(self.env.context['date_from_aml'], "%Y-%m-%d") + timedelta(days=-1)
        initial_bal_results = self.with_context(date_to=initial_bal_date_to.strftime('%Y-%m-%d')).do_query(line_id)
        unaffected_earnings_xml_ref = self.env.ref('account.data_unaffected_earnings')
        unaffected_earnings_line = True  # used to make sure that we add the unaffected earning initial balance only once
        if unaffected_earnings_xml_ref:
            #compute the benefit/loss of last year to add in the initial balance of the current year earnings account
            last_day_previous_fy = self.env.user.company_id.compute_fiscalyear_dates(datetime.strptime(self.env.context['date_from_aml'], "%Y-%m-%d"))['date_from'] + timedelta(days=-1)
            unaffected_earnings_results = self.with_context(date_to=last_day_previous_fy.strftime('%Y-%m-%d'), date_from=False).do_query_unaffected_earnings(line_id)
            unaffected_earnings_line = False
        context = self.env.context
        base_domain = [('date', '<=', context['date_to']), ('company_id', 'in', context['company_ids'])]
        if context['date_from_aml']:
            base_domain.append(('date', '>=', context['date_from_aml']))
        if context['state'] == 'posted':
            base_domain.append(('move_id.state', '=', 'posted'))
        for account_id, result in results.items():
            domain = list(base_domain)  # copying the base domain
            domain.append(('account_id', '=', account_id))
            account = self.env['account.account'].browse(account_id)
            accounts[account] = result
            accounts[account]['initial_bal'] = initial_bal_results.get(account.id, {'balance': 0, 'amount_currency': 0, 'debit': 0, 'credit': 0})
            if account.user_type_id.id == self.env.ref('account.data_unaffected_earnings').id and not unaffected_earnings_line:
                #add the benefit/loss of previous fiscal year to the first unaffected earnings account found.
                unaffected_earnings_line = True
                for field in ['balance', 'debit', 'credit']:
                    accounts[account]['initial_bal'][field] += unaffected_earnings_results[field]
                    accounts[account][field] += unaffected_earnings_results[field]
            if not context.get('print_mode'):
                #  fetch the 81 first amls. The report only displays the first 80 amls. We will use the 81st to know if there are more than 80 in which case a link to the list view must be displayed.
                accounts[account]['lines'] = self.env['account.move.line'].search(domain, order='date', limit=81)
            else:
                accounts[account]['lines'] = self.env['account.move.line'].search(domain, order='date')
        #if the unaffected earnings account wasn't in the selection yet: add it manually
        if not unaffected_earnings_line and unaffected_earnings_results['balance']:
            #search an unaffected earnings account
            unaffected_earnings_account = self.env['account.account'].search([('user_type_id', '=', self.env.ref('account.data_unaffected_earnings').id)], limit=1)
            if unaffected_earnings_account and (not line_id or unaffected_earnings_account.id == line_id):
                accounts[unaffected_earnings_account[0]] = unaffected_earnings_results
                accounts[unaffected_earnings_account[0]]['initial_bal'] = unaffected_earnings_results
                accounts[unaffected_earnings_account[0]]['lines'] = []
        return accounts

    @api.model
    def _lines(self, line_id=None):
        lines = []
        context = self.env.context
        company_id = context.get('company_id') or self.env.user.company_id
        grouped_accounts = self.with_context(date_from_aml=context['date_from'], date_from=context['date_from'] and company_id.compute_fiscalyear_dates(datetime.strptime(context['date_from'], "%Y-%m-%d"))['date_from'] or None).group_by_account_id(line_id)  # Aml go back to the beginning of the user chosen range but the amount on the account line should go back to either the beginning of the fy or the beginning of times depending on the account
        sorted_accounts = sorted(grouped_accounts, key=lambda a: a.code)
        unfold_all = context.get('print_mode') and not context['context_id']['unfolded_accounts']
        for account in sorted_accounts:
            debit = grouped_accounts[account]['debit']
            credit = grouped_accounts[account]['credit']
            balance = grouped_accounts[account]['balance']
            amount_currency = '' if not account.currency_id else self._format(grouped_accounts[account]['amount_currency'], currency=account.currency_id)
            lines.append({
                'id': account.id,
                'type': 'line',
                'name': account.code + " " + account.name,
                'footnotes': self.env.context['context_id']._get_footnotes('line', account.id),
                'columns': ['', '', '', amount_currency, self._format(debit), self._format(credit), self._format(balance)],
                'level': 2,
                'unfoldable': True,
                'unfolded': account in context['context_id']['unfolded_accounts'] or unfold_all,
                'colspan': 4,
            })
            if account in context['context_id']['unfolded_accounts'] or unfold_all:
                initial_debit = grouped_accounts[account]['initial_bal']['debit']
                initial_credit = grouped_accounts[account]['initial_bal']['credit']
                initial_balance = grouped_accounts[account]['initial_bal']['balance']
                initial_currency = '' if not account.currency_id else self._format(grouped_accounts[account]['initial_bal']['amount_currency'], currency=account.currency_id)
                domain_lines = [{
                    'id': account.id,
                    'type': 'initial_balance',
                    'name': _('Initial Balance'),
                    'footnotes': self.env.context['context_id']._get_footnotes('initial_balance', account.id),
                    'columns': ['', '', '', initial_currency, self._format(initial_debit), self._format(initial_credit), self._format(initial_balance)],
                    'level': 1,
                }]
                progress = initial_balance
                amls = grouped_accounts[account]['lines']
                too_many = False
                if len(amls) > 80 and not context.get('print_mode'):
                    amls = amls[-80:]
                    too_many = True
                for line in amls:
                    if self.env.context['cash_basis']:
                        line_debit = line.debit_cash_basis
                        line_credit = line.credit_cash_basis
                    else:
                        line_debit = line.debit
                        line_credit = line.credit
                    progress = progress + line_debit - line_credit
                    currency = "" if not line.currency_id else self._format(line.amount_currency, currency=line.currency_id)
                    name = []
                    name = line.name and line.name or ''
                    if line.ref:
                        #name = name and name + ' - ' + line.ref or line.ref
                        name = name or line.ref
                    #if len(name) > 35:
                        #name = name[:32] + "..."
                    partner_name = line.partner_id.name
                    if partner_name and len(partner_name) > 35:
                        partner_name = partner_name[:32] + "..."
                    domain_lines.append({
                        'id': line.id,
                        'type': 'move_line_id',
                        'move_id': line.move_id.id,
                        'action': line.get_model_id_and_name(),
                        'name': line.move_id.name if line.move_id.name else '/',
                        'footnotes': self.env.context['context_id']._get_footnotes('move_line_id', line.id),
                        'columns': [line.date, name, partner_name, currency,
                                    line_debit != 0 and self._format(line_debit) or '',
                                    line_credit != 0 and self._format(line_credit) or '',
                                    self._format(progress)],
                        'level': 1,
                    })
                domain_lines.append({
                    'id': account.id,
                    'type': 'o_account_reports_domain_total',
                    'name': _('Total '),
                    'footnotes': self.env.context['context_id']._get_footnotes('o_account_reports_domain_total', account.id),
                    'columns': ['', '', '', amount_currency, self._format(debit), self._format(credit), self._format(balance)],
                    'level': 1,
                })
                if too_many:
                    domain_lines.append({
                        'id': account.id,
                        'type': 'too_many',
                        'name': _('There are more than 80 items in this list, click here to see all of them'),
                        'footnotes': [],
                        'colspan': 8,
                        'columns': [],
                        'level': 3,
                    })
                lines += domain_lines
        return lines

    @api.model
    def get_title(self):
        return _("General Ledger")
        #return _("Balance 6 colonnes")

    @api.model
    def get_name(self):
        return 'general_ledger'

    @api.model
    def get_report_type(self):
        return 'no_comparison'

    def get_template(self):
        return 'account_reports.report_financial'


## BALANCE SIX COLONNES


class report_account_balance_six(models.AbstractModel):
    _name = "account.balance.six"
    _description = "Balance six colonnes Report"

    def _format(self, value, currency=False):
        if self.env.context.get('no_format'):
            return value
        currency_id = currency or self.env.user.company_id.currency_id
        if currency_id.is_zero(value):
            # don't print -0.0 in reports
            value = abs(value)
        res = formatLang(self.env, value, currency_obj=currency_id)
        return res

    @api.model
    def get_lines(self, context_id, line_id=None):
        global dateGlobal
        dateGlobal = context_id.date_to
        global dateGlobalFrom
        dateGlobalFrom = context_id.date_from
        if type(context_id) == int:
            context_id = self.env['account.context.balance.six'].search([['id', '=', context_id]])
        new_context = dict(self.env.context)
        new_context.update({
            'date_from': context_id.date_from,
            'date_to': context_id.date_to,
            'state': context_id.all_entries and 'all' or 'posted',
            'cash_basis': context_id.cash_basis,
            'context_id': context_id,
            'company_ids': context_id.company_ids.ids,
        })
        return self.with_context(new_context)._lines(line_id)

    def do_query_unaffected_earnings(self, line_id):
        ''' Compute the sum of ending balances for all accounts that are of a type that does not bring forward the balance in new fiscal years.
            This is needed to balance the trial balance and the general ledger reports (to have total credit = total debit)
        '''

        select = '''
        SELECT COALESCE(SUM("account_move_line".balance), 0),
               COALESCE(SUM("account_move_line".amount_currency), 0),
               COALESCE(SUM("account_move_line".debit), 0),
               COALESCE(SUM("account_move_line".credit), 0)'''
        if self.env.context.get('cash_basis'):
            select = select.replace('debit', 'debit_cash_basis').replace('credit', 'credit_cash_basis')
        select += " FROM %s WHERE %s"
        tables, where_clause, where_params = self.env['account.move.line']._query_get(domain=[('user_type_id.include_initial_balance', '=', False)])
        query = select % (tables, where_clause)
        self.env.cr.execute(query, where_params)
        res = self.env.cr.fetchone()
        return {'balance': res[0], 'amount_currency': res[1], 'debit': res[2], 'credit': res[3]}

    def do_query(self, line_id):
        select = ',COALESCE(SUM(\"account_move_line\".debit-\"account_move_line\".credit), 0),SUM(\"account_move_line\".amount_currency),SUM(\"account_move_line\".debit),SUM(\"account_move_line\".credit)'
        if self.env.context.get('cash_basis'):
            select = select.replace('debit', 'debit_cash_basis').replace('credit', 'credit_cash_basis')
        sql = "SELECT \"account_move_line\".account_id%s FROM %s WHERE %s%s GROUP BY \"account_move_line\".account_id"
        tables, where_clause, where_params = self.env['account.move.line']._query_get()
        line_clause = line_id and ' AND \"account_move_line\".account_id = ' + str(line_id) or ''
        query = sql % (select, tables, where_clause, line_clause)
        self.env.cr.execute(query, where_params)
        results = self.env.cr.fetchall()
        results = dict([(k[0], {'balance': k[1], 'amount_currency': k[2], 'debit': k[3], 'credit': k[4]}) for k in results])
        return results

    def group_by_account_id(self, line_id):
        accounts = {}
        results = self.do_query(line_id)
        initial_bal_date_to = datetime.strptime(self.env.context['date_from_aml'], "%Y-%m-%d") + timedelta(days=-1)
        initial_bal_results = self.with_context(date_to=initial_bal_date_to.strftime('%Y-%m-%d')).do_query(line_id)
        unaffected_earnings_xml_ref = self.env.ref('account.data_unaffected_earnings')
        unaffected_earnings_line = True  # used to make sure that we add the unaffected earning initial balance only once
        if unaffected_earnings_xml_ref:
            #compute the benefit/loss of last year to add in the initial balance of the current year earnings account
            last_day_previous_fy = self.env.user.company_id.compute_fiscalyear_dates(datetime.strptime(self.env.context['date_from_aml'], "%Y-%m-%d"))['date_from'] + timedelta(days=-1)
            unaffected_earnings_results = self.with_context(date_to=last_day_previous_fy.strftime('%Y-%m-%d'), date_from=False).do_query_unaffected_earnings(line_id)
            unaffected_earnings_line = False
        context = self.env.context
        base_domain = [('date', '<=', context['date_to']), ('company_id', 'in', context['company_ids'])]
        if context['date_from_aml']:
            base_domain.append(('date', '>=', context['date_from_aml']))
        if context['state'] == 'posted':
            base_domain.append(('move_id.state', '=', 'posted'))
        for account_id, result in results.items():
            domain = list(base_domain)  # copying the base domain
            domain.append(('account_id', '=', account_id))
            account = self.env['account.account'].browse(account_id)
            accounts[account] = result
            accounts[account]['initial_bal'] = initial_bal_results.get(account.id, {'balance': 0, 'amount_currency': 0, 'debit': 0, 'credit': 0})
            if account.user_type_id.id == self.env.ref('account.data_unaffected_earnings').id and not unaffected_earnings_line:
                #add the benefit/loss of previous fiscal year to the first unaffected earnings account found.
                unaffected_earnings_line = True
                for field in ['balance', 'debit', 'credit']:
                    accounts[account]['initial_bal'][field] += unaffected_earnings_results[field]
                    accounts[account][field] += unaffected_earnings_results[field]
            if not context.get('print_mode'):
                #  fetch the 81 first amls. The report only displays the first 80 amls. We will use the 81st to know if there are more than 80 in which case a link to the list view must be displayed.
                accounts[account]['lines'] = self.env['account.move.line'].search(domain, order='date', limit=81)
            else:
                accounts[account]['lines'] = self.env['account.move.line'].search(domain, order='date')
        #if the unaffected earnings account wasn't in the selection yet: add it manually
        if not unaffected_earnings_line and unaffected_earnings_results['balance']:
            #search an unaffected earnings account
            unaffected_earnings_account = self.env['account.account'].search([('user_type_id', '=', self.env.ref('account.data_unaffected_earnings').id)], limit=1)
            if unaffected_earnings_account and (not line_id or unaffected_earnings_account.id == line_id):
                accounts[unaffected_earnings_account[0]] = unaffected_earnings_results
                accounts[unaffected_earnings_account[0]]['initial_bal'] = unaffected_earnings_results
                accounts[unaffected_earnings_account[0]]['lines'] = []
        return accounts

    @api.model
    def _lines(self, line_id=None):
        lines = []
        context = self.env.context
        company_id = context.get('company_id') or self.env.user.company_id
        grouped_accounts = self.with_context(date_from_aml=context['date_from'], date_from=context['date_from'] and company_id.compute_fiscalyear_dates(datetime.strptime(context['date_from'], "%Y-%m-%d"))['date_from'] or None).group_by_account_id(line_id)  # Aml go back to the beginning of the user chosen range but the amount on the account line should go back to either the beginning of the fy or the beginning of times depending on the account
        currentYear = datetime.now().year
        currentDate = datetime.now().strftime("%Y-%m-%d")
        previousYear = currentYear - 1
        premierJanv = str(currentYear)+'-01-01'
        previousYear_str_one = str(previousYear)+'-1-1'
        previousYear_str_two = str(previousYear)+'-12-31'
        previousYear_obj = datetime.strptime(previousYear_str_one, '%Y-%m-%d')
        grouped_accounts_prev = self.with_context(date_from_aml=previousYear_str_two, date_from=previousYear_str_one).group_by_account_id(line_id)
        sorted_accounts = sorted(grouped_accounts, key=lambda a: a.code)
        unfold_all = context.get('print_mode') and not context['context_id']['unfolded_accounts']
        global sum_debit_prev, sum_credit_prev, sum_debit, sum_credit, sum_balance_debit, sum_balance_credit
        global sum_debit_prev_bilan, sum_credit_prev_bilan, sum_debit_bilan, sum_credit_bilan, sum_balance_debit_bilan, sum_balance_credit_bilan
        global sum_debit_prev_gestion, sum_credit_prev_gestion, sum_debit_gestion, sum_credit_gestion, sum_balance_debit_gestion, sum_balance_credit_gestion

        sum_debit_prev = 0
        sum_credit_prev = 0
        sum_debit = 0
        sum_credit = 0
        sum_balance_debit = 0
        sum_balance_credit = 0

        sum_debit_prev_bilan = 0
        sum_credit_prev_bilan = 0
        sum_debit_bilan = 0
        sum_credit_bilan = 0
        sum_balance_debit_bilan = 0
        sum_balance_credit_bilan = 0

        sum_debit_prev_gestion = 0
        sum_credit_prev_gestion = 0
        sum_debit_gestion = 0
        sum_credit_gestion = 0
        sum_balance_debit_gestion = 0
        sum_balance_credit_gestion = 0

        for account in sorted_accounts:
            #debit = grouped_accounts[account]['debit']
            #credit = grouped_accounts[account]['credit']
            if dateGlobal == "":
                res = self.get_mouvement_by_date(account.code, premierJanv, str(currentYear)+'-12-31')
                res2 = self.get_situation_by_date(account.code, self.get_prev_date())
            else:
                res = self.get_mouvement_by_date(account.code, dateGlobalFrom, dateGlobal)
                res2 = self.get_situation_by_date(account.code, self.get_prev_date())
            debit = res[0]
            credit = res[1]
            balance = res[2]
            #debit_prev = grouped_accounts_prev[account]['debit']
            #credit_prev = grouped_accounts_prev[account]['credit']
            debit_prev = res2[0]
            credit_prev = res2[1]
            #balance = grouped_accounts[account]['balance']
            nature = self.getCompteNature(account.code)
            baseCalcul = debit_prev + debit - credit - credit_prev
            ifCharge = self.ifCompteCharge(account.code)
            if ifCharge == 'charge':
                baseCalcul = debit - credit
                debit_prev_charge = debit_prev
                credit_prev_charge = credit_prev
                debit_prev = 0
                credit_prev = 0

            if nature == 'credit':
                scredit = (-1)*baseCalcul
                sdebit = 0
            else:
                sdebit = baseCalcul
                scredit = 0

            if scredit < 0:
                sdebit = (-1)*scredit
                scredit = 0

            if sdebit < 0:
                scredit = (-1)*sdebit
                sdebit = 0

            balance_prev = debit_prev - credit_prev
            if balance_prev < 0:
                credit_prev = (-1)*balance_prev
                debit_prev = 0
            else:
                debit_prev = balance_prev
                credit_prev = 0
            if balance_prev == 0:
                credit_prev = 0
                debit_prev = 0

            #if balance < 0:
                #scredit = (-1)*balance
                #sdebit = ''
            #else:
                #sdebit = balance
                #scredit = ''
            #if balance == 0:
                #scredit = ''
                #sdebit = ''

            #sdebit = debit_prev + debit - credit
            #scredit = credit_prev - credit + debit
            if int(debit_prev) == 0:
                debit_prev = 0
                # debit_prev = ''
            if int(credit_prev) == 0:
                credit_prev = 0
                # credit_prev = ''
            if int(debit) == 0:
                debit = 0
                # debit = ''
            if int(credit) == 0:
                credit = 0
                # credit = ''
            if int(sdebit) == 0:
                sdebit = 0
                # sdebit = ''
            if int(scredit) == 0:
                scredit = 0
                # scredit = ''

            if ifCharge == 'charge':
                #sum_debit_prev += debit_prev_charge
                #sum_credit_prev += credit_prev_charge
                sum_debit_prev += 0
                sum_credit_prev += 0
            else:
                sum_debit_prev += debit_prev
                sum_credit_prev += credit_prev

            sum_debit += debit
            sum_credit += credit
            sum_balance_debit += sdebit
            sum_balance_credit += scredit

            if ifCharge == 'charge':
                #sum_debit_prev_gestion += abs(debit_prev_charge)
                #sum_credit_prev_gestion += abs(credit_prev_charge)
                sum_debit_prev_gestion += 0
                sum_credit_prev_gestion += 0
                sum_debit_gestion += abs(debit)
                sum_credit_gestion += abs(credit)
                sum_balance_debit_gestion += abs(sdebit)
                sum_balance_credit_gestion += abs(scredit)
            else:
                sum_debit_prev_bilan += abs(debit_prev)
                sum_credit_prev_bilan += abs(credit_prev)
                sum_debit_bilan += abs(debit)
                sum_credit_bilan += abs(credit)
                sum_balance_debit_bilan += abs(sdebit)
                sum_balance_credit_bilan += abs(scredit)

            amount_currency = '' if not account.currency_id else self._format(grouped_accounts[account]['amount_currency'], currency=account.currency_id)
            if sdebit == 0 and scredit == 0:
                #balances = 0
                lines.append({
                    'id': account.id,
                    'type': 'line',
                    'name': account.code + " " + account.name,
                    'footnotes': self.env.context['context_id']._get_footnotes('line', account.id),
                    #'columns': [amount_currency, self.format_num(debit_prev), self.format_num(credit_prev), self.format_num(debit), self.format_num(credit), self.format_num(sdebit), self.format_num(scredit)],
                    'columns': [amount_currency, self.format_num(debit_prev), self.format_num(credit_prev), self.format_num(debit), self.format_num(credit), self.format_num(sdebit), self.format_num(scredit)],
                    'level': 2,
                    'unfoldable': True,
                    'unfolded': account in context['context_id']['unfolded_accounts'] or unfold_all,
                    'colspan': 0,
                })
            else:
                lines.append({
                    'id': account.id,
                    'type': 'line',
                    'name': account.code + " " + account.name,
                    'footnotes': self.env.context['context_id']._get_footnotes('line', account.id),
                    #'columns': [amount_currency, self.format_num(debit_prev), self.format_num(credit_prev), self.format_num(debit), self.format_num(credit), self.format_num(sdebit), self.format_num(scredit)],
                    'columns': [amount_currency, self.format_num(debit_prev), self.format_num(credit_prev), self.format_num(debit), self.format_num(credit), self.format_num(sdebit), self.format_num(scredit)],
                    'level': 2,
                    'unfoldable': True,
                    'unfolded': account in context['context_id']['unfolded_accounts'] or unfold_all,
                    'colspan': 0,
                })
            if account in context['context_id']['unfolded_accounts'] or unfold_all:
                initial_debit = grouped_accounts[account]['initial_bal']['debit']
                initial_credit = grouped_accounts[account]['initial_bal']['credit']
                initial_balance = grouped_accounts[account]['initial_bal']['balance']
                initial_currency = '' if not account.currency_id else self._format(grouped_accounts[account]['initial_bal']['amount_currency'], currency=account.currency_id)
                domain_lines = [{
                    'id': account.id,
                    'type': 'initial_balance',
                    'name': _('Initial Balance'),
                    'footnotes': self.env.context['context_id']._get_footnotes('initial_balance', account.id),
                    'columns': ['', '', '', initial_currency, self._format(initial_debit), self._format(initial_credit), self._format(initial_balance)],
                    'level': 1,
                }]
                progress = initial_balance
                amls = grouped_accounts[account]['lines']
                too_many = False
                if len(amls) > 80 and not context.get('print_mode'):
                    amls = amls[-80:]
                    too_many = True
                for line in amls:
                    if self.env.context['cash_basis']:
                        line_debit = line.debit_cash_basis
                        line_credit = line.credit_cash_basis
                    else:
                        line_debit = line.debit
                        line_credit = line.credit
                    progress = progress + line_debit - line_credit
                    currency = "" if not line.currency_id else self._format(line.amount_currency, currency=line.currency_id)
                    name = []
                    name = line.name and line.name or ''
                    if line.ref:
                        name = name and name + ' - ' + line.ref or line.ref
                    if len(name) > 35:
                        name = name[:32] + "..."
                    partner_name = line.partner_id.name
                    if partner_name and len(partner_name) > 35:
                        partner_name = partner_name[:32] + "..."
                    domain_lines.append({
                        'id': line.id,
                        'type': 'move_line_id',
                        'move_id': line.move_id.id,
                        'action': line.get_model_id_and_name(),
                        'name': line.move_id.name if line.move_id.name else '/',
                        'footnotes': self.env.context['context_id']._get_footnotes('move_line_id', line.id),
                        'columns': [line.date, name, partner_name, currency,
                                    line_debit != 0 and self._format(line_debit) or '',
                                    line_credit != 0 and self._format(line_credit) or '',
                                    self._format(progress)],
                        'level': 1,
                    })
                domain_lines.append({
                    'id': account.id,
                    'type': 'o_account_reports_domain_total',
                    'name': _('Total '),
                    'footnotes': self.env.context['context_id']._get_footnotes('o_account_reports_domain_total', account.id),
                    'columns': ['', '', '', amount_currency, self._format(debit), self._format(credit), self._format(balance)],
                    'level': 1,
                })
                if too_many:
                    domain_lines.append({
                        'id': account.id,
                        'type': 'too_many',
                        'name': _('There are more than 80 items in this list, click here to see all of them'),
                        'footnotes': [],
                        'colspan': 8,
                        'columns': [],
                        'level': 3,
                    })
                lines += domain_lines
        return lines

    @api.model
    def get_title(self):
        #return _("General Ledger")
        return _("Balance générale des comptes")

    @api.model
    def get_date_now(self):
        currentDate = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return currentDate

    def getCompteNature(self, code):
        classe = code[:2]
        creditArray = ['10', '11', '12', '13', '14', '15', '16', '17', '18', '19', '28', '29', '38', '39', '40', '42', '43', '44', '47', '48', '49', '59', '70', '71','72', '73', '74', '75', '76', '77', '78', '79', '80', '82', '84', '86', '88']
        #debitArray = [20,21,22,23,24,25,26,27,30,31,32,33,34,35,36,37,41,45,46,50,51,52,53,54,55,56,57,58,60,61,62,63,64,65,66,67,68,69,81,83,85,87,89]
        if classe in creditArray:
            return 'credit'
        else:
            return 'debit'

    def ifCompteCharge(self, code):
        classe = code[:2]
        chargeArray = ['60', '61', '62', '63', '64', '65', '66', '67', '68', '69', '70', '71', '72', '73', '74', '75', '76', '77', '78', '79', '80', '81', '82','83', '84', '85', '86', '87', '88', '89']
        if classe in chargeArray:
            return 'charge'
        else:
            return 'noncharge'

    def get_mouvement_by_date(self, code, date_debut, date_fin):
        sql = "SELECT COALESCE(SUM(\"account_move_line\".debit), 0), COALESCE(SUM(\"account_move_line\".credit), 0), COALESCE(SUM(\"account_move_line\".balance), 0) FROM \"account_move_line\", \"account_account\", \"account_move\"  WHERE \"account_move\".id = \"account_move_line\".move_id AND \"account_move\".state = 'posted' AND \"account_move_line\".account_id = \"account_account\".id AND \"account_move_line\".date <= %s AND \"account_move_line\".date >= %s  AND \"account_account\".code = %s"
        params = date_fin, date_debut, code
        self.env.cr.execute(sql, params)
        res = self.env.cr.fetchone()
        #return {'debit': res[0], 'credit': res[1], 'balance': res[2]}
        return res

    def get_situation_by_date(self, code, date):
        sql = "SELECT COALESCE(SUM(\"account_move_line\".debit), 0), COALESCE(SUM(\"account_move_line\".credit), 0), COALESCE(SUM(\"account_move_line\".balance), 0) FROM \"account_move_line\", \"account_account\", \"account_move\"  WHERE \"account_move\".id = \"account_move_line\".move_id AND \"account_move\".state = 'posted' AND \"account_move_line\".account_id = \"account_account\".id AND \"account_move_line\".date <= %s AND \"account_account\".code = %s"
        params = date, code
        self.env.cr.execute(sql, params)
        res = self.env.cr.fetchone()
        #return {'debit': res[0], 'credit': res[1], 'balance': res[2]}
        return res

    def format_num(self, num):
        inum = int(round(num, 0))
        #return str(inum)
        try:
            num_format = "{:,}".format(inum)
            return num_format.replace(",", " ")
        except (ValueError, TypeError):
            return str(inum)

    @api.model
    def get_prev_date(self):
        currentYear = datetime.now().year
        previousYear = currentYear - 1
        previousYear_str = str(previousYear)+'-12-31'
        if dateGlobal == "":
            return previousYear_str
        else:
            dateGlobal_obj = datetime.strptime(dateGlobal, '%Y-%m-%d')
            dateGlobal_obj_year = dateGlobal_obj.year
            return str(dateGlobal_obj_year-1)+'-12-31'
        #return previousYear_str

    def getBalanceSum(self):
        return [self.format_num(sum_debit_prev), self.format_num(sum_credit_prev), self.format_num(sum_debit), self.format_num(sum_credit), '', '']
        #return [self.format_num(sum_debit_prev), self.format_num(sum_credit_prev), self.format_num(sum_debit), self.format_num(sum_credit), self.format_num(sum_balance_debit), self.format_num(sum_balance_credit)]

    def getBalanceBilanSum(self):
        solde_gestion = sum_credit_gestion - sum_debit_gestion
        solde_bilan = sum_credit_bilan - sum_debit_bilan

        if solde_bilan < 0:
            return [self.format_num(sum_debit_prev_bilan), self.format_num(sum_credit_prev_bilan), self.format_num(sum_debit_bilan), self.format_num(sum_credit_bilan), self.format_num(-solde_bilan), '']
        else:
            return [self.format_num(sum_debit_prev_bilan), self.format_num(sum_credit_prev_bilan), self.format_num(sum_debit_bilan), self.format_num(sum_credit_bilan), '', self.format_num(solde_bilan)]
        #return [self.format_num(sum_debit_prev_bilan), self.format_num(sum_credit_prev_bilan), self.format_num(sum_debit_bilan), self.format_num(sum_credit_bilan), self.format_num(sum_balance_debit_bilan), self.format_num(sum_balance_credit_bilan)]

    def getBalanceGestionSum(self):
        solde_gestion = sum_credit_gestion - sum_debit_gestion
        if solde_gestion > 0:
            return [self.format_num(sum_debit_prev_gestion), self.format_num(sum_credit_prev_gestion), self.format_num(sum_debit_gestion), self.format_num(sum_credit_gestion), '', self.format_num(solde_gestion)]
        else:
            return [self.format_num(sum_debit_prev_gestion), self.format_num(sum_credit_prev_gestion), self.format_num(sum_debit_gestion), self.format_num(sum_credit_gestion), self.format_num(-solde_gestion), '']
        #return [self.format_num(sum_debit_prev_gestion), self.format_num(sum_credit_prev_gestion), self.format_num(sum_debit_gestion), self.format_num(sum_credit_gestion), self.format_num(sum_balance_debit_gestion), self.format_num(sum_balance_credit_gestion)]
        #return [self.format_num(sum_debit_prev_bilan), self.format_num(sum_credit_prev_bilan), self.format_num(sum_debit_gestion), self.format_num(sum_credit_gestion), self.format_num(sum_balance_debit_gestion), self.format_num(sum_balance_credit_gestion)]

    @api.model
    def get_name(self):
        return 'general_ledger'

    @api.model
    def get_report_type(self):
        return 'no_comparison'

    def get_template(self):
        return 'account_reports.report_balance_six'


class account_context_general_ledger(models.TransientModel):
    _name = "account.context.general.ledger"
    _description = "A particular context for the general ledger"
    _inherit = "account.report.context.common"

    fold_field = 'unfolded_accounts'
    unfolded_accounts = fields.Many2many('account.account', 'context_to_account', string='Unfolded lines')

    def get_report_obj(self):
        return self.env['account.general.ledger']

    def get_columns_names(self):
        return [_("Date"), _("Communication"), _("Partner"), _("Currency"), _("Debit"), _("Credit"), _("Balance")]
        #return [_("Numéro & Intitulé des comptes"), _(" "), _("Debit (Période prec.)"), _("Credit (Période prec.)"), _("Debit"), _("Credit"), _("Solde cumulé")]

    @api.multi
    def get_columns_types(self):
        return ["date", "text", "text", "number", "number", "number", "number"]


class account_context_balance_six(models.TransientModel):
    _name = "account.context.balance.six"
    _description = "A particular context for the general ledger"
    _inherit = "account.report.context.common"

    fold_field = 'unfolded_accounts'
    unfolded_accounts = fields.Many2many('account.account', 'context_to_balance_six', string='Unfolded lines')

    def get_report_obj(self):
        return self.env['account.balance.six']

    def get_columns_names(self):
        #return [_("Date"), _("Communication"), _("Partner"), _("Currency"), _("Debit"), _("Credit"), _("Balance")]
        return [_(""), _("Debit"), _("Credit"), _("Debit"), _("Credit"), _("Debit"), _("Credit")]

    @api.multi
    def get_columns_types(self):
        #return ["date", "text", "text", "number", "number", "number", "number"]
        return ["text", "number", "number", "number", "number", "number", "number"]
