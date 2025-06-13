#!/usr/bin/env python3

import argparse
import asyncio
import math
import os
import re
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Literal, NewType, TypedDict

from aiohttp import ClientSession
from gnucash import Session, Transaction, Split, GncNumeric

AccId = NewType("AccId", str)
AccsConfig = dict[AccId, tuple[str, Literal["bookingDate", "valueDate"]]]

# These variables need to be configured:
DISABLE_LOGS = True
REFRESH_TOKEN = ""
ACCOUNTS: dict[Path, AccsConfig] = {
    Path.home() / "personal.gnucash": {
        # Some institutions seem to swap bookingDate/valueDate.
        AccId("5328e9d3-84dc-413b-8e51-b7d240075cd8"): ("Assets.Current Account", "bookingDate"),
    },
    Path.home() / "business.gnucash": {
    },
}


API = "https://bankaccountdata.gocardless.com/api/v2/"
HEADERS = MappingProxyType({"Accept": "application/json"})

if DISABLE_LOGS:
    # Log files don't seem to respect user preferences.
    os.environ["GNUCASH_LOGFILE"] = "NUL"


class Mode(Enum):
    transactions = "transactions"
    register = "register"
    token = "token"


class _Amount(TypedDict):
    amount: str
    currency: str


class TransactionData(TypedDict):
    bookingDate: str
    internalTransactionId: str
    remittanceInformationUnstructured: str
    transactionAmount: _Amount
    valueDate: str


class TransactionsGroup(TypedDict):
    booked: list[TransactionData]
    pending: list[TransactionData]


async def refresh(sess: ClientSession) -> None:
    async with sess.post(API + "token/refresh/", json={"refresh": REFRESH_TOKEN}) as resp:
        if not resp.ok:
            print("Response status:", resp.status)
            print(await resp.text())
            raise RuntimeError()
        data = await resp.json()
    sess.headers["Authorization"] = f"Bearer {data['access']}"


async def _download_account(sess: ClientSession, acc_id: AccId) -> tuple[AccId, float, TransactionsGroup]:
    async with sess.get(API + f"accounts/{acc_id}/balances/") as resp:
        if not resp.ok:
            print("Response status:", resp.status)
            print(await resp.text())
            raise RuntimeError()
        data = await resp.json()
    balances = {b["balanceType"]: b for b in data["balances"]}
    balance = None
    # The first balanceType we find in this list is likely the balance we want to know.
    for k in ("expectedClosed", "interimBooked", "closingBooked", "openingBooked", "information", "interimAvailable", "closingAvailable", "openingAvailable"):
        if k in balances:
            balance = float(balances[k]["balanceAmount"]["amount"])
            break
    assert balance is not None, balances

    async with sess.get(API + f"accounts/{acc_id}/transactions/") as resp:
        if not resp.ok:
            print("Response status:", resp.status)
            print(await resp.text())
            raise RuntimeError()
        data = await resp.json()
    transactions = data["transactions"]

    return acc_id, balance, transactions


async def download_transactions() -> tuple[dict[AccId, float], dict[AccId, TransactionsGroup]]:
    async with ClientSession(headers=HEADERS) as sess:
        await refresh(sess)

        tasks = []
        for f, accounts in ACCOUNTS.items():
            for acc_id in accounts:
                tasks.append(_download_account(sess, acc_id))

        balances = {}
        transaction_data = {}
        for acc_id, balance, transactions in await asyncio.gather(*tasks):
            balances[acc_id] = balance
            transaction_data[acc_id] = transactions
    return balances, transaction_data


def _import_transactions(session: Session, accounts: AccsConfig, transactions: dict[AccId, TransactionsGroup]) -> None:
    root = session.book.get_root_account()
    for acc_id, (acc_path, date_key) in accounts.items():
        gc_account = root.lookup_by_full_name(acc_path)

        # Build search index of transactions.
        gc_splits = gc_account.GetSplitList()
        split_by_txid = {m.group(1): s for s in gc_splits if (m := re.search(r"TXID: (.+?)(;|$)", s.GetMemo()))}
        gc_splits = [s for s in gc_splits if s not in split_by_txid.values()]
        splits_by_name: dict[str, list[Split]] = {}
        for split in split_by_txid.values():
            m = re.search(r"TXNAME: (.+?)(;|$)", split.GetMemo())
            if m is None:
                continue
            name = m.group(1)
            splits_by_name.setdefault(name, []).append(split)
        for splits in splits_by_name.values():
            splits.sort(key=lambda s: s.parent.GetDate())

        for tx_data in transactions[acc_id]["booked"]:  # TODO: Include pending?
            tx_date = datetime.fromisoformat(tx_data[date_key])

            existing_split = split_by_txid.get(tx_data["internalTransactionId"])
            if existing_split:
                if not math.isclose(existing_split.GetAmount().to_double(), float(tx_data["transactionAmount"]["amount"])):
                    print("ERROR: Can't reconcile due to incorrect amounts ({})".format(tx_data))
                    continue
                existing_split.SetReconcile("y")
                continue

            # Search for existing transaction that matches.
            candidates = []
            for split in gc_splits:
                if math.isclose(split.GetAmount().to_double(), float(tx_data["transactionAmount"]["amount"])):
                    min_date = split.parent.GetDate() - timedelta(5)
                    max_date = split.parent.GetDate() + timedelta(5)
                    if min_date < tx_date < max_date:
                        candidates.append(split)

            if candidates:
                split = min(candidates, key=lambda s: abs(s.parent.GetDate() - tx_date))
                note = split.GetMemo()
                if note:
                    note += "; "
                note += f"TXID: {tx_data['internalTransactionId']}; TXNAME: {tx_data['remittanceInformationUnstructured']};"
                split.SetMemo(note)
                split.parent.SetDate(tx_date.day, tx_date.month, tx_date.year)
                continue

            # Create new transaction.
            tx = Transaction(session.book)
            tx.BeginEdit()
            tx.SetCurrency(session.book.get_table().lookup("CURRENCY", tx_data["transactionAmount"]["currency"]))

            new_split = Split(session.book)
            new_split.SetValue(GncNumeric(float(tx_data["transactionAmount"]["amount"])))
            new_split.SetAccount(gc_account)
            new_split.SetParent(tx)
            new_split.SetMemo(f"TXID: {tx_data['internalTransactionId']}; TXNAME: {tx_data['remittanceInformationUnstructured']};")

            desc = tx_data["remittanceInformationUnstructured"]
            prev_splits = splits_by_name.get(tx_data["remittanceInformationUnstructured"])
            if prev_splits:
                prev_split = prev_splits[-1]
                prev_tx = prev_split.parent
                desc = prev_tx.GetDescription()

                total = prev_split.GetAmount().to_double()
                for other_split in filter(lambda s: s != prev_split, prev_split.parent.GetSplitList()):
                    new_split = Split(session.book)
                    ratio = other_split.GetValue().to_double() / prev_split.GetAmount().to_double()
                    new_split.SetValue(GncNumeric(float(tx_data["transactionAmount"]["amount"]) * ratio))
                    new_split.SetAccount(other_split.GetAccount())
                    new_split.SetParent(tx)

            tx.SetDate(tx_date.day, tx_date.month, tx_date.year)
            tx.SetDescription(desc)
            tx.CommitEdit()


async def import_transactions() -> None:
    balances, transactions = await download_transactions()

    for f, accounts in ACCOUNTS.items():
        with Session(str(f)) as session:
            _import_transactions(session, accounts, transactions)

            for acc_id, (acc_path, date_key) in accounts.items():
                amount = balances[acc_id]
                acc = session.book.get_root_account().lookup_by_full_name(acc_path)
                if not math.isclose(acc.GetBalance().to_double(), amount):
                    print(f"{acc_path} balance out of sync, please reconcile.")
                    print(f"Expected: {amount}")


async def register_account() -> None:
    country = ""
    while len(country) != 2:
        country = input("Country code (default: GB): ") or "GB"

    async with ClientSession(headers=HEADERS) as sess:
        await refresh(sess)

        async with sess.get(API + "institutions/", params={"country": country}) as resp:
            if not resp.ok:
                print("Response status:", resp.status)
                print(await resp.text())
                raise RuntimeError()
            data = await resp.json()
            for b in data:
                print(f"{b['id']}: {b['name']}")

        inst_id = input("Institution ID: ")
        r = {"redirect": "http://localhost/success", "institution_id": inst_id}
        async with sess.post(API + "requisitions/", json=r) as resp:
            if not resp.ok:
                print("Response status:", resp.status)
                print(await resp.text())
                raise RuntimeError()
            data = await resp.json()
            req_id = data["id"]
            print("Navigate to:", data["link"])

        y = ""
        while y.lower().strip() != "y":
            y = input("Enter 'y' when complete: ")

        async with sess.get(API + "requisitions/" + req_id + "/") as resp:
            if not resp.ok:
                print("Response status:", resp.status)
                print(await resp.text())
                raise RuntimeError()
            data = await resp.json()
            print("Account IDs (Add these to the ACCOUNTS global):")
            for acc_id in data["accounts"]:
                print(acc_id)


async def fetch_token() -> None:
    secret_id = input("Secret ID: ")
    secret_key = input("Secret Key: ")
    data = {"secret_id": secret_id, "secret_key": secret_key}

    async with ClientSession() as sess:
        async with sess.post(API + "token/new/", headers=HEADERS, json=data) as resp:
            if resp.ok:
                d = await resp.json()
                print("Set the global in the code:")
                print(f'REFRESH_TOKEN = "{d["refresh"]}"')
            else:
                print("Status:", resp.status)
                print(await resp.text())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-m", "--mode", type=Mode, default=Mode.transactions)
    args = parser.parse_args()

    f = {Mode.transactions: import_transactions, Mode.register: register_account,
         Mode.token: fetch_token}[args.mode]
    asyncio.run(f())
