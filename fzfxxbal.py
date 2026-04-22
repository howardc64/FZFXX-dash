#!/usr/bin/env python3
"""
fzfxxbal.py - Add FZFXX running balance column to Fidelity activity CSV

Synopsis:
    python fzfxxbal.py <csv> <endbal>

    csv     CSV file from Fidelity containing activity log
    endbal  Current FZFXX balance as of today (script run date)

Logic:
    - Rows are newest-first in the CSV.
    - endbal is the known FZFXX balance as of today (script run date).
    - Work backwards from newest row to oldest, undoing each FZFXX delta,
      to derive the balance at every point in history.
    - FZFXX balance is written on EVERY data row (not just FZFXX rows);
      non-FZFXX rows carry the same balance as the row above them (balance
      is unchanged by non-FZFXX activity).
    - Negative balance triggers a warning with the offending row details.
    - Action keywords (Symbol must be FZFXX):
        Deposits  (balance increases):
            BOUGHT / YOU BOUGHT       -> Amount is negative; use abs(Amount)
            DIVIDEND RECEIVED         -> dividend reinvested; use abs(Amount)
            DIVIDEND EARNED           -> dividend reinvested; use abs(Amount)
            TRANSFERRED TO FZFXX      -> inbound transfer; use abs(Amount)
        Withdrawals (balance decreases):
            REDEMPTION                -> Amount is positive; use -abs(Amount)
            YOU SOLD                  -> Amount is positive; use -abs(Amount)
            TRANSFERRED FROM FZFXX    -> outbound transfer; use -abs(Amount)
    - Header rows, footer rows, and blank rows pass through unchanged
      (empty string in the new balance column).
"""

import sys
import csv
import re
import os
from datetime import date


def is_fzfxx_row(row, symbol_col):
    if symbol_col is None or symbol_col >= len(row):
        return False
    return row[symbol_col].strip().upper() == 'FZFXX'


def fzfxx_delta(action, amount_str):
    """
    Return the signed change to FZFXX balance for a recognised FZFXX action.
    Positive = balance increases (deposit/dividend/inbound transfer).
    Negative = balance decreases (redemption/withdrawal/outbound transfer).
    Returns None if action is not a recognised FZFXX transaction type.
    """
    action_upper = action.upper()
    try:
        amount = float(amount_str.replace(',', '').strip())
    except (ValueError, AttributeError):
        amount = 0.0

    if 'BOUGHT' in action_upper:
        # Amount is negative (cash paid out); FZFXX balance increases
        return abs(amount)
    elif 'REDEMPTION' in action_upper or 'YOU SOLD' in action_upper:
        # Amount is positive (cash received); FZFXX balance decreases
        return -abs(amount)
    elif 'DIVIDEND RECEIVED' in action_upper or 'DIVIDEND EARNED' in action_upper:
        # Dividend reinvested into FZFXX; balance increases
        return abs(amount)
    elif 'TRANSFERRED FROM FZFXX' in action_upper:
        # Outbound transfer out of FZFXX; balance decreases
        return -abs(amount)
    elif 'TRANSFERRED TO FZFXX' in action_upper:
        # Inbound transfer into FZFXX; balance increases
        return abs(amount)
    return None


def find_header_row(rows):
    for i, row in enumerate(rows):
        for cell in row:
            if 'Run Date' in cell:
                return i
    return None


def find_footer_start(rows, header_idx):
    # Accept both MM/DD/YYYY and YYYY-MM-DD date formats
    date_pattern = re.compile(r'(\d{2}/\d{2}/\d{4}|\d{4}-\d{2}-\d{2})')
    for i in range(header_idx + 1, len(rows)):
        row = rows[i]
        non_empty = [c.strip() for c in row if c.strip()]
        if not non_empty:
            return i
        if not date_pattern.match(non_empty[0]):
            return i
    return len(rows)


def main():
    if len(sys.argv) != 3:
        print("Usage: python fzfxxbal.py <csv> <endbal>")
        sys.exit(1)

    csv_path = sys.argv[1]
    try:
        end_bal = float(sys.argv[2].replace(',', '').strip())
    except ValueError:
        print(f"Error: endbal '{sys.argv[2]}' is not a valid number.")
        sys.exit(1)

    if not os.path.isfile(csv_path):
        print(f"Error: File not found: {csv_path}")
        sys.exit(1)

    run_date = date.today().strftime('%m/%d/%Y')
    print(f"Script run date                    : {run_date}")
    print(f"Ending FZFXX balance (as of today) : ${end_bal:,.2f}")

    with open(csv_path, newline='', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        all_rows = list(reader)

    header_idx = find_header_row(all_rows)
    if header_idx is None:
        print("Error: Could not find header row with 'Run Date'.")
        sys.exit(1)

    header       = all_rows[header_idx]
    footer_start = find_footer_start(all_rows, header_idx)
    data_rows    = all_rows[header_idx + 1 : footer_start]   # newest-first

    # Strip any pre-existing FZFXX Balance column so re-runs don't duplicate it
    fzfxx_bal_col = None
    for i, h in enumerate(header):
        if 'fzfxx balance' in h.lower():
            fzfxx_bal_col = i
            break
    if fzfxx_bal_col is not None:
        header    = [c for j, c in enumerate(header)    if j != fzfxx_bal_col]
        data_rows = [[c for j, c in enumerate(row) if j != fzfxx_bal_col] for row in data_rows]
        all_rows  = (
            [[c for j, c in enumerate(row) if j != fzfxx_bal_col] for row in all_rows[:header_idx]]
            + [header]
            + data_rows
            + [[c for j, c in enumerate(row) if j != fzfxx_bal_col] for row in all_rows[footer_start:]]
        )
        # Recompute indices after stripping
        header_idx   = find_header_row(all_rows)
        header       = all_rows[header_idx]
        footer_start = find_footer_start(all_rows, header_idx)
        data_rows    = all_rows[header_idx + 1 : footer_start]
        print("  NOTE: pre-existing 'FZFXX Balance' column removed and recalculated.")

    def col(name):
        for i, h in enumerate(header):
            if name.lower() in h.lower():
                return i
        return None

    action_col = col('Action')
    symbol_col = col('Symbol')
    amount_col = col('Amount')
    date_col   = col('Run Date')

    if action_col is None or amount_col is None:
        print("Error: Could not locate required columns (Action, Amount) in header.")
        sys.exit(1)

    # --- Pass 1: collect deltas ---
    deltas = []
    for row in data_rows:
        if is_fzfxx_row(row, symbol_col):
            action     = row[action_col] if action_col < len(row) else ''
            amount_str = row[amount_col] if amount_col < len(row) else ''
            delta = fzfxx_delta(action, amount_str)
            if delta is None:
                # Unrecognised FZFXX action — warn but don't crash
                dt = row[date_col].strip() if date_col is not None and date_col < len(row) else '?'
                print(f"  NOTICE unrecognised FZFXX action at {dt}: {action[:80]}")
        else:
            delta = None
        deltas.append(delta)

    # --- Pass 2: walk newest→oldest, reconstruct balance at every row ---
    n            = len(data_rows)
    post_balance = [None] * n

    running = end_bal
    for i in range(n):          # i=0 is newest
        if deltas[i] is not None:
            post_balance[i] = running
            running -= deltas[i]    # undo delta going backwards in time
        else:
            post_balance[i] = running   # no FZFXX activity; balance unchanged

    # --- Negative balance check ---
    neg_count = 0
    for i, bal in enumerate(post_balance):
        if bal is not None and bal < -0.005:
            neg_count += 1
            row      = data_rows[i]
            row_date = row[date_col].strip() if date_col is not None and date_col < len(row) else '?'
            action   = row[action_col].strip() if action_col < len(row) else '?'
            amount   = row[amount_col].strip() if amount_col < len(row) else '?'
            symbol   = row[symbol_col].strip() if symbol_col is not None and symbol_col < len(row) else '?'
            print(f"  WARNING negative balance ${bal:,.2f} at data row {i} "
                  f"| Date={row_date} | Symbol={symbol} | Action={action[:60]} | Amount={amount}")

    if neg_count:
        print(f"\n  {neg_count} negative balance(s) detected.")
        print("  Possible causes: wrong endbal, missing transactions, or unrecognised action keyword.")
        print("  Tip: re-run with a higher endbal to confirm the direction of historical transactions.\n")
    else:
        print("No negative balances detected.")

    # --- Build output ---
    FZFXX_BAL_HEADER = 'FZFXX Balance ($)'
    output_rows = []

    for row in all_rows[:header_idx]:
        output_rows.append(row + [''])

    output_rows.append(header + [FZFXX_BAL_HEADER])

    for i, row in enumerate(data_rows):
        bal_str = f"{post_balance[i]:,.2f}" if post_balance[i] is not None else ''
        output_rows.append(row + [bal_str])

    for row in all_rows[footer_start:]:
        output_rows.append(row + [''])

    base, ext = os.path.splitext(csv_path)
    out_path  = base + '_fzfxx' + ext
    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerows(output_rows)

    # Implied starting balance (oldest row)
    implied_start = post_balance[-1] if post_balance else None

    fzfxx_count = sum(1 for d in deltas if d is not None)
    print(f"FZFXX transactions found : {fzfxx_count}")
    print(f"Total data rows          : {n}")
    if implied_start is not None:
        print(f"Implied FZFXX balance at start of history: ${implied_start:,.2f}")
    print(f"Output written to        : {out_path}")


if __name__ == '__main__':
    main()
