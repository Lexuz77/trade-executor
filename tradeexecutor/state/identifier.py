"""Asset and trading pair identifiers.

How executor internally knows how to connect trading pairs in data and in execution environment (on-chain).
"""
from dataclasses import dataclass
from typing import Optional

from dataclasses_json import dataclass_json
from eth_typing import HexAddress
from web3 import Web3

from tradeexecutor.state.types import JSONHexAddress


@dataclass_json
@dataclass
class AssetIdentifier:
    """Identify a blockchain asset for trade execution.

    As internal token_ids and pair_ids may be unstable, trading pairs and tokens are explicitly
    referred by their smart contract addresses when a strategy decision moves to the execution.
    We duplicate data here to make sure we have a persistent record that helps to diagnose the sisues.
    """

    #: See https://chainlist.org/
    chain_id: int

    #: Smart contract address of the asset
    address: JSONHexAddress

    token_symbol: str
    decimals: Optional[int] = None

    #: How this asset is referred in the internal database
    internal_id: Optional[int] = None

    #: Info page URL for this asset
    info_url: Optional[str] = None

    def __str__(self):
        return f"<{self.token_symbol} at {self.address}>"

    def __post_init__(self):
        assert type(self.address) == str, f"Got address {self.address} as {type(self.address)}"
        assert self.address.startswith("0x")
        assert type(self.chain_id) == int

    def get_identifier(self) -> str:
        """Assets are identified by their smart contract address."""
        return self.address.lower()

    @property
    def checksum_address(self) -> HexAddress:
        """Ethereum madness."""
        return Web3.toChecksumAddress(self.address)


@dataclass_json
@dataclass
class TradingPairIdentifier:
    base: AssetIdentifier
    quote: AssetIdentifier

    #: Smart contract address of the pool contract.
    pool_address: str

    #: How this asset is referred in the internal database
    internal_id: Optional[int] = None

    #: Info page URL for this trading pair e.g. with the price charts
    info_url: Optional[str] = None

    def __repr__(self):
        return f"<Pair {self.base.token_symbol}-{self.quote.token_symbol} at {self.pool_address}>"

    def get_identifier(self) -> str:
        """We use the smart contract pool address to uniquely identify trading positions.

        Ethereum address is lowercased, not checksummed.
        """
        return self.pool_address.lower()

    def get_human_description(self) -> str:
        return f"{self.base.token_symbol}-{self.quote.token_symbol}"

    #def get_trading_pair(self, pair_universe: PandasPairUniverse) -> DEXPair:
    #    """Reverse resolves the smart contract address to trading pair data in the current trading pair universe."""
    #    return pair_universe.get_pair_by_smart_contract(self.pool_address)