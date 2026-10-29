# User guide

## Everyday commands

Run these in the project folder.

| | |
|---|---|
| Open the app | http://localhost:8080 |
| Start | `docker compose up -d` |
| Stop | `docker compose stop` |
| After changing `.env` | `docker compose up -d` |
| After updating the code | `git pull`, then `docker compose up -d --build` |
| See the logs | `docker compose logs -f` |

The app restarts on its own whenever Docker is running. While it runs, it syncs your banks every 6 hours, and **Sync now** syncs them right away. If the computer was off, the next sync catches up.

## Finding things

The search field at the top of the sidebar looks through names and notes; on **Transactions** it searches within the filters you picked there. Below the pages, the sidebar lists your accounts by group with their balances: click one for its page (its balance over time, its latest transactions, and for a card how much of its limit is used), or fold a group away. Hidden and archived accounts aren't listed. On a phone, **Transactions** has its own search field.

On **Transactions**, **Filters** opens a field for each filter:

- **Dates:** a span (this month, last month, the last 3 or 12 months, this year or last year), one month, or days of your own.
- **Amount:** equals, between, more than or less than, money in and out alike.
- **Type**, **Review**, and whether to show moves between your own accounts.
- **Accounts**, **Categories** (Uncategorized and Excluded among them) and **Tags**: tick several to see any of them.

The list changes as you pick. The filters in use show as chips under the menu: press one to remove it. **Newest first** turns into **Biggest first**, one list by amount. Above the list, what it adds up to: spent, income and net, counted as the dashboard counts them.

## Light or dark

The app follows your system's light or dark mode. To pick one yourself, press the moon or the sun at the top of the sidebar (at the top of the screen on a phone), or choose in **Settings → Appearance**, where **Same as the system** goes back.

## Reports

**Reports** shows where the money went over a span of time: the last 12 months to start with, or this month, last month, the last 3 months, this year, last year, one month, or dates of your own.

- Totals come **by month**, **by quarter** or **by year**, for **spending** or **income**, and **by category**, **merchant**, **account** or **tag**. Up to seven of them get a color of their own; the rest add up as **Other**.
- Above the chart: what was spent and earned, the difference, how much of your income you kept, and a month's spending on average. Each compares with the span before and with the same span a year earlier, when your history goes back that far.
- Point at a bar for its numbers, or press **Show as a table**. Click a part of a bar to see its transactions, beside the report; click a transaction there to open it.
- **Leave out** takes things out of the report only, without changing any transaction: categories (a group with its subcategories), accounts, tags, merchants, anything over an amount, and single transactions, with the **×** beside them in **Top merchants** and **Biggest expenses**. The chips under the menu bring each back.
- **Save** keeps a report under a name, listed above it. **Pin to dashboard** shows it there too.

Reports count the way the dashboard does: a friend's payback goes with its purchase, a split counts through its parts, and transfers, excluded transactions and hidden accounts count nowhere.

## A month in review

In a new month's first week, the dashboard offers last month's review; any month's is at `/review`, with arrows to the months around it. It shows what you spent against your budgets and earned, how much of your income you kept, the categories and merchants whose spending changed most from the month before, the biggest expenses, which budgets you kept, and how your net worth moved.

## Everyday spending

A big, rare cost, like a semester's tuition, makes one month's bar tower over the others. Set it aside to see what you spend from day to day: it still counts everywhere, but apart.

1. Open the transaction and pick **Set aside: not everyday spending** under **⋯**. To set aside more at once, use **Set aside…** above **Spending by month** on the dashboard: categories, tags, merchants, or anything over an amount.
2. Switch on **Everyday** above the chart: each month's bar shows what was set aside on top, hatched gray, and its readout and table give both parts.
3. In **Reports**, tick **Leave out what isn't everyday spending** under **Leave out**.

## Categories and budgets

**Categories** shows each category's spending in the month against its budget: what's left or how far over, whether it's ahead of the month's pace (the tick on its bar is how far through the month you are), and the recurring charges still to come, hatched on its bar. A group adds up its subcategories.

1. **Edit budgets** changes the month's budgets, for that month and the ones after, or for that month only (a bigger one for December, say).
2. Open a category for its own page: its last 12 months, a month on average, the year so far, its top merchants and the month's transactions.
3. On that page, **Roll over** carries what's left of a month's budget into the next one, and what went over it too, from the month you switch it on. Its emoji, color, group and name are there too.

Rules, tags and how Plaid's categories map to yours are on **Rules & tags**.

## Reviewing new transactions

New transactions wait on **Dashboard → To review** and **Transactions → To review**. Pick the right category, or press ✓ if it's already right. When you change a category, the app offers to do the same for every transaction with that name, now and later.

## Editing a transaction

Click a transaction to open it beside the list (over it on a smaller screen); the list stays where it was. Changes save as soon as you make them: the name, the date, the category, notes, tags, the type and **Reviewed**. **Split** and **Recurring** are at the top, and the rarer actions, like linking a payback or the other side of a move, are under **⋯**. A transaction's date is the day you spent the money, not the day it posted, and your bank's updates never move it to a later day. A date you set by hand stays, whatever the bank says later.

## Names from your bank

New transactions come without the codes and account numbers banks add to their names, like "Conf# 1A2B3C4D" or "SAV 1234", and without the note a Zelle sender wrote. When Plaid knows the merchant, the transaction gets its name. What the bank sent stays under **From your bank** on the transaction.

1. Rename a transaction, and the app offers to rename the others with that name, and the new ones that come with it.
2. Taking the offer makes a rule. Rules are listed on **Rules & tags**, where **Rename to** makes one by hand.
3. A name you change by hand stays, whatever the bank or a rule says later.
4. **Settings → Names from your bank** offers rules that name new transactions the way your earlier ones are named, in your Copilot history or by you. Below them, it lists the earlier transactions whose names can still be cleaned up. Nothing changes until you press a button.

## Leaving something out of spending

Pick **🚫 Excluded** in its category menu, in the list or on its page. A transaction is either in a category or excluded: excluding clears its category, and picking a category counts it again.

## Splitting a transaction

Open it and press **Split**: give each part an amount, a category and a name. Leave one amount empty and it gets what's left. In the list, the split gets a row of its own with its parts under it, each counting in its own category. Open a part to change its type, notes or tags, or to exclude it. Open the split's row to rename it, change its date (its parts share it), use **Change the split**, or unsplit it from **⋯**.

If your bank later changes the amount (a tip, say), the split comes back to **To review** so you can fix the parts.

## Putting a split back together

For items you split in Copilot Money that have no bank transaction to rebuild them from (say, on a card that isn't connected), open one and pick **Combine with others into one** under **⋯**: tick the others and name it. They then show together, each keeping its category. **Ungroup**, under **⋯** on the combined line, separates them again, each on its own date.

## When friends pay you back

Open the money they sent and pick **Link the purchase it pays back** under **⋯**, or **Link a friend's payback** on the purchase. The purchase then counts only your share, in its own month and category. When friends send more than it cost (rounding up, say), it's paid back in full and the extra counts like a refund.

## Moves between your own accounts

When both sides of a move arrive (opposite amounts on two of your accounts, within 3 days), they're paired, marked ↔, and never count as spending or income. Paired moves are hidden from the list; **Show** brings them back. A transfer without its other side stays visible, since it may have gone to someone else. You can pair the other side by hand with **Link the other side of a move** under **⋯**, or unpair it.

## Linking transactions that belong together

Rent paid in two payments, say: tick them on **Transactions** and press **Link them**. They show together on the biggest one's day, joined by a line, and each keeps its own amount, category and date. Open one to see their total or to unlink it.

## Changing many at once

1. Tick the transactions: point at a row and its checkbox shows, to the left of its badge. Shift-click another to tick the ones between. On a phone, press **Select**, then tap the rows.
2. With two or more ticked, they open together beside the list. On a smaller screen, press **Change them** in the bar at the bottom.
3. Change them all at once: the category (or **Excluded**), tags, reviewed or not, link or unlink, or combine them into one.
4. **Clear selection** unticks them.

## Keyboard shortcuts

On **Transactions**, press **?** to see them:

| Key | |
|---|---|
| ↑ ↓ | Move through the list; an open transaction follows |
| Return | Open the transaction |
| X | Tick it, or untick it |
| R | Mark it reviewed, or the ticked ones |
| C | Change its category |
| / | Search |
| Esc | Close the menu or the transaction, then untick |

## Recurring charges

**Recurring** starts with a calendar of the month: what came, in gray, and what's expected from today on, paydays in green. The arrows show other months, and above the calendar, when your next payday is and what's due before it.

Below it are the charges you track, what's still to come this month first; each opens beside the list, where you change its name, how often it comes, its usual amount and its category, or pause it, and see the charges it matched. **Track a charge** tracks one by hand, a paycheck too (its amount as money in, like 2450), and **Looks recurring** offers the ones that repeat on a schedule.

## Tags

On a transaction, type a tag to add it (an existing one or a new one), or press × on one to take it off. Pick a tag above the list to see everything with it, and take it off all of them at once from there. Rename, recolor or delete tags on **Rules & tags**.

## Cash, gift cards and store credit

**Transactions → Add transaction** adds a transaction by hand, for cash or an account that isn't connected; only these can be deleted. For a gift card or a store's credit, add an account on **Accounts → Add an account**: refunds that went there and orders it paid for go on it, and the Accounts page shows what's left.

**Archive** an account you don't use any more: its history still counts, but it's no longer offered when you add a transaction. Open an account, from the sidebar or **Accounts**, to see its balance over time and its latest transactions, and to rename, hide or archive it.

## Your bank connections

- **Left out an account?** Use **Add or remove accounts** next to that bank on Accounts. It logs in to the same connection, so it doesn't use a new slot of Plaid's Trial plan.
- **Once a year**, many banks ask you to approve access again. From two weeks before, the app shows a banner and a **Log in again** button. That doesn't use a new slot either.

## Moving to a new computer

Bring over the project folder with its `backups/` and `.env` (or make `.env` again from `.env.example` with your keys). Then run `mkdir -p data import && cp backups/money-<date>.db data/money.db` and `docker compose up -d`. A copy includes your bank connections, so you don't connect your banks again.
