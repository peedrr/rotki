import copy
import logging
import random
from collections import defaultdict
from typing import DefaultDict, List, NamedTuple, Set, Tuple

import gevent

from rotkehlchen.assets.asset import Asset
from rotkehlchen.chain.bitcoin.xpub import XpubManager
from rotkehlchen.chain.ethereum.transactions import EthTransactions
from rotkehlchen.chain.manager import ChainManager
from rotkehlchen.constants.assets import A_USD
from rotkehlchen.db.dbhandler import DBHandler
from rotkehlchen.db.ethtx import DBEthTx
from rotkehlchen.db.filtering import DBIgnoreValuesFilter, DBStringFilter, HistoryEventFilterQuery
from rotkehlchen.db.history_events import DBHistoryEvents
from rotkehlchen.errors import NoPriceForGivenTimestamp, RemoteError
from rotkehlchen.exchanges.manager import ExchangeManager
from rotkehlchen.externalapis.cryptocompare import Cryptocompare
from rotkehlchen.fval import FVal
from rotkehlchen.globaldb.handler import GlobalDBHandler
from rotkehlchen.greenlets import GreenletManager
from rotkehlchen.history.price import PriceHistorian
from rotkehlchen.history.typing import HistoricalPriceOracle
from rotkehlchen.logging import RotkehlchenLogsAdapter
from rotkehlchen.premium.sync import PremiumSyncManager
from rotkehlchen.typing import ChecksumEthAddress, Location, Timestamp
from rotkehlchen.utils.misc import ts_now

logger = logging.getLogger(__name__)
log = RotkehlchenLogsAdapter(logger)


CRYPTOCOMPARE_QUERY_AFTER_SECS = 86400  # a day
DEFAULT_MAX_TASKS_NUM = 2
CRYPTOCOMPARE_HISTOHOUR_FREQUENCY = 240  # at least 4 mins apart
XPUB_DERIVATION_FREQUENCY = 3600  # every hour
ETH_TX_QUERY_FREQUENCY = 3600  # every hour
EXCHANGE_QUERY_FREQUENCY = 3600  # every hour


def noop_exchange_succes_cb(trades, margin, asset_movements, ledger_actions, exchange_specific_data) -> None:  # type: ignore # noqa: E501
    pass


def exchange_fail_cb(error: str) -> None:
    log.error(error)


class CCHistoQuery(NamedTuple):
    from_asset: Asset
    to_asset: Asset


class TaskManager():

    def __init__(
            self,
            max_tasks_num: int,
            greenlet_manager: GreenletManager,
            api_task_greenlets: List[gevent.Greenlet],
            database: DBHandler,
            cryptocompare: Cryptocompare,
            premium_sync_manager: PremiumSyncManager,
            chain_manager: ChainManager,
            exchange_manager: ExchangeManager,
    ) -> None:
        self.max_tasks_num = max_tasks_num
        self.greenlet_manager = greenlet_manager
        self.api_task_greenlets = api_task_greenlets
        self.database = database
        self.cryptocompare = cryptocompare
        self.exchange_manager = exchange_manager
        self.premium_sync_manager = premium_sync_manager
        self.cryptocompare_queries: Set[CCHistoQuery] = set()
        self.chain_manager = chain_manager
        self.last_xpub_derivation_ts = 0
        self.last_eth_tx_query_ts: DefaultDict[ChecksumEthAddress, int] = defaultdict(int)
        self.last_exchange_query_ts: DefaultDict[Tuple[str, Location], int] = defaultdict(int)
        self.base_entries_ignore_set: Set[str] = set()
        self.prepared_cryptocompare_query = False
        self.greenlet_manager.spawn_and_track(  # Needs to run in greenlet, is slow
            after_seconds=None,
            task_name='Prepare cryptocompare queries',
            exception_is_error=True,
            method=self._prepare_cryptocompare_queries,
        )

        self.potential_tasks = [
            self._maybe_schedule_cryptocompare_query,
            self.premium_sync_manager.maybe_upload_data_to_server,
            self._maybe_schedule_xpub_derivation,
            self._maybe_query_ethereum_transactions,
            self._maybe_schedule_exchange_history_query,
            self._maybe_schedule_ethereum_txreceipts,
            self._maybe_query_missing_prices,
        ]
        self.schedule_lock = gevent.lock.Semaphore()

    def _prepare_cryptocompare_queries(self) -> None:
        """Prepare the queries to do to cryptocompare

        This would be really slow if the entire json cache files were read but we
        have implemented get_cached_data_metadata to only read the relevant part of the file.
        Before doing that we had to yield with gevent.sleep() at each loop iteration.

        Runs only once in the beginning and then has a number of queries prepared
        for the task manager to schedule
        """
        now_ts = ts_now()
        if self.prepared_cryptocompare_query is True:
            return

        if len(self.cryptocompare_queries) != 0:
            return

        assets = self.database.query_owned_assets()
        main_currency = self.database.get_main_currency()
        for asset in assets:

            if asset.is_fiat() and main_currency.is_fiat():
                continue  # ignore fiat to fiat

            if asset.cryptocompare == '' or main_currency.cryptocompare == '':
                continue  # not supported in cryptocompare

            if asset.cryptocompare is None and asset.symbol is None:
                continue  # type: ignore  # asset.symbol may be None for auto generated underlying tokens # noqa: E501

            data_range = GlobalDBHandler().get_historical_price_range(
                from_asset=asset,
                to_asset=main_currency,
                source=HistoricalPriceOracle.CRYPTOCOMPARE,
            )
            if data_range is not None and now_ts - data_range[1] < CRYPTOCOMPARE_QUERY_AFTER_SECS:
                continue

            self.cryptocompare_queries.add(CCHistoQuery(from_asset=asset, to_asset=main_currency))

        self.prepared_cryptocompare_query = True

    def _maybe_schedule_cryptocompare_query(self) -> bool:
        """Schedules a cryptocompare query for a single asset history"""
        if self.prepared_cryptocompare_query is False:
            return False

        if len(self.cryptocompare_queries) == 0:
            return False

        # If there is already a cryptocompary query running don't schedule another
        if any(
                'Cryptocompare historical prices' in x.task_name
                for x in self.greenlet_manager.greenlets
        ):
            return False

        now_ts = ts_now()
        # Make sure there is a long enough period  between an asset's histohour query
        # to avoid getting rate limited by cryptocompare
        if now_ts - self.cryptocompare.last_histohour_query_ts <= CRYPTOCOMPARE_HISTOHOUR_FREQUENCY:  # noqa: E501
            return False

        query = self.cryptocompare_queries.pop()
        task_name = f'Cryptocompare historical prices {query.from_asset} / {query.to_asset} query'
        log.debug(f'Scheduling task for {task_name}')
        self.greenlet_manager.spawn_and_track(
            after_seconds=None,
            task_name=task_name,
            exception_is_error=False,
            method=self.cryptocompare.query_and_store_historical_data,
            from_asset=query.from_asset,
            to_asset=query.to_asset,
            timestamp=now_ts,
        )
        return True

    def _maybe_schedule_xpub_derivation(self) -> None:
        """Schedules the xpub derivation task if enough time has passed and if user has xpubs"""
        now = ts_now()
        if now - self.last_xpub_derivation_ts <= XPUB_DERIVATION_FREQUENCY:
            return

        xpubs = self.database.get_bitcoin_xpub_data()
        if len(xpubs) == 0:
            return

        log.debug('Scheduling task for Xpub derivation')
        self.greenlet_manager.spawn_and_track(
            after_seconds=None,
            task_name='Derive new xpub addresses',
            exception_is_error=True,
            method=XpubManager(self.chain_manager).check_for_new_xpub_addresses,
        )
        self.last_xpub_derivation_ts = now

    def _maybe_query_ethereum_transactions(self) -> None:
        """Schedules the ethereum transaction query task if enough time has passed"""
        accounts = self.database.get_blockchain_accounts().eth
        if len(accounts) == 0:
            return

        now = ts_now()
        queriable_accounts = []
        for x in accounts:
            queried_range = self.database.get_used_query_range(f'ethtxs_{x}')
            end_ts = queried_range[1] if queried_range else 0
            if now - max(self.last_eth_tx_query_ts[x], end_ts) > ETH_TX_QUERY_FREQUENCY:
                queriable_accounts.append(x)
        if len(queriable_accounts) == 0:
            return

        tx_module = EthTransactions(
            ethereum=self.chain_manager.ethereum,
            database=self.database,
        )
        address = random.choice(queriable_accounts)
        task_name = f'Query ethereum transactions for {address}'
        log.debug(f'Scheduling task to {task_name}')
        self.greenlet_manager.spawn_and_track(
            after_seconds=None,
            task_name=task_name,
            exception_is_error=True,
            method=tx_module.single_address_query_transactions,
            address=address,
            start_ts=0,
            end_ts=now,
        )
        self.last_eth_tx_query_ts[address] = now

    def _run_ethereum_txreceipts_query(self, hash_results: List[Tuple]) -> None:
        dbethtx = DBEthTx(self.database)
        for entry in hash_results:
            tx_hash = '0x' + entry[0].hex()
            tx_receipt_data = self.chain_manager.ethereum.get_transaction_receipt(tx_hash=tx_hash)
            dbethtx.add_receipt_data(tx_receipt_data)

    def _maybe_schedule_ethereum_txreceipts(self) -> None:
        """Schedules the ethereum transaction receipts query task"""
        cursor = self.database.conn.cursor()
        result = cursor.execute(
            'SELECT tx_hash from ethereum_transactions WHERE tx_hash NOT IN '
            '(SELECT tx_hash from ethtx_receipts) LIMIT 100;',
        ).fetchall()
        if len(result) == 0:
            return

        task_name = f'Query {len(result)} ethereum transactions receipts'
        log.debug(f'Scheduling task to {task_name}')
        self.greenlet_manager.spawn_and_track(
            after_seconds=None,
            task_name=task_name,
            exception_is_error=True,
            method=self._run_ethereum_txreceipts_query,
            hash_results=result,
        )

    def _maybe_schedule_exchange_history_query(self) -> None:
        """Schedules the exchange history query task if enough time has passed"""
        if len(self.exchange_manager.connected_exchanges) == 0:
            return

        now = ts_now()
        queriable_exchanges = []
        for exchange in self.exchange_manager.iterate_exchanges():
            if exchange.location in (Location.BINANCE, Location.BINANCEUS):
                continue  # skip binance due to the way their history is queried and rate limiting
            queried_range = self.database.get_used_query_range(f'{str(exchange.location)}_trades')
            end_ts = queried_range[1] if queried_range else 0
            if now - max(self.last_exchange_query_ts[exchange.location_id()], end_ts) > EXCHANGE_QUERY_FREQUENCY:  # noqa: E501
                queriable_exchanges.append(exchange)

        if len(queriable_exchanges) == 0:
            return

        exchange = random.choice(queriable_exchanges)
        task_name = f'Query history of {exchange.name} exchange'
        log.debug(f'Scheduling task to {task_name}')
        self.greenlet_manager.spawn_and_track(
            after_seconds=None,
            task_name=task_name,
            exception_is_error=True,
            method=exchange.query_history_with_callbacks,
            start_ts=0,
            end_ts=now,
            success_callback=noop_exchange_succes_cb,
            fail_callback=exchange_fail_cb,
        )
        self.last_exchange_query_ts[exchange.location_id()] = now

    def _maybe_query_missing_prices(self) -> None:
        query_filter = HistoryEventFilterQuery.make(limit=100)
        entries = self.get_base_entries_missing_prices(query_filter)
        if len(entries) > 0:
            task_name = 'Periodically query history events prices'
            log.debug(f'Scheduling task to {task_name}')
            self.greenlet_manager.spawn_and_track(
                after_seconds=None,
                task_name=task_name,
                exception_is_error=True,
                method=self.query_missing_prices_of_base_entries,
                entries_missing_prices=entries,
            )

    def get_base_entries_missing_prices(
        self,
        query_filter: HistoryEventFilterQuery,
    ) -> List[Tuple[str, FVal, Asset, Timestamp]]:
        """
        Searches base entries missing usd prices that have not previously been checked in
        this session.
        """
        # Use a deepcopy to avoid mutations in the filter query if it is used later
        db = DBHistoryEvents(self.database)
        new_query_filter = copy.deepcopy(query_filter)
        new_query_filter.filters.append(
            DBStringFilter(and_op=True, column='usd_value', value='0'),
        )
        new_query_filter.filters.append(
            DBIgnoreValuesFilter(
                and_op=True,
                column='identifier',
                values=list(self.base_entries_ignore_set),
            ),
        )
        return db.rows_missing_prices_in_base_entries(filter_query=new_query_filter)

    def query_missing_prices_of_base_entries(
        self,
        entries_missing_prices: List[Tuple[str, FVal, Asset, Timestamp]],
    ) -> None:
        """Queries missing prices for HistoryBaseEntry in database updating
        the price if it is found. Otherwise we add the id to the ignore list
        for this session.
        """
        inquirer = PriceHistorian()
        updates = []
        for identifier, amount, asset, timestamp in entries_missing_prices:
            try:
                price = inquirer.query_historical_price(
                    from_asset=asset,
                    to_asset=A_USD,
                    timestamp=timestamp,
                )
            except (NoPriceForGivenTimestamp, RemoteError) as e:
                log.error(
                    f'Failed to find price for {asset} at {timestamp} in base '
                    f'entry {identifier}. {str(e)}.',
                )
                self.base_entries_ignore_set.add(identifier)
                continue

            usd_value = amount * price
            updates.append((str(usd_value), identifier))

        query = 'UPDATE history_events SET usd_value=? WHERE identifier=?'
        cursor = self.database.conn.cursor()
        cursor.executemany(query, updates)
        self.database.update_last_write()

    def _schedule(self) -> None:
        """Schedules background tasks"""
        self.greenlet_manager.clear_finished()
        current_greenlets = len(self.greenlet_manager.greenlets) + len(self.api_task_greenlets)
        not_proceed = current_greenlets >= self.max_tasks_num
        log.debug(
            f'At task scheduling. Current greenlets: {current_greenlets} '
            f'Max greenlets: {self.max_tasks_num}. '
            f'{"Will not schedule" if not_proceed else "Will schedule"}.',
        )
        if not_proceed:
            return  # too busy

        callables = random.sample(
            population=self.potential_tasks,
            k=min(self.max_tasks_num - current_greenlets, len(self.potential_tasks)),
        )

        for callable_fn in callables:
            callable_fn()  # type: ignore

    def schedule(self) -> None:
        """Schedules background task while holding the scheduling lock

        Used during logout to make sure no task is being scheduled at the same time
        as logging out
        """
        with self.schedule_lock:
            self._schedule()
