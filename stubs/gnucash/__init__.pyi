from datetime import date, datetime
from types import TracebackType
from typing import Literal, NewType, Self

_AccType = NewType("_AccType", int)

ACCT_TYPE_PAYABLE: _AccType
ACCT_TYPE_RECEIVABLE: _AccType
ACCT_TYPE_TRADING: _AccType

class GncNumeric:
    def __init__(self, num: float):
        ...

    def to_double(self) -> float:
        ...

class GncCommodity:
    ...

class GncCommodityTable:
    def lookup(self, commodity_type: Literal["CURRENCY"], symbol: str) -> GncCommodity:
        ...

class Account:
    def GetBalance(self) -> GncNumeric:
        ...

    def GetSplitList(self) -> list[Split]:
        ...

    def GetType(self) -> _AccType:
        ...

    def lookup_by_full_name(self, name: str) -> Account | None:
        ...

class Book:
    def get_root_account(self) -> Account:
        ...

    def get_table(self) -> GncCommodityTable:
        ...

class Session:
    book: Book

    def __init__(self, path: str):
        ...

    def __enter__(self) -> Self:
        ...

    def __exit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None) -> None:
        ...

class Transaction:
    def __init__(self, book: Book):
        ...

    def BeginEdit(self) -> None:
        ...

    def CommitEdit(self) -> None:
        ...

    def GetDate(self) -> date:
        ...

    def GetDescription(self) -> str:
        ...

    def GetSplitList(self) -> list[Split]:
        ...

    def SetCurrency(self, curr: GncCommodity) -> None:
        ...

    def SetDate(self, day: int, month: int, year: int) -> None:
        ...

    def SetDescription(self, desc: str) -> None:
        ...

class Split:
    parent: Transaction

    def __init__(self, book: Book):
        ...

    def GetAccount(self) -> Account:
        ...

    def GetAmount(self) -> GncNumeric:
        ...

    def GetMemo(self) -> str:
        ...

    def GetValue(self) -> GncNumeric:
        ...

    def SetAccount(self, account: Account) -> None:
        ...

    def SetMemo(self, memo: str) -> None:
        ...

    def SetParent(self, parent: Transaction) -> None:
        ...

    def SetReconcile(self, state: Literal["y", "n", "c"]) -> None:
        ...

    def SetValue(self, value: GncNumeric) -> None:
        ...
