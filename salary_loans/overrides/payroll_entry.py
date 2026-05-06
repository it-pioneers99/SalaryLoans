import frappe
from frappe.utils import flt

from hrms.payroll.doctype.payroll_entry.payroll_entry import PayrollEntry as HRMSPayrollEntry

_PATCHED = False
_ORIGINAL_MAKE_LOAN_REPAYMENT_ENTRY = None


@frappe.whitelist()
def submit_salary_slips(payroll_entry):
	"""Whitelisted wrapper used by Payroll Entry action button."""
	doc = (
		frappe.get_doc("Payroll Entry", payroll_entry)
		if isinstance(payroll_entry, str)
		else frappe.get_doc(payroll_entry)
	)
	return doc.submit_salary_slips()


def _is_enabled() -> bool:
	return bool(frappe.conf.get("salary_loans_consolidate_loan_in_payroll_accrual_je", 1))


def _patch_loan_repayment_entry():
	global _PATCHED, _ORIGINAL_MAKE_LOAN_REPAYMENT_ENTRY
	if _PATCHED:
		return

	from hrms.payroll.doctype.salary_slip import salary_slip_loan_utils

	_ORIGINAL_MAKE_LOAN_REPAYMENT_ENTRY = salary_slip_loan_utils.make_loan_repayment_entry

	def _wrapped_make_loan_repayment_entry(doc):
		if _is_enabled() and frappe.flags.via_payroll_entry:
			return
		return _ORIGINAL_MAKE_LOAN_REPAYMENT_ENTRY(doc)

	salary_slip_loan_utils.make_loan_repayment_entry = _wrapped_make_loan_repayment_entry
	_PATCHED = True


class PayrollEntry(HRMSPayrollEntry):
	def on_submit(self):
		_patch_loan_repayment_entry()
		return super().on_submit()

	def submit_salary_slips(self):
		# Ensure patch is active before Salary Slip submit loop starts.
		_patch_loan_repayment_entry()
		return super().submit_salary_slips()

	def make_journal_entry(
		self,
		accounts,
		currencies,
		payroll_payable_account=None,
		voucher_type="Journal Entry",
		user_remark="",
		submitted_salary_slips=None,
		submit_journal_entry=False,
		employee_wise_accounting_enabled=False,
	):
		_patch_loan_repayment_entry()

		if _is_enabled() and voucher_type == "Journal Entry" and submitted_salary_slips:
			self._append_loan_entries(
				accounts,
				submitted_salary_slips,
				employee_wise_accounting_enabled=employee_wise_accounting_enabled,
			)

		return super().make_journal_entry(
			accounts,
			currencies,
			payroll_payable_account=payroll_payable_account,
			voucher_type=voucher_type,
			user_remark=user_remark,
			submitted_salary_slips=submitted_salary_slips,
			submit_journal_entry=submit_journal_entry,
			employee_wise_accounting_enabled=employee_wise_accounting_enabled,
		)

	def _append_loan_entries(
		self,
		accounts: list,
		submitted_salary_slips: list,
		employee_wise_accounting_enabled: bool = False,
	) -> None:
		total_loan_repayment = 0
		employee_totals = {}

		for slip in submitted_salary_slips:
			ss = slip if getattr(slip, "doctype", None) == "Salary Slip" else frappe.get_doc("Salary Slip", slip)
			for loan in ss.get("loans", []):
				loan_total = flt(loan.total_payment)
				if loan_total <= 0:
					continue

				total_loan_repayment += loan_total
				employee_totals[ss.employee] = employee_totals.get(ss.employee, 0) + loan_total
				common = {
					"cost_center": self.cost_center,
					"party_type": "Employee",
					"party": ss.employee,
					"reference_type": "Loan",
					"reference_name": loan.loan,
				}

				principal = flt(loan.principal_amount)
				if principal and not loan.loan_account:
					frappe.throw(
						f"Loan account is missing in Salary Slip {ss.name} for loan {loan.loan}."
					)
				if principal and loan.loan_account:
					accounts.append(
						{
							**common,
							"account": loan.loan_account,
							"credit_in_account_currency": principal,
						}
					)

				interest = flt(loan.interest_amount)
				if interest and not loan.interest_income_account:
					frappe.throw(
						f"Interest income account is missing in Salary Slip {ss.name} for loan {loan.loan}."
					)
				if interest and loan.interest_income_account:
					accounts.append(
						{
							**common,
							"account": loan.interest_income_account,
							"credit_in_account_currency": interest,
						}
					)

		if not total_loan_repayment:
			return

		if employee_wise_accounting_enabled:
			for employee, amount in employee_totals.items():
				row = next(
					(
						account_row
						for account_row in accounts
						if account_row.get("account") == self.payroll_payable_account
						and account_row.get("party_type") == "Employee"
						and account_row.get("party") == employee
						and flt(account_row.get("credit_in_account_currency")) > 0
					),
					None,
				)
				if not row:
					frappe.throw(
						f"Payroll payable row not found in accrual JE for employee {employee} to deduct loan repayment."
					)

				updated_credit = flt(row.get("credit_in_account_currency")) - amount
				if updated_credit < 0:
					frappe.throw(
						f"Loan repayment amount for employee {employee} exceeds payroll payable amount."
					)
				row["credit_in_account_currency"] = updated_credit
		else:
			row = next(
				(
					account_row
					for account_row in accounts
					if account_row.get("account") == self.payroll_payable_account
					and flt(account_row.get("credit_in_account_currency")) > 0
				),
				None,
			)
			if not row:
				frappe.throw("Payroll payable row not found in accrual JE to deduct loan repayment.")

			updated_credit = flt(row.get("credit_in_account_currency")) - total_loan_repayment
			if updated_credit < 0:
				frappe.throw("Loan repayment amount exceeds payroll payable amount in accrual JE.")
			row["credit_in_account_currency"] = updated_credit
