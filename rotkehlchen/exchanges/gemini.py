import hashlib
import hmac
import json
import logging
from base64 import b64encode
from http import HTTPStatus
from json.decoder import JSONDecodeError
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple, Union, overload

import gevent
import requests
from typing_extensions import Literal

from rotkehlchen.assets.asset import Asset
from rotkehlchen.constants.misc import ZERO
from rotkehlchen.constants.timing import QUERY_RETRY_TIMES, GLOBAL_REQUESTS_TIMEOUT
from rotkehlchen.errors import (
    DeserializationError,
    RemoteError,
    UnknownAsset,
    UnprocessableTradePair,
    UnsupportedAsset,
)
from rotkehlchen.exchanges.data_structures import AssetMovement, Trade
from rotkehlchen.exchanges.exchange import ExchangeInterface
from rotkehlchen.exchanges.utils import deserialize_asset_movement_address, get_key_if_has_val
from rotkehlchen.inquirer import Inquirer
from rotkehlchen.logging import RotkehlchenLogsAdapter
from rotkehlchen.serialization.deserialize import (
    deserialize_asset_amount,
    deserialize_asset_amount_force_positive,
    deserialize_asset_movement_category,
    deserialize_fee,
    deserialize_price,
    deserialize_timestamp,
    deserialize_trade_type,
)
from rotkehlchen.typing import ApiKey, ApiSecret, Fee, Location, Timestamp, TradePair
from rotkehlchen.user_messages import MessagesAggregator
from rotkehlchen.utils.interfaces import cache_response_timewise, protect_with_lock
from rotkehlchen.utils.misc import ts_now_in_ms
from rotkehlchen.utils.serialization import rlk_jsonloads_dict, rlk_jsonloads_list

if TYPE_CHECKING:
    from rotkehlchen.db.dbhandler import DBHandler


logger = logging.getLogger(__name__)
log = RotkehlchenLogsAdapter(logger)


class GeminiPermissionError(Exception):
    pass


def gemini_symbol_to_pair(symbol: str) -> TradePair:
    """Turns a gemini symbol product into our trade pair format

    - Can raise UnprocessableTradePair if symbol is in unexpected format
    - Case raise UnknownAsset if any of the pair assets are not known to Rotki
    """
    if len(symbol) == 6:
        base_asset = Asset(symbol[:3].upper())
        quote_asset = Asset(symbol[3:].upper())
    elif len(symbol) == 7:
        try:
            base_asset = Asset(symbol[:4].upper())
            quote_asset = Asset(symbol[4:].upper())
        except UnknownAsset:
            base_asset = Asset(symbol[:3].upper())
            quote_asset = Asset(symbol[3:].upper())
    elif len(symbol) == 8:
        if 'storj' in symbol:
            base_asset = Asset(symbol[:5].upper())
            quote_asset = Asset(symbol[5:].upper())
        else:
            base_asset = Asset(symbol[:4].upper())
            quote_asset = Asset(symbol[4:].upper())
    else:
        raise UnprocessableTradePair(symbol)

    return TradePair(f'{base_asset.identifier}_{quote_asset.identifier}')


class Gemini(ExchangeInterface):

    def __init__(
            self,
            api_key: ApiKey,
            secret: ApiSecret,
            database: 'DBHandler',
            msg_aggregator: MessagesAggregator,
            base_uri: str = 'https://api.gemini.com',
    ):
        super().__init__('gemini', api_key, secret, database)
        self.base_uri = base_uri
        self.msg_aggregator = msg_aggregator

        self.session.headers.update({
            'Content-Type': 'text/plain',
            'X-GEMINI-APIKEY': self.api_key,
            'Cache-Control': 'no-cache',
            'Content-Length': '0',
        })

    def first_connection(self) -> None:
        if self.first_connection_made:
            return

        # If it's the first time, populate the gemini trade symbols
        self._symbols = self._public_api_query('symbols')
        self.first_connection_made = True

    def validate_api_key(self) -> Tuple[bool, str]:
        """Validates that the Gemini API key is good for usage in Rotki

        Makes sure that the following permissions are given to the key:
        - Auditor
        """
        msg = (
            'Provided Gemini API key needs to have "Auditor" permission activated. '
            'Please log into your gemini account and create a key with '
            'the required permissions.'
        )
        try:
            roles = self._private_api_query(endpoint='roles')
        except GeminiPermissionError:
            return False, msg
        except RemoteError as e:
            error = str(e)
            return False, error

        if roles.get('isAuditor', False) is False:
            return False, msg

        return True, ''

    @property
    def symbols(self) -> List[str]:
        self.first_connection()
        return self._symbols

    def _query_continuously(
            self,
            method: Literal['get', 'post'],
            endpoint: str,
            options: Optional[Dict[str, Any]] = None,
    ) -> requests.Response:
        """Queries endpoint until anything but 429 is returned

        May raise:
        - RemoteError if something is wrong connecting to the exchange
        """
        v_endpoint = f'/v1/{endpoint}'
        url = f'{self.base_uri}{v_endpoint}'
        retries_left = QUERY_RETRY_TIMES
        while retries_left > 0:
            if endpoint in ('mytrades', 'balances', 'transfers', 'roles'):
                # private endpoints
                timestamp = str(ts_now_in_ms())
                payload = {'request': v_endpoint, 'nonce': timestamp}
                if options is not None:
                    payload.update(options)
                encoded_payload = json.dumps(payload).encode()
                b64 = b64encode(encoded_payload)
                signature = hmac.new(self.secret, b64, hashlib.sha384).hexdigest()

                self.session.headers.update({
                    'X-GEMINI-PAYLOAD': b64.decode(),
                    'X-GEMINI-SIGNATURE': signature,
                })

            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    timeout=GLOBAL_REQUESTS_TIMEOUT,
                )
            except requests.exceptions.RequestException as e:
                raise RemoteError(
                    f'Gemini {method} query at {url} connection error: {str(e)}',
                ) from e

            if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
                # Backoff a bit by sleeping. Sleep more, the more retries have been made
                gevent.sleep(QUERY_RETRY_TIMES / retries_left)
                retries_left -= 1
            else:
                # get out of the retry loop, we did not get 429 complaint
                break

        return response

    def _public_api_query(
            self,
            endpoint: str,
    ) -> List[Any]:
        """Performs a Gemini API Query for a public endpoint

        You can optionally provide extra arguments to the endpoint via the options argument.

        Raises RemoteError if something went wrong with connecting or reading from the exchange
        """
        response = self._query_continuously(method='get', endpoint=endpoint)
        if response.status_code != HTTPStatus.OK:
            raise RemoteError(
                f'Gemini query at {response.url} responded with error '
                f'status code: {response.status_code} and text: {response.text}',
            )

        try:
            json_ret = rlk_jsonloads_list(response.text)
        except JSONDecodeError as e:
            raise RemoteError(
                f'Gemini  query at {response.url} '
                f'returned invalid JSON response: {response.text}',
            ) from e

        return json_ret

    @overload  # noqa: F811
    def _private_api_query(  # pylint: disable=no-self-use
            self,
            endpoint: Literal['roles'],
            options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        ...

    @overload  # noqa: F811
    def _private_api_query(  # pylint: disable=no-self-use
            self,
            endpoint: Literal['balances', 'mytrades', 'transfers'],
            options: Optional[Dict[str, Any]] = None,
    ) -> List[Any]:
        ...

    def _private_api_query(  # noqa: F811
            self,
            endpoint: str,
            options: Optional[Dict[str, Any]] = None,
    ) -> Union[Dict[str, Any], List[Any]]:
        """Performs a Gemini API Query for a private endpoint

        You can optionally provide extra arguments to the endpoint via the options argument.

        Raises RemoteError if something went wrong with connecting or reading from the exchange
        Raises GeminiPermissionError if the API Key does not have sufficient
        permissions for the endpoint
        """
        response = self._query_continuously(method='post', endpoint=endpoint, options=options)
        json_ret: Union[List[Any], Dict[str, Any]]
        if response.status_code == HTTPStatus.FORBIDDEN:
            raise GeminiPermissionError(
                f'API key does not have permission for {endpoint}',
            )
        if response.status_code == HTTPStatus.BAD_REQUEST:
            if 'InvalidSignature' in response.text:
                raise GeminiPermissionError('Invalid API Key or API secret')
            # else let it be handled by the generic non-200 code error below

        if response.status_code != HTTPStatus.OK:
            raise RemoteError(
                f'Gemini query at {response.url} responded with error '
                f'status code: {response.status_code} and text: {response.text}',
            )

        deserialization_fn: Union[Callable[[str], Dict[str, Any]], Callable[[str], List[Any]]]
        deserialization_fn = rlk_jsonloads_dict if endpoint == 'roles' else rlk_jsonloads_list

        try:
            json_ret = deserialization_fn(response.text)
        except JSONDecodeError as e:
            raise RemoteError(
                f'Gemini query at {response.url} '
                f'returned invalid JSON response: {response.text}',
            ) from e

        return json_ret

    @protect_with_lock()
    @cache_response_timewise()
    def query_balances(self) -> Tuple[Optional[Dict[Asset, Dict[str, Any]]], str]:
        try:
            balances = self._private_api_query('balances')
        except (GeminiPermissionError, RemoteError) as e:
            msg = f'Gemini API request failed. {str(e)}'
            log.error(msg)
            return None, msg

        returned_balances: Dict[Asset, Dict[str, Any]] = {}
        for entry in balances:
            try:
                amount = deserialize_asset_amount(entry['amount'])
                # ignore empty balances
                if amount == ZERO:
                    continue

                asset = Asset(entry['currency'])
                try:
                    usd_price = Inquirer().find_usd_price(asset=asset)
                except RemoteError as e:
                    self.msg_aggregator.add_error(
                        f'Error processing gemini balance result due to inability to '
                        f'query USD price: {str(e)}. Skipping balance entry',
                    )
                    continue
                returned_balances[asset] = {
                    'amount': amount,
                    'usd_value': amount * usd_price,
                }

            except UnknownAsset as e:
                self.msg_aggregator.add_warning(
                    f'Found gemini balance result with unknown asset '
                    f'{e.asset_name}. Ignoring it.',
                )
                continue
            except UnsupportedAsset as e:
                self.msg_aggregator.add_warning(
                    f'Found gemini balance result with unsupported asset '
                    f'{e.asset_name}. Ignoring it.',
                )
                continue
            except (DeserializationError, KeyError) as e:
                msg = str(e)
                if isinstance(e, KeyError):
                    msg = f'Missing key entry for {msg}.'
                self.msg_aggregator.add_error(
                    'Error processing a gemini balance. Check logs '
                    'for details. Ignoring it.',
                )
                log.error(
                    'Error processing a gemini balance',
                    error=msg,
                )
                continue

        return returned_balances, ''

    def _get_paginated_query(
            self,
            endpoint: Literal['mytrades', 'transfers'],
            start_ts: Timestamp,
            end_ts: Timestamp,
            **kwargs: Any,
    ) -> List[Dict]:
        """Gets all possible results of a paginated gemini query"""
        options: Dict[str, Any] = {'timestamp': start_ts, **kwargs}
        # set maximum limits per endpoint as per API docs
        if endpoint == 'mytrades':
            # https://docs.gemini.com/rest-api/?python#get-past-trades
            limit = 500
            options['limit_trades'] = limit
        elif endpoint == 'transfers':
            # https://docs.gemini.com/rest-api/?python#transfers
            limit = 50
            options['limit_trades'] = limit
        else:
            raise AssertionError('_get_paginated_query() used with invalid endpoint')
        result = []

        while True:
            single_result = self._private_api_query(
                endpoint=endpoint,
                options=options,
            )
            result.extend(single_result)
            if len(single_result) < limit:
                break
            # Use millisecond timestamp as pagination mechanism for lack of better option
            # Most recent entry is first
            last_ts_ms = single_result[0]['timestampms']
            # also if we are already over the end timestamp stop
            if int(last_ts_ms / 1000) > end_ts:
                break
            options['timestamp'] = last_ts_ms + 1

        # Gemini results have the most recent first, but we want the oldest first.
        result.reverse()
        # If any entry falls outside the end_ts skip it
        checked_result = []
        for entry in result:
            if (entry['timestampms'] / 1000) > end_ts:
                break
            checked_result.append(entry)

        return checked_result

    def _get_trades_for_symbol(
            self,
            symbol: str,
            start_ts: Timestamp,
            end_ts: Timestamp,
    ) -> List[Dict]:
        try:
            trades = self._get_paginated_query(
                endpoint='mytrades',
                start_ts=start_ts,
                end_ts=end_ts,
                symbol=symbol,
            )
        except GeminiPermissionError as e:
            self.msg_aggregator.add_error(
                f'Got permission error while querying Gemini for trades: {str(e)}',
            )
            return []
        except RemoteError as e:
            self.msg_aggregator.add_error(
                f'Got remote error while querying Gemini for trades: {str(e)}',
            )
            return []
        return trades

    def query_online_trade_history(
            self,
            start_ts: Timestamp,
            end_ts: Timestamp,
    ) -> List[Trade]:
        """Queries gemini for trades
        """
        log.debug('Query gemini trade history', start_ts=start_ts, end_ts=end_ts)
        trades = []
        gemini_trades = []
        for symbol in self.symbols:
            gemini_trades = self._get_trades_for_symbol(
                symbol=symbol,
                start_ts=start_ts,
                end_ts=end_ts,
            )
            for entry in gemini_trades:
                try:
                    timestamp = deserialize_timestamp(entry['timestamp'])
                    if timestamp > end_ts:
                        break

                    trades.append(Trade(
                        timestamp=timestamp,
                        location=Location.GEMINI,
                        pair=gemini_symbol_to_pair(symbol),
                        trade_type=deserialize_trade_type(entry['type']),
                        amount=deserialize_asset_amount(entry['amount']),
                        rate=deserialize_price(entry['price']),
                        fee=deserialize_fee(entry['fee_amount']),
                        fee_currency=Asset(entry['fee_currency']),
                        link=str(entry['tid']),
                        notes='',
                    ))
                except UnprocessableTradePair as e:
                    self.msg_aggregator.add_warning(
                        f'Found unprocessable Gemini pair {e.pair}. Ignoring the trade.',
                    )
                    continue
                except UnknownAsset as e:
                    self.msg_aggregator.add_warning(
                        f'Found unknown Gemini asset {e.asset_name}. '
                        f'Ignoring the trade.',
                    )
                    continue
                except (DeserializationError, KeyError) as e:
                    msg = str(e)
                    if isinstance(e, KeyError):
                        msg = f'Missing key entry for {msg}.'
                    self.msg_aggregator.add_error(
                        'Failed to deserialize a gemini trade. '
                        'Check logs for details. Ignoring it.',
                    )
                    log.error(
                        'Error processing a gemini trade.',
                        raw_trade=entry,
                        error=msg,
                    )
                    continue

        return trades

    def query_online_deposits_withdrawals(
            self,
            start_ts: Timestamp,
            end_ts: Timestamp,
    ) -> List[AssetMovement]:
        result = self._get_paginated_query(
            endpoint='transfers',
            start_ts=start_ts,
            end_ts=end_ts,
        )
        movements = []
        for entry in result:
            try:
                timestamp = deserialize_timestamp(entry['timestampms'])
                timestamp = Timestamp(int(timestamp / 1000))
                asset = Asset(entry['currency'])

                movement = AssetMovement(
                    location=Location.GEMINI,
                    category=deserialize_asset_movement_category(entry['type']),
                    address=deserialize_asset_movement_address(entry, 'destination', asset),
                    transaction_id=get_key_if_has_val(entry, 'txHash'),
                    timestamp=timestamp,
                    asset=asset,
                    amount=deserialize_asset_amount_force_positive(entry['amount']),
                    fee_asset=asset,
                    # Gemini does not include withdrawal fees neither in the API nor in their UI
                    fee=Fee(ZERO),
                    link=str(entry['eid']),
                )
            except UnknownAsset as e:
                self.msg_aggregator.add_warning(
                    f'Found gemini deposit/withdrawal with unknown asset '
                    f'{e.asset_name}. Ignoring it.',
                )
                continue
            except UnsupportedAsset as e:
                self.msg_aggregator.add_warning(
                    f'Found gemini deposit/withdrawal with unsupported asset '
                    f'{e.asset_name}. Ignoring it.',
                )
                continue
            except (DeserializationError, KeyError) as e:
                msg = str(e)
                if isinstance(e, KeyError):
                    msg = f'Missing key entry for {msg}.'
                self.msg_aggregator.add_error(
                    'Error processing a gemini deposit/withdrawal. Check logs '
                    'for details. Ignoring it.',
                )
                log.error(
                    'Error processing a gemini deposit_withdrawal',
                    asset_movement=entry,
                    error=msg,
                )
                continue

            movements.append(movement)

        return movements
