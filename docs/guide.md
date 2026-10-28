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

The search field at the top of the sidebar looks through names and notes; on **Transactions** it searches within the filters you picked there. Below the pages, the sidebar lists your accounts by group with their balances: click one to see its transactions, or fold a group away. Hidden and archived accounts aren't listed. On a phone, **Transactions** has its own search field.

## Reviewing new transactions

New transactions wait on **Dashboard → To review** and **Transactions → To review**. Pick the right category, or press ✓ if it's already right. When you change a category, the app offers to do the same for every transaction with that name, now and later.

## Editing a transaction

Click a transaction to open it in a panel over the list; the list stays where it was when you save or close it. A transaction's date is the day you spent the money, not the day it posted, and your bank's updates never move it to a later day. A date you set by hand stays, whatever the bank says later.

## Leaving something out of spending

Pick **🚫 Excluded** in its category menu, in the list or on its page. A transaction is either in a category or excluded: excluding clears its category, and picking a category counts it again.

## Splitting a transaction

Open it and use **Split this transaction**: give each part an amount, a category and a name. Leave one amount empty and it gets what's left. In the list, the split gets a row of its own with its parts under it, each counting in its own category. Open a part to change its type, notes or tags, or to exclude it. Open the split's row to change the parts, rename it, change its date (its parts share it) or unsplit it.

If your bank later changes the amount (a tip, say), the split comes back to **To review** so you can fix the parts.

## Putting a split back together

For items you split in Copilot Money that have no bank transaction to rebuild them from (say, on a card that isn't connected), open one and use **Combine with others into one**: tick the others and name it. They then show together, each keeping its category. **Ungroup** separates them again, each on its own date.

## When friends pay you back

Open the money they sent and choose the purchase under **Paying you back for a purchase?**, or add it from the purchase's page. The purchase then counts only your share, in its own month and category. When friends send more than it cost (rounding up, say), it's paid back in full and the extra counts like a refund.

## Moves between your own accounts

When both sides of a move arrive (opposite amounts on two of your accounts, within 3 days), they're paired, marked ↔, and never count as spending or income. Paired moves are hidden from the list; **Show** brings them back. A transfer without its other side stays visible, since it may have gone to someone else. You can pair or unpair the other side by hand on a transaction's page.

## Linking transactions that belong together

Rent paid in two payments, say: **Transactions → Select**, tick them, and press **🔗 Link**. They show together on the biggest one's day, joined by a line, and each keeps its own amount, category and date. Open one to see their total or to unlink it.

## Changing many at once

**Transactions → Select**, tick the rows (or **All on this page**), then use the bar at the bottom: add or remove a tag, set a category, mark reviewed, exclude or include, link or unlink, or **Combine into one**.

## Tags

On a transaction's page, switch tags on or off or type new ones. Pick a tag above the list to see everything with it, and take it off all of them at once from there. Rename, recolor or delete tags on **Categories & rules**.

## Cash, gift cards and store credit

**Transactions → Add transaction** adds a transaction by hand, for cash or an account that isn't connected; only these can be deleted. For a gift card or a store's credit, add an account on **Accounts → Add an account**: refunds that went there and orders it paid for go on it, and the Accounts page shows what's left.

**Archive** an account you don't use any more: its history still counts, but it's no longer offered when you add a transaction.

## Your bank connections

- **Left out an account?** Use **Add or remove accounts** next to that bank on Accounts. It logs in to the same connection, so it doesn't use a new slot of Plaid's Trial plan.
- **Once a year**, many banks ask you to approve access again. From two weeks before, the app shows a banner and a **Log in again** button. That doesn't use a new slot either.

## Moving to a new computer

Bring over the project folder with its `backups/` and `.env` (or make `.env` again from `.env.example` with your keys). Then run `mkdir -p data import && cp backups/money-<date>.db data/money.db` and `docker compose up -d`. A copy includes your bank connections, so you don't connect your banks again.
