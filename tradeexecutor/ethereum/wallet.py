import logging
import dataclasses
import datetime
from decimal import Decimal
from typing import Dict, List, Tuple, Union

from dataclasses_json import dataclass_json
from eth_typing import HexAddress
from web3 import Web3

from eth_hentai.balances import DecimalisedHolding, \
    fetch_erc20_balances_by_token_list, convert_balances_to_decimal
from tradeexecutor.state.state import Portfolio, AssetIdentifier, ReservePosition


logger = logging.getLogger(__name__)


@dataclass_json
@dataclasses.dataclass
class ReserveUpdateEvent:
    asset: AssetIdentifier
    updated_at: datetime.datetime
    past_balance: Decimal
    new_balance: Decimal


def update_wallet_balances(web3: Web3, address: HexAddress, tokens: List[HexAddress]) -> Dict[HexAddress, DecimalisedHolding]:
    """Get raw balances of ERC-20 tokens."""
    balances = fetch_erc20_balances_by_token_list(web3, address, tokens)
    return convert_balances_to_decimal(web3, balances)


def sync_reserves(
        web3: Web3,
        clock: datetime.datetime,
        wallet_address: HexAddress,
        current_reserves: List[ReservePosition],
        supported_reserve_currencies: List[AssetIdentifier]) -> List[ReserveUpdateEvent]:
    """Check the address for any incoming stablecoin transfers to see how much cash we have."""

    our_chain_id = web3.eth.chain_id

    # Get raw ERC-20 holdings of the address
    balances = update_wallet_balances(web3, wallet_address, [web3.toChecksumAddress(a.address) for a in supported_reserve_currencies])

    reserves_per_token = {r.asset.address: r for r in current_reserves}

    events: ReserveUpdateEvent = []

    for currency in supported_reserve_currencies:

        address = web3.toChecksumAddress(currency.address)

        # 1337 is Ganache
        if our_chain_id != 1337:
            assert currency.chain_id == our_chain_id, f"Asset expects chain_id {currency.chain_id}, currently connected to {our_chain_id}"

        if currency.address in reserves_per_token:
            # We have an existing record of having this reserve
            current_value = reserves_per_token[address].quantity
        else:
            current_value = Decimal(0)

        decimal_holding = balances.get(address)

        # We get decimals = None if Ganache is acting
        assert decimal_holding.decimals, f"Token did not have decimals: token:{currency} holding:{decimal_holding}"

        if (decimal_holding is not None) and (decimal_holding.value != current_value):
            evt = ReserveUpdateEvent(
                asset=currency,
                past_balance=current_value,
                new_balance=decimal_holding.value,
                updated_at=clock
            )
            events.append(evt)
            logger.info("Reserve currency update detected. Asset: %s, past: %s, new: %s", evt.asset, evt.past_balance, evt.new_balance)

    return events


def sync_portfolio(portfolio: Portfolio, new_reserves: List[ReserveUpdateEvent], default_price=1.0):
    """Update reserves in the portfolio.

    :param default_price: Set the reserve currency price for new reserves.
    """

    for evt in new_reserves:

        res_pos = portfolio.reserves.get(evt.asset.get_identifier())
        if res_pos is not None:
            # Update existing
            res_pos.quantity = evt.new_balance
            res_pos.last_sync_at = evt.updated_at
            logger.info("Portfolio reserve synced. Asset: %s", evt.asset)
        else:
            # Set new
            res_pos = ReservePosition(
                asset=evt.asset,
                quantity=evt.new_balance,
                last_sync_at=evt.updated_at,
                reserve_token_price=default_price,
                last_pricing_at=evt.updated_at,
            )
            portfolio.reserves[res_pos.get_identifier()] = res_pos
            logger.info("Portfolio reserve created. Asset: %s", evt.asset)


