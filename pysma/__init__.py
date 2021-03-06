"""SMA WebConnect library for Python.

See: http://www.sma.de/en/products/monitoring-control/webconnect.html

Source: http://www.github.com/kellerza/pysma
"""
import asyncio
import json
import logging

import async_timeout
import attr
import jmespath

_LOGGER = logging.getLogger(__name__)

USERS = {
    'user': 'usr',
    'installer': 'istl',
}

JMESPATH_BASE = 'result.*'
JMESPATH_VAL_IDX = '"1"[{}].val'
JMESPATH_VAL = 'val'


@attr.s(slots=True)
class Sensor(object):
    """pysma sensor definition."""
    name = attr.ib()
    key = attr.ib()
    unit = attr.ib()
    factor = attr.ib(default=None)
    path = attr.ib(default=None)
    value = attr.ib(default=None)
    key_idx = attr.ib(default=0)

    def __attrs_post_init__(self):
        """Init path."""
        idx = 0
        key = str(self.key)
        if key[-2] == '_' and key[-1].isdigit():
            idx = key[-1]
            key = key[:-2]
        self.key = key
        self.key_idx = idx

    def extract_value(self, json_body):
        """Extract value from json body."""
        # Extract
        res = next(iter(jmespath.search(JMESPATH_BASE, json_body)))

        try:
            res = res[self.key]
        except (KeyError, TypeError):
            _LOGGER.warning("Sensor %s not found in %s", self.key, res)
            self.value = None
            return False

        if self.path is None:
            # Try different methods until we can decode...
            _paths = [JMESPATH_VAL, JMESPATH_VAL_IDX.format(self.key_idx)]
            while _paths:
                _path = _paths.pop()
                _val = jmespath.search(_path, res)
                if _val:
                    _LOGGER.debug("Extracting %s using %s", self.name, _path)
                    self.path = _path
                    break

        # Extract new value
        if self.path is None:
            _LOGGER.debug("Null path %s", res)
            res = None
        else:
            res = jmespath.search(self.path, res)

        if isinstance(res, int) and self.factor:
            res /= self.factor
        try:
            return res != self.value
        finally:
            self.value = res


SENSORS = [
    Sensor('current_consumption', '6100_00543100', 'W'),
    Sensor('current_power', '6100_40263F00', 'W'),
    Sensor('total_consumption', '6400_00543A00', 'kWh', 1000),
    Sensor('total_yield', '6400_00260100', 'kWh', 1000),
    Sensor('daily_yield', '6400_00262200', 'Wh'),
    Sensor('grid_voltage', '6100_00464800', 'V'),
    Sensor('pv_power', '6100_0046C200', 'W'),
    Sensor('daily_yield', '6400_00262200', 'Wh'),

    Sensor('grid_power_supplied', '6100_40463600', 'W'),
    Sensor('grid_power_absorbed', '6100_40463700', 'W'),
    Sensor('grid_total_yield', '6400_00462400', 'kWh', 1000),
    Sensor('grid_total_absorbed', '6400_00462500', 'kWh', 1000),

    Sensor('status', '6180_08214800', '', None, '"1"[].val[0].tag'),
]


def add_sensor(sensor):
    """Add a sensor, warning if it exists."""
    if not isinstance(sensor, Sensor):
        raise TypeError("pysma.Sensor expected")
    if get_sensor(sensor.name):
        _LOGGER.error("Cannot add sensor. %s already exists", sensor.name)
    if get_sensor(sensor.key):
        _LOGGER.error("Cannot add sensor %s. Key %s already exists",
                      sensor.name, sensor.key)
    SENSORS.append(sensor)


def get_sensor(name):
    """Get a sensor using either the name or key."""
    for sen in SENSORS:
        if sen.name == name or sen.key == name:
            return sen
    return None


URL_LOGIN = "/dyn/login.json"
URL_LOGOUT = "/dyn/logout.json"
URL_VALUES = "/dyn/getValues.json"


class SMA:
    """Class to connect to the SMA webconnect module and read parameters."""

    def __init__(self, session, url, password, group='user'):
        """Init SMA connection."""
        if group not in USERS:
            raise KeyError("Invalid user type: {}".format(group))
        self._new_session_data = {'right': USERS[group], 'pass': password}
        self._url = url.rstrip('/')
        if not url.startswith('http'):
            self._url = "http://" + self._url
        self._aio_session = session
        self.sma_sid = None

    @asyncio.coroutine
    def _fetch_json(self, url, payload):
        """Fetch json data for requests."""
        params = {
            'data': json.dumps(payload),
            'headers': {'content-type': 'application/json'},
            'params': {'sid': self.sma_sid} if self.sma_sid else None,
        }
        for _ in range(3):
            try:
                with async_timeout.timeout(3):
                    res = yield from self._aio_session.post(
                        self._url + url, **params)
                    return (yield from res.json()) or {}
            except asyncio.TimeoutError:
                continue
        return {'err': "Could not connect to SMA at {} (timeout)"
                       .format(self._url)}

    @asyncio.coroutine
    def new_session(self):
        """Establish a new session."""
        body = yield from self._fetch_json(URL_LOGIN, self._new_session_data)
        self.sma_sid = jmespath.search('result.sid', body)
        if self.sma_sid:
            return True

        msg = 'Could not start session, %s, got {}'.format(body)

        if body.get('err'):
            if body.get('err') == 503:
                _LOGGER.error("Max amount of sesions reached")
            else:
                _LOGGER.error(msg, body.get('err'))
        else:
            _LOGGER.error(msg, "Session ID expected [result.sid]")
        return False

    @asyncio.coroutine
    def close_session(self):
        """Close the session login."""
        if self.sma_sid is None:
            return
        yield from self._fetch_json(URL_LOGOUT, {})
        self.sma_sid = None

    @asyncio.coroutine
    def read(self, sensors):
        """Read a set of keys."""
        payload = {'destDev': [], 'keys': list(set([s.key for s in sensors]))}
        if self.sma_sid is None:
            yield from self.new_session()
            if self.sma_sid is None:
                return False
        body = yield from self._fetch_json(URL_VALUES, payload=payload)

        # On the first 401 error we close the session which will re-login
        if body.get('err') == 401:
            _LOGGER.warning("401 error detected, closing session to force "
                            "another login attempt")
            self.close_session()
            return False

        _LOGGER.debug(json.dumps(body))
        for sen in sensors:
            if sen.extract_value(body):
                _LOGGER.debug("%s\t= %s %s",
                              sen.name, sen.value, sen.unit)
        return True
