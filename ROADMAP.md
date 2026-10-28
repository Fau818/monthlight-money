# Roadmap

Where the app is going, in the order it's planned. Versions follow SemVer, and new features land as small releases
within one version line rather than as big jumps. There are no dates: each step ships when it's ready.

## v0.1.0: the first public release

Everything the README describes: bank sync through Plaid, a Copilot Money import, splits, paybacks, links between
transactions, tags, budgets, recurring charges and net worth.

## v0.2.0: the Transactions workspace

The features added so far had made the transactions screens crowded. This release starts reorganizing them into one
workspace, in the style of Copilot Money's Mac app.

- **Look:** a theme like Copilot Money's, dark and light, on every page, and an icon for the browser tab and a
  phone's home screen.
- **Sidebar:** search, the pages, and the accounts by group with their balances.
- **List:**
  - One line per transaction: name, account, the category, the amount, and small icons for splits, links, paybacks,
    transfers, notes and tags. A narrow list gives each row two lines.
  - Each month starts with a card showing income, spending, and what's left of the budget or how far over it is.
  - Scrolling loads the earlier months.
- **The transaction pane:**
  - On a wide screen the selected transaction opens beside the list, which stays usable; elsewhere, over it.
  - Date, name, category, notes, tags, type and review come first, each saved as soon as it's changed.
  - Split and Recurring are buttons. Rarer actions sit under a menu: link a payback or the other side of a move,
    combine, make a rule, unsplit and delete.
  - Splits, paybacks, links and the other side of a move show only when there are some. The bank's details are
    folded at the bottom.
- **Kept as it is:**
  - The list still groups splits, links, paybacks and transfers the same way, in the same order within a day.
  - Every page still works without JavaScript, except connecting a bank.

## v0.2.x: after v0.2.0

In this order, each step its own release.

1. **Cleaner names.**
   - New transactions lose the confirmation codes and reference numbers banks add: "Online transfer to savings
     Conf# 1A2B3C4D" shows as "Online transfer to savings". When Plaid knows the merchant, its name still comes first.
   - Rename rules beside the category rules: renaming a transaction offers to rename the others with that name, now
     and later.
   - Names changed by hand stay as they are, and the bank's own text stays in the transaction's bank details.
   - Earlier transactions get the same cleanup once, after a preview.
2. **Filters you can see, selecting and the keyboard.**
   - A filter menu has a labeled field for each filter: dates (presets or custom), accounts, categories, tags, type,
     review status and transfers. Amount gets a choice of equals, between, more than or less than.
   - Active filters show as chips you can remove. The list sorts by date or amount, and totals show what's listed as
     spent, income and net.
   - Every row has a checkbox. With two or more ticked, the pane edits them together: category, tags, review,
     exclude, link and combine.
   - ↑ and ↓ move, X selects, R marks reviewed, C changes the category, / searches and Esc closes.
3. **The other pages** laid out like the transactions: the dashboard, budgets, recurring, accounts and settings.
4. **Reports: where the money goes.**
   - Pick a period: this month, the last 3 or 12 months, year to date, last year, or custom. Totals come by month,
     quarter or year.
   - Show spending, income, net and savings rate, grouped by category, merchant, account or tag.
   - Leave things out for the report only, without changing any transaction: categories, tags, merchants, accounts,
     single transactions, or anything above an amount. For example, a year's spending without one large category.
   - Charts:
     - stacked bars over time
     - monthly averages
     - change from the previous period or the same period last year
     - top merchants and biggest expenses
   - Click a bar to see its transactions. Save a report and pin it to the dashboard.
   - It reuses the filter menu.
5. **Categories and budgets.**
   - Budgets move into a Categories page with spending pace and expected recurring charges.
   - Each category gets its own page: history, average, year to date and top merchants.
   - Budget rollover and budgets that differ by month.

## Later

Not planned in detail yet:

- a month-in-review summary
- a page for each account, with its balance history
- a recurring calendar
- importing CSV files from other banks
- savings goals
- investment holdings
