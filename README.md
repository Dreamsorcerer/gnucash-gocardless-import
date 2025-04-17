# Importing bank transactions into GnuCash

This script can be used to automatically import transactions from UK (and a few other countries) bank accounts.
The download_transactions() function could also be used directly if you don't use GnuCash and just want the data.

## Get API access

First you'll need to get API access from GoCardless. You'll want to sign up to the free plan for Account Data
(https://gocardless.com/bank-account-data/), which currently requires scrolling down the page to find the sign up button.

Once in to the dashboard, you can go to Developers -> User Secrets and create a new secret pair.
Now you're ready to setup the script.

## Get a refresh token

Start by copying the gnucash_imports.py file somewhere you want to run it from.
Make sure the file is executable: `chmod +x gnucash_import.py`

To start, run: `./gnucash_import.py -m token`

Paste in the secrets created in the dashboard and you'll receive a refresh token.

Edit the gnucash_import.py file and paste the refresh token into the global variable near the top of the file.

## Register an account

To register a new bank account, run: `./gnucash_import.py -m register`

After entering the country, you'll get a list of institutions. Find the one you want to add and copy/paste
the institution's code.

You'll then be given a URL to navigate to in a browser in order to complete the authorisation with your account.
Upon completion you'll be redirected to a localhost page that doesn't exist. Return to the script and enter
`y` to get the resulting list of account IDs.

You'll now need to paste these account IDs into the `ACCOUNTS` global variable. You'll want to replace the
example configuration that's already in the script. The sample one shows using 2 accounts files (named
`personal.gnucash` and `business.gnucash` in the user's home directory), with 1 bank account in the first file
that maps to the `Assets.Current Account` account in GnuCash.

> [!NOTE]  
> Banks allow access for 90 days. Then you'll need to repeat the authorisation process again.

## Importing transactions

> [!IMPORTANT]  
> Banks have rather restrictive rate limits. I suggest avoiding running this script against live accounts
> more than twice in any given day.

Once accounts are setup, you can import transactions by running `./gnucash_import.py`. This will fetch all
the transactions for all the accounts and create them in the respective GnuCash accounts.

The script will attempt to find already existing transactions that match the transaction, so manually created
transactions will get linked correctly most of the time. Previously existing transactions will also get
marked as reconciled as long as the amounts still match correctly.

> [!WARNING]
> Because the script will reconcile transactions it sees for the second time, you should avoid running the
> script twice in a row, as this will cause them to be reconciled without a chance for you to check them first.

When creating a new transaction, the script will also look for previous transactions to the same entity
and reuse the same splits and description. This means the script should learn over time and reduce the amount
of work needed to mark up all the transactions correctly in future.

At the end of the run, the script will also compare the balance in the accounts. If they fail to match, you'll
get a warning asking you to perform a manual reconciliation. It will display the expected balance and the date
which should both be entered into the reconciliation window (note that for liability accounts, the balance
may be negative, but should be entered as a positive value in GnuCash).
