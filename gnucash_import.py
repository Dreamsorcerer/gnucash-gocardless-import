#!/usr/bin/env python3

import argparse
import asyncio
import json
import math
import os
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Literal, NewType, TypedDict

from aiohttp import ClientSession
from gnucash import Session, Transaction, Split, GncNumeric

try:
    from gi.repository import GLib
except ImportError:
    import os
    config_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
else:
    config_dir = Path(GLib.get_user_config_dir())

AccId = NewType("AccId", str)


class AccountData(TypedDict):
    gc_account: str
    date_key: Literal["bookingDate", "valueDate"]
    iban: str
    inst: str


class Config(TypedDict, total=False):
    secret_id: str
    secret_key: str
    token: str
    accounts: dict[str, dict[AccId, AccountData]]


API = "https://bankaccountdata.gocardless.com/api/v2/"
CONFIG_PATH = config_dir / "gnucash-import"
CONFIG: Config = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else Config()
DISABLE_LOGS = True
UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

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
    async with sess.post(API + "token/refresh/", json={"refresh": CONFIG["token"]}) as resp:
        if resp.status == 401:
            await fetch_token(sess, interactive=False)
            return
        if not resp.ok:
            print("Response status:", resp.status)
            print(await resp.text())
            raise RuntimeError()
        data = await resp.json()
    sess.headers["Authorization"] = f"Bearer {data['access']}"


async def _reconfirm_account(sess: ClientSession, eua_id: str) -> None:
    async with sess.post(API + f"agreements/enduser/{eua_id}/reconfirm/") as resp:
        if not resp.ok:
            print("Response status:", resp.status)
            print(await resp.text())
            raise RuntimeError()
        data = await resp.json()
    print("Navigate to:", data["reconfirmation_url"])

    y = ""
    while y.lower().strip() != "y":
        y = input("Enter 'y' when complete: ")


async def _download_account(sess: ClientSession, acc_id: AccId) -> tuple[AccId, float, TransactionsGroup]:
    for retry in range(2):
        async with sess.get(API + f"accounts/{acc_id}/balances/") as resp:
            if retry == 0 and resp.status == 401:
                error = await resp.json()
                eua_id = re.search(UUID_RE, error.get("summary", ""))
                if eua_id:
                    await _reconfirm_account(sess, eua_id.group(0))
                    continue

            if not resp.ok:
                print("Response status:", resp.status)
                print(await resp.text())
                raise RuntimeError()
            data = await resp.json()
            break

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


async def download_transactions(sess: ClientSession) -> tuple[dict[AccId, float], dict[AccId, TransactionsGroup]]:
    await refresh(sess)

    tasks = []
    for f, accounts in CONFIG["accounts"].items():
        for acc_id in accounts:
            tasks.append(_download_account(sess, acc_id))

    balances = {}
    transaction_data = {}
    for acc_id, balance, transactions in await asyncio.gather(*tasks):
        balances[acc_id] = balance
        transaction_data[acc_id] = transactions
    return balances, transaction_data


def _import_transactions(session: Session, accounts: dict[AccId, AccountData], transactions: dict[AccId, TransactionsGroup]) -> None:
    root = session.book.get_root_account()
    for acc_id, acc in accounts.items():
        gc_account = root.lookup_by_full_name(acc["gc_account"])
        if gc_account is None:
            raise RuntimeError(f"Account name not found: {gc_account}")

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

        for tx_data in transactions[acc_id]["booked"]:
            desc = tx_data["remittanceInformationUnstructured"]
            tx_date = datetime.fromisoformat(tx_data[acc["date_key"]])

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

                txname = desc
                note += f"TXID: {tx_data['internalTransactionId']}; TXNAME: {txname};"
                split.SetMemo(note)
                split.parent.SetDate(tx_date.day, tx_date.month, tx_date.year)
                continue

            # Create new transaction.
            tx = Transaction(session.book)
            tx.BeginEdit()
            tx.SetDate(tx_date.day, tx_date.month, tx_date.year)
            tx.SetCurrency(session.book.get_table().lookup("CURRENCY", tx_data["transactionAmount"]["currency"]))

            new_split = Split(session.book)
            new_split.SetValue(GncNumeric(float(tx_data["transactionAmount"]["amount"])))
            new_split.SetAccount(gc_account)
            new_split.SetParent(tx)
            new_split.SetMemo(f"TXID: {tx_data['internalTransactionId']}; TXNAME: {desc};")

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

            tx.SetDescription(desc)
            tx.CommitEdit()


async def import_transactions(sess: ClientSession) -> None:
    balances, transactions = await download_transactions(sess)

    for f, accounts in CONFIG["accounts"].items():
        with Session(str(Path(f).expanduser())) as session:
            _import_transactions(session, accounts, transactions)

            for acc_id, acc in accounts.items():
                amount = balances[acc_id]
                gc_acc = session.book.get_root_account().lookup_by_full_name(acc["gc_account"])
                if not math.isclose(gc_acc.GetBalance().to_double(), amount):
                    print(f"{acc['gc_account']} balance out of sync, please reconcile.")
                    print(f"Expected: {amount}")


async def register_account(sess: ClientSession) -> None:
    country = ""
    while len(country) != 2:
        country = input("Country code (default: GB): ") or "GB"

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
    r = {"access_valid_for_days": "730", "institution_id": inst_id, "reconfirmation": True}
    async with sess.post(API + "agreements/enduser/", json=r) as resp:
        if not resp.ok:
            print("Response status:", resp.status)
            print(await resp.text())
            raise RuntimeError()
        data = await resp.json()
        eua_id = data["id"]

    r = {"agreement": eua_id, "redirect": "http://localhost/success",
         "institution_id": inst_id}
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

    for acc_id in data["accounts"]:
        async with sess.get(API + f"accounts/{acc_id}") as resp:
            if not resp.ok:
                print("Response status:", resp.status)
                print(await resp.text())
                raise RuntimeError()
            account_summary = await resp.json()
        iban = account_summary["iban"]
        inst = account_summary["institution_id"]

        try:
            file_path, old_acc_id = next(
                (f, k) for f, accs in CONFIG.get("accounts", {}).items() for k, a in accs.items()
                if a["inst"] == inst and a["iban"] == iban
            )
        except StopIteration:
            file_paths = tuple(CONFIG.get("accounts", {}).keys())
            print()
            print("Select gnucash file for {} (IBAN: {}):".format(acc_id, iban))
            for i, p in enumerate(file_paths, 1):
                print(" {} - {}".format(i, p))
            print(" 0 - Enter new path")
            selection = -1
            while selection < 0 or selection > len(file_paths):
                with suppress(ValueError):
                    selection = int(input("> "))

            if selection == 0:
                file_path = input("Enter file path (e.g. ~/personal.gnucash): ")
            else:
                file_path = file_paths[selection - 1]

            account = input("Enter GNUCash account (e.g. Assets.Current Account): ")

            CONFIG.setdefault("accounts", {}).setdefault(file_path, {})[acc_id] = {
                "date_key": "bookingDate",
                "gc_account": account,
                "iban": iban,
                "inst": inst,
            }
        else:
            acc_config = CONFIG["accounts"][file_path].pop(old_acc_id)
            CONFIG["accounts"][file_path][acc_id] = acc_config

    CONFIG_PATH.write_text(json.dumps(CONFIG, sort_keys=True, indent=4))


async def fetch_token(sess: ClientSession, interactive: bool = True) -> None:
    if interactive:
        msg = " ({})".format(CONFIG["secret_id"]) if "secret_id" in CONFIG else ""
        secret_id = input(f"Secret ID{msg}: ") or CONFIG["secret_id"]
        msg = " ({})".format(CONFIG["secret_key"]) if "secret_key" in CONFIG else ""
        secret_key = input(f"Secret Key{msg}: ") or CONFIG["secret_key"]
    else:
        secret_id = CONFIG["secret_id"]
        secret_key = CONFIG["secret_key"]
    data = {"secret_id": secret_id, "secret_key": secret_key}

    async with sess.post(API + "token/new/", json=data) as resp:
        if resp.ok:
            d = await resp.json()
            CONFIG["secret_id"] = secret_id
            CONFIG["secret_key"] = secret_key
            CONFIG["token"] = d["refresh"]
            CONFIG_PATH.write_text(json.dumps(CONFIG, sort_keys=True, indent=4))
            sess.headers["Authorization"] = f"Bearer {d['access']}"
        else:
            print("Status:", resp.status)
            print(await resp.text())


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("-m", "--mode", type=Mode, default=Mode.transactions)
    args = parser.parse_args()

    f_map: dict[Mode, Callable[[ClientSession], Awaitable[None]]] = {
        Mode.register: register_account,
        Mode.token: fetch_token,
        Mode.transactions: import_transactions,
    }
    headers = {"Accept": "application/json"}
    async with ClientSession(headers=headers) as sess:  # TODO(3.11): base_url=API
        await f_map[args.mode](sess)


if __name__ == "__main__":
    asyncio.run(main())
