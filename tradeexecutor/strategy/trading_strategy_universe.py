"""Trading Strategy oracle data integration.

Define trading universe based on data from :py:mod:`tradingstrategy` and
market data feeds.

See :ref:`trading universe` for more information.
"""

import contextlib
import datetime
import textwrap
from abc import abstractmethod
from dataclasses import dataclass
import logging
from typing import List, Optional, Callable, Tuple, Set

import pandas as pd
import pyarrow

from tradeexecutor.backtest.data_preload import preload_data
from tradeexecutor.strategy.execution_context import ExecutionMode, ExecutionContext
from tradingstrategy.token import Token

from tradeexecutor.state.identifier import AssetIdentifier, TradingPairIdentifier
from tradeexecutor.strategy.universe_model import StrategyExecutionUniverse, UniverseModel, DataTooOld
from tradingstrategy.candle import GroupedCandleUniverse
from tradingstrategy.chain import ChainId
from tradingstrategy.client import Client
from tradingstrategy.exchange import ExchangeUniverse
from tradingstrategy.liquidity import GroupedLiquidityUniverse
from tradingstrategy.pair import DEXPair, PandasPairUniverse, PairType, resolve_pairs_based_on_ticker
from tradingstrategy.timebucket import TimeBucket
from tradingstrategy.universe import Universe
from tradingstrategy.utils.groupeduniverse import filter_for_pairs

logger = logging.getLogger(__name__)


class TradingUniverseIssue(Exception):
    """Raised in the case trading universe has some bad data etc. issues."""


@dataclass
class Dataset:
    """Contain raw loaded datasets."""

    #: Granularity of our OHLCV data
    time_bucket: TimeBucket

    #: All exchanges
    exchanges: ExchangeUniverse

    #: All trading pairs
    pairs: pd.DataFrame

    #: Candle data for all pairs
    candles: Optional[pd.DataFrame] = None

    #: All liquidity samples
    liquidity: Optional[pd.DataFrame] = None

    #: Granularity of backtesting OHLCV data
    backtest_stop_loss_time_bucket: Optional[TimeBucket] = None

    #: All candles in stop loss time bucket
    backtest_stop_loss_candles: Optional[pd.DataFrame] = None

    def __post_init__(self):
        """Check we got good data."""
        candles = self.candles
        if candles is not None:
            assert isinstance(candles, pd.DataFrame), f"Expected DataFrame, got {candles.__class__}"

        liquidity = self.liquidity
        if liquidity is not None:
            assert isinstance(liquidity, pd.DataFrame), f"Expected DataFrame, got {liquidity.__class__}"


@dataclass
class TradingStrategyUniverse(StrategyExecutionUniverse):
    """A trading executor trading universe that using data from TradingStrategy.ai data feeds."""

    #: Trading universe datasets
    universe: Optional[Universe] = None

    backtest_stop_loss_time_bucket: Optional[TimeBucket] = None

    backtest_stop_loss_candles: Optional[GroupedCandleUniverse] = None

    def validate(self):
        """Check that the created universe looks good.

        :raise TradingUniverseIssue:
            In the case of detected issues
        """
        if len(self.reserve_assets) != 1:
            raise TradingUniverseIssue(f"Only single reserve asset strategies supported for now, got {self.reserve_assets}")

        for a in self.reserve_assets:
            if a.decimals == 0:
                raise TradingUniverseIssue(f"Reserve asset lacks decimals {a}")

    @staticmethod
    def create_single_pair_universe(
        dataset: Dataset,
        chain_id: ChainId,
        exchange_slug: str,
        base_token: str,
        quote_token: str,
    ) -> "TradingStrategyUniverse":
        """Filters down the dataset for a single trading pair.

        This is ideal for strategies that only want to trade a single pair.

        :param reserve_currency:
            If set use this as a reserve currency,
            otherwise use quote_token.
        """

        # We only trade on Pancakeswap v2
        exchange_universe = dataset.exchanges
        exchange = exchange_universe.get_by_chain_and_slug(chain_id, exchange_slug)
        assert exchange, f"No exchange {exchange_slug} found on chain {chain_id.name}"

        # Create trading pair database
        pair_universe = PandasPairUniverse.create_single_pair_universe(
            dataset.pairs,
            exchange,
            base_token,
            quote_token,
        )

        # Get daily candles as Pandas DataFrame
        all_candles = dataset.candles
        filtered_candles = filter_for_pairs(all_candles, pair_universe.df)
        candle_universe = GroupedCandleUniverse(filtered_candles)

        # Get liquidity candles as Pandas Dataframe
        all_liquidity = dataset.liquidity
        filtered_liquidity = filter_for_pairs(all_liquidity, pair_universe.df)
        liquidity_universe = GroupedLiquidityUniverse(filtered_liquidity)

        pair = pair_universe.get_single()

        # We have only a single pair, so the reserve asset must be its quote token
        trading_pair_identifier = translate_trading_pair(pair)
        reserve_assets = [
            trading_pair_identifier.quote
        ]

        universe = Universe(
            time_bucket=dataset.time_bucket,
            chains={chain_id},
            pairs=pair_universe,
            exchanges={exchange},
            candles=candle_universe,
            liquidity=liquidity_universe,
        )

        return TradingStrategyUniverse(universe=universe, reserve_assets=reserve_assets)

    @staticmethod
    def create_limited_pair_universe(
        dataset: Dataset,
        chain_id: ChainId,
        exchange_slug: str,
        pairs: Set[Tuple[str, str]],
        reserve_asset_pair_ticker: Optional[Tuple[str, str]] = None) -> "TradingStrategyUniverse":
        """Filters down the dataset for couple trading pair.

        This is ideal for strategies that only want to trade few pairs,
        or a single pair using three-way trading on a single exchange.

        The university reserve currency is set to the quote token of the first pair.

        :param pairs:
            List of trading pairs as ticket tuples. E.g. `[ ("WBNB, "BUSD"), ("Cake", "WBNB") ]`

        :param reserve_asset_pair_ticker:
            Choose the quote token of this trading pair as a reserve asset.
            This must be given if there are several pairs (Python set order is unstable).

        """

        # We only trade on Pancakeswap v2
        exchange_universe = dataset.exchanges
        exchange = exchange_universe.get_by_chain_and_slug(chain_id, exchange_slug)
        assert exchange, f"No exchange {exchange_slug} found on chain {chain_id.name}"

        # Create trading pair database
        pair_universe = PandasPairUniverse.create_limited_pair_universe(
            dataset.pairs,
            exchange,
            pairs,
        )

        # Get daily candles as Pandas DataFrame
        all_candles = dataset.candles
        filtered_candles = filter_for_pairs(all_candles, pair_universe.df)
        candle_universe = GroupedCandleUniverse(filtered_candles)

        # Get liquidity candles as Pandas Dataframe
        if dataset.liquidity:
            all_liquidity = dataset.liquidity
            filtered_liquidity = filter_for_pairs(all_liquidity, pair_universe.df)
            liquidity_universe = GroupedLiquidityUniverse(filtered_liquidity)
        else:
            # Liquidity data loading skipped
            liquidity_universe = None

        if reserve_asset_pair_ticker:
            reserve_pair = pair_universe.get_one_pair_from_pandas_universe(
                exchange.exchange_id,
                reserve_asset_pair_ticker[0],
                reserve_asset_pair_ticker[1],
            )
        else:
            assert len(pairs) == 1, "Cannot automatically determine reserve asset if there are multiple trading pairs."
            first_ticker = next(iter(pairs))
            reserve_pair = pair_universe.get_one_pair_from_pandas_universe(
                exchange.exchange_id,
                first_ticker[0],
                first_ticker[1],
            )
        # We have only a single pair, so the reserve asset must be its quote token
        trading_pair_identifier = translate_trading_pair(reserve_pair)
        reserve_assets = [
            trading_pair_identifier.quote
        ]

        universe = Universe(
            time_bucket=dataset.time_bucket,
            chains={chain_id},
            pairs=pair_universe,
            exchanges={exchange},
            candles=candle_universe,
            liquidity=liquidity_universe,
        )

        return TradingStrategyUniverse(universe=universe, reserve_assets=reserve_assets)

    def get_pair_by_address(self, address: str) -> Optional[TradingPairIdentifier]:
        """Get a trading pair data by a smart contract address."""
        pair = self.universe.pairs.get_pair_by_smart_contract(address)
        if not pair:
            return None
        return translate_trading_pair(pair)


class TradingStrategyUniverseModel(UniverseModel):
    """A universe constructor that builds the trading universe data using Trading Strategy client.

    On a live exeuction, trade universe is reconstructor for the every tick,
    by refreshing the trading data from the server.
    """

    def __init__(self, client: Client, timed_task_context_manager: contextlib.AbstractContextManager):
        self.client = client
        self.timed_task_context_manager = timed_task_context_manager

    def log_universe(self, universe: Universe):
        """Log the state of the current universe.]"""
        data_start, data_end = universe.candles.get_timestamp_range()

        if universe.liquidity:
            liquidity_start, liquidity_end = universe.liquidity.get_timestamp_range()
        else:
            liquidity_start = liquidity_end = None

        logger.info(textwrap.dedent(f"""
                Universe constructed.                    
                
                Time periods
                - Time frame {universe.time_bucket.value}
                - Candle data range: {data_start} - {data_end}
                - Liquidity data range: {liquidity_start} - {liquidity_end}
                
                The size of our trading universe is
                - {len(universe.exchanges):,} exchanges
                - {universe.pairs.get_count():,} pairs
                - {universe.candles.get_sample_count():,} candles
                - {universe.liquidity.get_sample_count():,} liquidity samples                
                """))
        return universe

    def load_data(self,
                  time_frame: TimeBucket,
                  mode: ExecutionMode,
                  backtest_stop_loss_time_frame: Optional[TimeBucket]=None) -> Dataset:
        """Loads the server-side data using the client.

        :param client:
            Client instance. Note that this cannot be stable across ticks, as e.g.
            API keys can change. Client is recreated for every tick.

        :param mode:
            Live trading or vacktesting

        :param backtest_stop_loss_time_frame:
            Load more granular data for backtesting stop loss

        :return:
            None if not dataset for the strategy required
        """

        assert isinstance(mode, ExecutionMode), f"Expected ExecutionMode, got {mode}"

        client = self.client

        with self.timed_task_context_manager("load_data", time_bucket=time_frame.value):

            if mode.is_live_trading():
                # This will force client to redownload the data
                logger.info("Purging trading data caches")
                client.clear_caches()
            else:
                logger.info("Using cached data if available")

            exchanges = client.fetch_exchange_universe()
            pairs = client.fetch_pair_universe().to_pandas()
            candles = client.fetch_all_candles(time_frame).to_pandas()
            liquidity = client.fetch_all_liquidity_samples(time_frame).to_pandas()

            if backtest_stop_loss_time_frame:
                backtest_stop_loss_candles = client.fetch_all_candles(backtest_stop_loss_time_frame).to_pandas()
            else:
                backtest_stop_loss_candles = None

            return Dataset(
                time_bucket=time_frame,
                backtest_stop_loss_time_bucket=backtest_stop_loss_time_frame,
                exchanges=exchanges,
                pairs=pairs,
                candles=candles,
                liquidity=liquidity,
                backtest_stop_loss_candles=backtest_stop_loss_candles,
            )

    def check_data_age(self, ts: datetime.datetime, universe: TradingStrategyUniverse, best_before_duration: datetime.timedelta):
        """Check if our data is up-to-date and we do not have issues with feeds.

        Ensure we do not try to execute live trades with stale data.

        :raise DataTooOld: in the case data is too old to execute.
        """
        max_age = ts - best_before_duration
        universe = universe.universe

        if universe.candles is not None:
            # Convert pandas.Timestamp to executor internal datetime format
            candle_start, candle_end = universe.candles.get_timestamp_range()
            candle_start = candle_start.to_pydatetime().replace(tzinfo=None)
            candle_end = candle_end.to_pydatetime().replace(tzinfo=None)

            if candle_end < max_age:
                diff = max_age - candle_end
                raise DataTooOld(f"Candle data {candle_start} - {candle_end} is too old to work with, we require threshold {max_age}, diff is {diff}")

        if universe.liquidity is not None:
            liquidity_start, liquidity_end = universe.liquidity.get_timestamp_range()
            liquidity_start = liquidity_start.to_pydatetime().replace(tzinfo=None)
            liquidity_end = liquidity_end.to_pydatetime().replace(tzinfo=None)

            if liquidity_end < max_age:
                raise DataTooOld(f"Liquidity data is too old to work with {liquidity_start} - {liquidity_end}")

    @staticmethod
    def create_from_dataset(dataset: Dataset, chains: List[ChainId], reserve_assets: List[AssetIdentifier], pairs_index=True):
        """Create an trading universe from dataset with zero filtering for the data."""

        exchanges = list(dataset.exchanges.exchanges.values())
        logger.debug("Preparing pairs")
        pairs = PandasPairUniverse(dataset.pairs, build_index=pairs_index)
        logger.debug("Preparing candles")
        candle_universe = GroupedCandleUniverse(dataset.candles)
        logger.debug("Preparing liquidity")
        liquidity_universe = GroupedLiquidityUniverse(dataset.liquidity)

        logger.debug("Preparing backtest stop loss data")
        if dataset.backtest_stop_loss_candles:
            backtest_stop_loss_candles = GroupedCandleUniverse(dataset.backtest_stop_loss_candles)
        else:
            backtest_stop_loss_candles = None

        universe = Universe(
            time_bucket=dataset.time_bucket,
            chains=chains,
            pairs=pairs,
            exchanges=exchanges,
            candles=candle_universe,
            liquidity=liquidity_universe,
        )

        logger.debug("Universe created")
        return TradingStrategyUniverse(
            universe=universe,
            reserve_assets=reserve_assets,
            backtest_stop_loss_candles=backtest_stop_loss_candles,
            backtest_stop_loss_time_bucket=dataset.backtest_stop_loss_time_bucket)

    @abstractmethod
    def construct_universe(self, ts: datetime.datetime, live: bool) -> TradingStrategyUniverse:
        pass


class DefaultTradingStrategyUniverseModel(TradingStrategyUniverseModel):
    """Shortcut for simple strategies.

    - Assumes we have a strategy that fits to :py:mod:`tradeexecutor.strategy_module` definiton

    - At the start of the backtests or at each cycle of live trading, call
      the `create_trading_universe` callback of the strategy

    - Validate the output of the function
    """

    def __init__(self,
                 client: Optional[Client],
                 execution_context: ExecutionContext,
                 create_trading_universe: Callable,
                 candle_time_frame_override: Optional[TimeBucket] = None,
                 ):
        assert isinstance(client, Client) or client is None
        assert isinstance(execution_context, ExecutionContext), f"Got {execution_context}"
        assert isinstance(create_trading_universe, Callable), f"Got {create_trading_universe}"
        self.client = client
        self.execution_context = execution_context
        self.create_trading_universe = create_trading_universe
        self.candle_time_frame_override = candle_time_frame_override

    def preload_universe(self):
        """Triggered before backtesting execution.

        - Load all datasets with progress bar display.

        - Not triggered in live trading, as universe changes between cycles
        """
        with self.execution_context.timed_task_context_manager(task_name="preload_universe"):
            preload_data(
                self.client,
                self.create_trading_universe,
                candle_time_frame_override=self.candle_time_frame_override)

    def construct_universe(self, ts: datetime.datetime, mode: ExecutionMode) -> TradingStrategyUniverse:
        with self.execution_context.timed_task_context_manager(task_name="create_trading_universe"):
            universe = self.create_trading_universe(ts, self.client, self.execution_context, candle_time_frame_override=self.candle_time_frame_override)
            assert isinstance(universe, TradingStrategyUniverse), f"Expected TradingStrategyUniverse, got {universe.__class__}"
            universe.validate()
            return universe


def translate_token(token: Token) -> AssetIdentifier:
    """Translate Trading Strategy token data definition to trade executor.

    Trading Strategy client uses compressed columnar data for pairs and tokens.

    Creates `AssetIdentifier` based on data coming from
    Trading Strategy :py:class:`tradingstrategy.pair.PandasPairUniverse`.
    """
    return AssetIdentifier(
        token.chain_id.value,
        token.address,
        token.symbol,
        token.decimals
    )


def translate_trading_pair(pair: DEXPair) -> TradingPairIdentifier:
    """Translate trading pair from client download to the trade executor.

    Trading Strategy client uses compressed columnar data for pairs and tokens.

    Translates a trading pair presentation from Trading Strategy client Pandas format to the trade executor format.

    Trade executor work with multiple different strategies, not just Trading Strategy client based.
    For example, you could have a completely on-chain data based strategy.
    Thus, Trade Executor has its internal asset format.

    This module contains functions to translate asset presentations between Trading Strategy client
    and Trade Executor.


    This is called when a trade is made: this is the moment when trade executor data format must be made available.
    """

    assert isinstance(pair, DEXPair), f"Expected DEXPair, got {type(pair)}"
    assert pair.base_token_decimals is not None, f"Base token missing decimals: {pair}"
    assert pair.quote_token_decimals is not None, f"Quote token missing decimals: {pair}"

    base = AssetIdentifier(
        chain_id=pair.chain_id.value,
        address=pair.base_token_address,
        token_symbol=pair.base_token_symbol,
        decimals=pair.base_token_decimals,
    )
    quote = AssetIdentifier(
        chain_id=pair.chain_id.value,
        address=pair.quote_token_address,
        token_symbol=pair.quote_token_symbol,
        decimals=pair.quote_token_decimals,
    )

    return TradingPairIdentifier(
        base=base,
        quote=quote,
        pool_address=pair.address,
        internal_id=pair.pair_id,
        info_url=pair.get_trading_pair_page_url(),
        exchange_address=pair.exchange_address,
    )


def create_pair_universe_from_code(chain_id: ChainId, pairs: List[TradingPairIdentifier]) -> "PandasPairUniverse":
    """Create the trading universe from handcrafted data.

    Used in unit testing.
    """
    data = []
    for idx, p in enumerate(pairs):
        assert p.base.decimals
        assert p.quote.decimals
        assert p.internal_exchange_id, f"All trading pairs must have internal_exchange_id set, did not have it set {p}"
        assert p.internal_id
        dex_pair = DEXPair(
            pair_id=p.internal_id,
            chain_id=chain_id,
            exchange_id=p.internal_exchange_id,
            address=p.pool_address,
            exchange_address=p.exchange_address,
            dex_type=PairType.uniswap_v2,
            base_token_symbol=p.base.token_symbol,
            quote_token_symbol=p.quote.token_symbol,
            token0_symbol=p.base.token_symbol,
            token1_symbol=p.quote.token_symbol,
            token0_address=p.base.address,
            token1_address=p.quote.address,
            token0_decimals=p.base.decimals,
            token1_decimals=p.quote.decimals,
        )
        data.append(dex_pair.to_dict())
    df = pd.DataFrame(data)
    return PandasPairUniverse(df)


def load_all_data(client: Client, time_frame: TimeBucket, execution_context: ExecutionContext) -> Dataset:
    """Load all pair, candle and liquidity data for a given time bucket.

    - Backtest data is never reloaded

    - Live trading purges old data fields and reloads data

    :param client:
        Trading Strategy client instance

    :param time_frame:
        Candle time bucket to load

    :param execution_context:
        Defines if we are live or backtesting
    """

    assert isinstance(client, Client)
    assert isinstance(time_frame, TimeBucket)
    assert isinstance(execution_context, ExecutionContext)

    live = execution_context.live_trading
    with execution_context.timed_task_context_manager("load_data", time_bucket=time_frame.value):
        if live:
            # This will force client to redownload the data
            logger.info("Purging trading data caches")
            client.clear_caches()
        else:
            logger.info("Using cached data if available")

        exchanges = client.fetch_exchange_universe()
        pairs = client.fetch_pair_universe().to_pandas()
        candles = client.fetch_all_candles(time_frame).to_pandas()
        liquidity = client.fetch_all_liquidity_samples(time_frame).to_pandas()
        return Dataset(
            time_bucket=time_frame,
            exchanges=exchanges,
            pairs=pairs,
            candles=candles,
            liquidity=liquidity,
        )


def load_pair_data_for_single_exchange(
        client: Client,
        execution_context: ExecutionContext,
        time_bucket: TimeBucket,
        chain_id: ChainId,
        exchange_slug: str,
        pair_tickers: Set[Tuple[str, str]],
        liquidity=False,
) -> Dataset:
    """Load pair data for a single decentralised exchange.

    If you are not trading the full trading universe,
    this function does a much smaller dataset download than
    :py:func:`load_all_data`.

    - This function uses optimised JSONL loading
      via :py:meth:`~tradingstrategy.client.Client.fetch_candles_by_pair_ids`.

    - Backtest data is never reloaded.
      Furthermore, the data is stored in :py:class:`Client`
      disk cache for the subsequent notebook and backtest runs.

    - Live trading purges old data fields and reloads data

    :param client:
        Trading Strategy client instance

    :param time_bucket:
        The candle time frame

    :param chain_id:
        Which blockchain hosts our exchange

    :param exchange_slug:
        Which exchange hosts our trading pairs

    :param exchange_slug:
        Which exchange hosts our trading pairs

    :param pair_tickers:
        List of trading pair tickers as base token quote token tuples.
        E.g. `[('WBNB', 'BUSD'), ('Cake', 'BUSD')]`.

    :param liquidity:
        Set true to load liquidity data as well

    :param execution_context:
        Defines if we are live or backtesting
    """

    assert isinstance(client, Client)
    assert isinstance(time_bucket, TimeBucket)
    assert isinstance(execution_context, ExecutionContext)
    assert isinstance(chain_id, ChainId)
    assert isinstance(exchange_slug, str)

    live = execution_context.live_trading
    with execution_context.timed_task_context_manager("load_pair_data_for_single_exchange", time_bucket=time_bucket.value):
        if live:
            # This will force client to redownload the data
            logger.info("Purging trading data caches")
            client.clear_caches()
        else:
            logger.info("Using cached data if available")

        exchanges = client.fetch_exchange_universe()
        pairs_df = client.fetch_pair_universe().to_pandas()

        # Resolve full pd.Series for each pair
        # we are interested in
        our_pairs = resolve_pairs_based_on_ticker(
            pairs_df,
            chain_id,
            exchange_slug,
            pair_tickers
        )

        assert len(our_pairs) > 0, f"Pair data not found {chain_id.name}, {exchange_slug}, {pair_tickers}"

        assert len(our_pairs) == len(pair_tickers), f"Pair resolution failed. Wanted to have {len(pair_tickers)} pairs, but after pair id resolution ended up with {len(our_pairs)} pairs"

        our_pair_ids = set(our_pairs["pair_id"])

        if len(our_pair_ids) > 1:
            desc = f"Loading OHLCV data for {exchange_slug}"
        else:
            only, _ = pair_tickers
            desc = f"Loading OHLCV data for {only[0]}-{only[1]}"

        candles = client.fetch_candles_by_pair_ids(
            our_pair_ids,
            time_bucket,
            progress_bar_description=desc,
        )

        if liquidity:
            raise NotImplemented("Partial liquidity data loading is not yet supported")

        return Dataset(
            time_bucket=time_bucket,
            exchanges=exchanges,
            pairs=our_pairs,
            candles=candles,
            liquidity=None
        )
