"""
Classy Portfolio extension for Fava.
"""

import datetime
import re
import sys
import traceback
from decimal import Decimal
from typing import Literal, Optional, Union

from beancount.core import amount, convert, prices
from beancount.core.data import iter_entry_dates
from beancount.core.number import ZERO
from fava.core import FilteredLedger
from fava.core.tree import Tree, TreeNode
from fava.ext import FavaExtensionBase
from fava.helpers import FavaAPIError
from flask import g


class AccountsDict(dict):
    pass


class DecimalPercent(Decimal):
    pass


class DecimalIncomeGainLoss(Decimal):
    pass


class DecimalPercentGainLoss(Decimal):
    pass


class FavaClassyPortfolio(FavaExtensionBase):
    """Fava Extension Report that prints out a portfolio list based
    on asset-class and asset-subclass metadata.
    """

    report_title = "Classy Portfolio"

    def load_report(self) -> None:
        self.commodity_dict = {
            entry.currency: entry for entry in self.ledger.all_entries_by_type.Commodity
        }

    def portfolio_accounts(self, begin=None, end=None) -> list:
        """An account tree based on matching regex patterns."""
        portfolios = []

        try:
            self.load_report()

            filt_ledger = FilteredLedger(self.ledger)
            if begin:
                tree = Tree(iter_entry_dates(filt_ledger.entries, begin, end))
            else:
                tree = filt_ledger.root_tree

            for option in self.config:
                opt_key = option[0]
                if opt_key == "account_name_pattern":
                    portfolio = self._account_name_pattern(tree, end, option[1])
                elif opt_key == "account_open_metadata_pattern":
                    portfolio = self._account_metadata_pattern(
                        tree, end, option[1][0], option[1][1]
                    )
                else:
                    exception = FavaAPIError("Classy Portfolio: Invalid option.")
                    raise (exception)

                portfolio = (
                    portfolio[0],  # title
                    portfolio[1],  # subtitle
                    (
                        insert_rowspans(portfolio[2][0], portfolio[2][1], True),
                        portfolio[2][1],
                    ),  # portfolio data
                )
                portfolios.append(portfolio)

        except Exception:
            traceback.print_exc(file=sys.stdout)

        return portfolios

    def _account_name_pattern(
        self, tree: Tree, date: datetime.date, pattern: str
    ) -> tuple[str, str, tuple]:
        """
        Returns portfolio info based on matching account name.

        Args:
            tree: Ledger root tree node.
            date: Date.
            pattern: Account name regex pattern.
        Return:
            Data structured for use with a querytable (types, rows).
        """
        title = pattern.capitalize()
        subtitle = "Account names matching: '" + pattern + "'"
        selected_accounts = []
        regexer = re.compile(pattern)
        for acct in tree.keys():
            if (regexer.match(acct) is not None) and (acct not in selected_accounts):
                selected_accounts.append(acct)

        selected_nodes = [tree[x] for x in selected_accounts]
        portfolio_data = self._portfolio_data(selected_nodes, date)
        return title, subtitle, portfolio_data

    def _account_metadata_pattern(
        self, tree: Tree, date: datetime.date, metadata_key: str, pattern: str
    ) -> tuple[str, str, tuple]:
        """
        Returns portfolio info based on matching account open metadata.

        Args:
            tree: Ledger root tree node.
            date: Date.
            metadata_key: Metadata key to match for in account open.
            pattern: Metadata value's regex pattern to match for.
        Return:
            Data structured for use with a querytable - (types, rows).
        """
        title = pattern.capitalize()
        subtitle = (
            "Accounts with '" + metadata_key + "' metadata matching: '" + pattern + "'"
        )
        selected_accounts = []
        regexer = re.compile(pattern)
        for entry in self.ledger.all_entries_by_type.Open:
            if (metadata_key in entry.meta) and (
                regexer.match(entry.meta[metadata_key]) is not None
            ):
                selected_accounts.append(entry.account)

        selected_nodes = [tree[x] for x in selected_accounts]
        portfolio_data = self._portfolio_data(selected_nodes, date)
        return title, subtitle, portfolio_data

    def _asset_info(
        self, node: TreeNode, date: datetime.date
    ) -> tuple[Decimal, Decimal, Decimal]:
        """
        Additional info on an asset (price, gain/loss)
        """
        account_cost_conv = self._convert_cost(node, date)
        account_cost = account_cost_conv.number

        account_balance_market_value_node = node.balance.reduce(
            convert.convert_position,
            self.operating_currency,
            g.ledger.price_map,
            datetime.date.today(),
        )
        account_balance_market_value = account_balance_market_value_node.get(
            self.operating_currency, ZERO
        )

        # Calculate unrealized gain/loss
        # (follow beancount convention that negative values are income)
        account_income_gain_loss_unrealized = (
            account_cost - account_balance_market_value
        )

        # Calculate unrealized gain/loss (percentage)
        account_gain_loss_unrealized_percentage = (
            (account_income_gain_loss_unrealized * Decimal(-1.0)) / account_cost
        ) * Decimal(100)

        return (
            account_balance_market_value,
            account_income_gain_loss_unrealized,
            account_gain_loss_unrealized_percentage,
        )

    def _account_latest_price(self, node: TreeNode) -> Optional[tuple]:
        # Get latest price date
        quote_price = list(node.balance.keys())[0]
        if quote_price[1] is None:
            latest_price = None
        else:
            base = quote_price[0]
            currency = quote_price[1][1]  # type: ignore
            latest_price = prices.get_latest_price(g.ledger.price_map, (currency, base))
        return latest_price

    def _convert_cost(self, node: TreeNode, date: datetime.date) -> amount.Amount:
        account_cost_node = node.balance.reduce(convert.get_cost)
        cur, num = list(account_cost_node.items())[0]
        amt = amount.Amount(num, cur)
        account_cost_amt = convert.convert_amount(
            amt, self.operating_currency, g.ledger.price_map, date
        )
        return account_cost_amt

    def _portfolio_data(
        self, nodes: list[TreeNode], date: datetime.date
    ) -> tuple[dict, list, list]:
        """
        Turn a portfolio of tree nodes into portfolio_table-style data,
        looking at account 'asset_class' and 'asset_subclass' data.

        Args:
            nodes: Account tree nodes.
            date: Date.
        Return:
            types: Tuples of column names and types as strings.
            rows: Dictionaries of row data by column names.
        """
        errors = []
        self.operating_currency = self.ledger.options["operating_currency"][0]

        types = [
            ("portfolio_total", str(Decimal)),
            ("asset_classes", str(dict)),
            ("portfolio_allocation", str(DecimalPercent)),
            ("asset_class_total", str(Decimal)),
            ("asset_subclasses", str(dict)),
            ("asset_class_allocation", str(DecimalPercent)),
            ("asset_subclass_total", str(Decimal)),
            ("accounts", str(AccountsDict)),
            ("asset_subclass_allocation", str(DecimalPercent)),
            ("balance_market_value", str(Decimal)),
            ("income_gain_loss", str(DecimalIncomeGainLoss)),
            ("gain_loss_percentage", str(DecimalPercentGainLoss)),
            ("latest_price_date", str(datetime.date)),
        ]

        portfolio_tree: dict = {"portfolio_total": ZERO, "asset_classes": {}}
        for node in nodes:
            if node.balance == {}:
                continue
            account_name = node.name
            commodity = node_commodity(node)
            if (commodity in self.commodity_dict) and (
                "asset-class" in self.commodity_dict[commodity].meta
            ):
                asset_class = self.commodity_dict[commodity].meta["asset-class"]
            else:
                asset_class = "noclass"

            if (commodity in self.commodity_dict) and (
                "asset-subclass" in self.commodity_dict[commodity].meta
            ):
                asset_subclass = self.commodity_dict[commodity].meta["asset-subclass"]
            else:
                asset_subclass = "nosubclass"

            if asset_class not in portfolio_tree["asset_classes"]:
                portfolio_tree["asset_classes"][asset_class] = {}
                portfolio_tree["asset_classes"][asset_class][
                    "portfolio_allocation"
                ] = ZERO
                portfolio_tree["asset_classes"][asset_class]["asset_class_total"] = ZERO
                portfolio_tree["asset_classes"][asset_class]["asset_subclasses"] = {}
            if (
                asset_subclass
                not in portfolio_tree["asset_classes"][asset_class]["asset_subclasses"]
            ):
                portfolio_tree["asset_classes"][asset_class]["asset_subclasses"][
                    asset_subclass
                ] = {}
                portfolio_tree["asset_classes"][asset_class]["asset_subclasses"][
                    asset_subclass
                ]["asset_subclass_total"] = ZERO
                portfolio_tree["asset_classes"][asset_class]["asset_subclasses"][
                    asset_subclass
                ]["portfolio_allocation"] = ZERO
                portfolio_tree["asset_classes"][asset_class]["asset_subclasses"][
                    asset_subclass
                ]["asset_subclass_asset_class_allocation"] = ZERO
                portfolio_tree["asset_classes"][asset_class]["asset_subclasses"][
                    asset_subclass
                ]["accounts"] = {}

            # Insert account-level balances and
            # Sum totals for later calculating allocation
            account_data = {}
            # Get balance market value at today's date, if possible.

            # Calculate cost
            account_cost_conv = self._convert_cost(node, date)
            account_cost_node = {account_cost_conv.currency: account_cost_conv.number}

            if self.operating_currency in account_cost_node:

                account_cost = account_cost_node[self.operating_currency]
                latest_price = self._account_latest_price(node)
                if latest_price is None or latest_price[0] is None:
                    latest_price_date = None
                    account_balance_market_value = account_cost
                    # assume there's no gain loss
                    account_data["balance_market_value"] = account_cost
                    account_data["income_gain_loss"] = None
                    account_data["gain_loss_percentage"] = None
                    account_data["latest_price_date"] = None
                else:
                    latest_price_date = latest_price[0]
                    (
                        account_balance_market_value,
                        account_income_gain_loss_unrealized,
                        account_gain_loss_unrealized_percentage,
                    ) = self._asset_info(node, date)

                    account_data["balance_market_value"] = account_balance_market_value
                    account_data[
                        "income_gain_loss"
                    ] = account_income_gain_loss_unrealized
                    account_data[
                        "gain_loss_percentage"
                    ] = account_gain_loss_unrealized_percentage
                    account_data["latest_price_date"] = latest_price_date

                portfolio_tree["asset_classes"][asset_class]["asset_subclasses"][
                    asset_subclass
                ]["accounts"][account_name] = account_data

                # Accumulate sums
                if account_balance_market_value:
                    portfolio_tree["portfolio_total"] += account_balance_market_value
                portfolio_tree["asset_classes"][asset_class][
                    "asset_class_total"
                ] += account_balance_market_value
                portfolio_tree["asset_classes"][asset_class]["asset_subclasses"][
                    asset_subclass
                ]["asset_subclass_total"] += account_balance_market_value

            elif len(account_cost_node) == 0:
                # Assume account is empty
                account_data["balance_market_value"] = ZERO
                account_data["income_gain_loss"] = ZERO
                account_data["gain_loss_percentage"] = ZERO
                account_data["latest_price_date"] = None
                portfolio_tree["asset_classes"][asset_class]["asset_subclasses"][
                    asset_subclass
                ]["accounts"][account_name] = account_data
            else:
                errors.append(
                    "account "
                    + account_name
                    + " has balances not in operating currency "
                    + self.operating_currency
                )

        # Now that account balances and totals are calculated,
        # Traverse and calculate portfolio-level info.
        for asset_class in portfolio_tree["asset_classes"]:
            asset_class_dict = portfolio_tree["asset_classes"][asset_class]

            asset_class_dict["portfolio_allocation"] = (
                ZERO
                if portfolio_tree["portfolio_total"] == ZERO
                else round(
                    (
                        asset_class_dict["asset_class_total"]
                        / portfolio_tree["portfolio_total"]
                    )
                    * 100,
                    2,
                )
            )

            for asset_subclass in asset_class_dict["asset_subclasses"]:
                asset_subclass_dict = asset_class_dict["asset_subclasses"][
                    asset_subclass
                ]

                asset_subclass_dict["portfolio_allocation"] = (
                    ZERO
                    if portfolio_tree["portfolio_total"] == ZERO
                    else round(
                        (
                            asset_subclass_dict["asset_subclass_total"]
                            / portfolio_tree["portfolio_total"]
                        )
                        * 100,
                        2,
                    )
                )

                asset_subclass_dict["asset_class_allocation"] = (
                    ZERO
                    if asset_class_dict["asset_class_total"] == ZERO
                    else round(
                        (
                            asset_subclass_dict["asset_subclass_total"]
                            / asset_class_dict["asset_class_total"]
                        )
                        * 100,
                        2,
                    )
                )

                for account in asset_subclass_dict["accounts"]:
                    account_dict = asset_subclass_dict["accounts"][account]

                    account_dict["portfolio_allocation"] = (
                        ZERO
                        if portfolio_tree["portfolio_total"] == ZERO
                        else round(
                            (
                                account_dict["balance_market_value"]
                                / portfolio_tree["portfolio_total"]
                            )
                            * 100,
                            2,
                        )
                    )

                    account_dict["asset_class_allocation"] = (
                        ZERO
                        if asset_class_dict["asset_class_total"] == ZERO
                        else round(
                            (
                                account_dict["balance_market_value"]
                                / asset_class_dict["asset_class_total"]
                            )
                            * 100,
                            2,
                        )
                    )

                    account_dict["asset_subclass_allocation"] = (
                        ZERO
                        if asset_subclass_dict["asset_subclass_total"] == ZERO
                        else round(
                            (
                                account_dict["balance_market_value"]
                                / asset_subclass_dict["asset_subclass_total"]
                            )
                            * 100,
                            2,
                        )
                    )

        return portfolio_tree, types, errors


def node_commodity(node: TreeNode) -> str:
    """
    Return the common 'commodity' in an account.
    Return 'mixed_commodities' if an account has multiple commodities.
    """
    if len(node.balance):
        currencies = [cost[0] for cost in list(node.balance.keys())]
        ref_currency = currencies[0]
        for currency in currencies:
            if currency != ref_currency:
                return "mixed_commodities"
        return ref_currency
    else:
        return ""


def insert_rowspans(data: dict, coltypes: list, isStart: bool) -> dict:
    new_data: dict[str, Union[dict, tuple]] = {}
    colcount = 0

    if isStart:
        # if starting, we start traversing the data by coltype
        for coltype in coltypes:
            if coltype[1] == "<class 'dict'>":
                coltype_key: Literal["portfolio_total", "asset_classes"] = coltype[0]
                # Recurse and call rowspans again
                new_data_inner = insert_rowspans(
                    data[coltype_key], coltypes[(colcount + 1) :], False
                )

                # Collect the results
                new_data[coltype[0]] = new_data_inner
                rowsum = 0
                for value in new_data_inner.values():
                    rowsum += value[1]["rowspan"]

                # append sum of columns to prior columns
                for i in list(range(0, colcount, 1)):
                    new_data[coltypes[i][0]] = (
                        new_data[coltypes[i][0]][0],
                        {"rowspan": rowsum},
                    )
                break

            else:
                # assume non-dict, row-span of 1 as placeholder
                new_data[coltype[0]] = (data[coltype[0]], {"rowspan": 1})

            colcount = colcount + 1

    else:
        # Assume start data is a (multi-key) dictionary and we need to go
        # through the keys

        # reformat data for dict to have rowspan data
        for key in data.keys():
            new_data[key] = (data[key], {"rowspan": 1})

        for coltype in coltypes:
            if (coltype[1] == "<class 'dict'>") or (
                coltype[1] == "<class 'fava_classy_portfolio.AccountsDict'>"
            ):
                # Return length of each key.
                for key in data.keys():
                    new_data_inner = insert_rowspans(
                        data[key][coltype[0]], coltypes[(colcount + 1) :], False
                    )
                    new_data[key][0][coltype[0]] = new_data_inner

                    rowsum = 0
                    for value in new_data_inner.values():
                        rowsum += value[1]["rowspan"]

                    # Backpropagate rowspans to earlier coltypes...
                    for i in list(range(0, colcount, 1)):
                        new_data[key][0][coltypes[i][0]] = (
                            new_data[key][0][coltypes[i][0]][0],
                            {"rowspan": rowsum},
                        )
                    # ...including the dictionary key
                    new_data[key] = (new_data[key][0], {"rowspan": rowsum})
                break

            else:
                # placeholder for each key
                for key in data.keys():
                    new_data[key][0][coltype[0]] = (
                        data[key][coltype[0]],
                        {"rowspan": 1},
                    )

            colcount = colcount + 1

    return new_data
