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

## v0.2.1: cleaner names

- **Names:**
  - New transactions lose the confirmation codes, reference numbers and account numbers banks add: "Online transfer
    to savings Conf# 1A2B3C4D" shows as "Online transfer to savings". When Plaid knows the merchant, its name still
    comes first.
  - Renaming a transaction offers to rename the others with that name, now and later, through a rule beside the
    category rules.
  - Names changed by hand stay as they are, and the bank's own text stays in the transaction's bank details.
  - Earlier transactions get the same cleanup once, after a preview, and rules can name new transactions the way the
    earlier ones are named.
- **Colors:** each category gets a color of its own, from 13.

## v0.2.2: filters you can see, selecting and the keyboard

- **Filters:** a menu with a labeled field for each: dates (this month, last month, the last 3 or 12 months, this
  year, last year, one month, or days of your own), the amount (equals, between, more than or less than), type,
  review, moves between accounts, and accounts, categories and tags, several at once. The list changes as you pick.
- The filters in use show as chips you can remove. The list sorts by date or by amount, and totals show what's listed
  as spent, income and net.
- **Selecting:** each row's checkbox shows under the pointer, as in Copilot; Shift-click ticks a range, and on a phone
  Select shows them all. With two or more ticked, the pane changes them together: category, tags, review, link and
  combine.
- **Keys:** ↑ and ↓ move, Return opens, X ticks, R marks reviewed, C changes the category, / searches, Esc closes,
  and ? lists them.

## v0.2.3: reports, budgets and a page for everything

The rest of the v0.2 plans, in one release.

- **Reports:** spending or income over any span (this month, last month, the last 3 or 12 months, this year, last year,
  one month, or days of your own), by month, quarter or year, and by category, merchant, account or tag.
  - Totals with the share of income kept and a month's average, compared with the span before and a year earlier.
  - Stacked bars with a readout and a table, top merchants and the biggest expenses.
  - Leave things out for one report alone, click a bar for its transactions, and save a report or pin it to the
    dashboard.
- **Everyday spending:** set aside big, rare costs, like tuition: categories, tags, merchants, single transactions or
  anything over an amount. They still count, but the dashboard's months show them apart, and a report can leave them
  out.
- **Categories and budgets:** Budgets became Categories, each with its spending pace and the recurring charges still to
  come, and a page of its own: its last 12 months, its average, the year so far and its top merchants. Budgets can
  differ by month and roll over what's left or over. The old settings page is now Rules & tags.
- **A month in review:** what was spent against the budgets, the share of income kept, what changed most from the
  month before, the biggest expenses and how net worth moved. The dashboard offers it in a new month's first days.
- **A page for each account:** its balance over time, its transactions, and a card's limit used. The sidebar's
  accounts open it.
- **Recurring:** a calendar of charges and paydays, and what's due before the next payday. Charges and accounts open
  beside their lists, as transactions do.
- **Light or dark** by choice, from the sidebar or Settings.

## v0.3: investments, and maybe savings goals

- **Investments:** what each investment account holds, from Plaid: its holdings, their value and how it changed.
  Investments count in net worth.
- **Savings goals**, if they turn out to be wanted: a target, a date, and an account it's saved in; how far along it
  is, and how much a month it takes to get there in time.
