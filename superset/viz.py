# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
# pylint: disable=C,R,W
"""This module contains the 'Viz' objects

These objects represent the backend of all the visualizations that
Superset can render.
"""
import copy
import inspect
import logging
import math
import re
from collections import defaultdict, OrderedDict
from datetime import date, datetime, timedelta
from itertools import product
from typing import (
    Any,
    Callable,
    cast,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    Type,
    TYPE_CHECKING,
    Union,
)

import geohash
import numpy as np
import pandas as pd
import polyline
import simplejson as json
from dateutil import relativedelta as rdelta
from flask import request
from flask_babel import lazy_gettext as _
from geopy.point import Point
from pandas.tseries.frequencies import to_offset

from superset import app, db, is_feature_enabled
from superset.constants import NULL_STRING
from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.exceptions import (
    CacheLoadError,
    NullValueException,
    QueryObjectValidationError,
    SpatialException,
    SupersetSecurityException,
)
from superset.extensions import cache_manager, security_manager
from superset.models.cache import CacheKey
from superset.models.helpers import QueryResult
from superset.typing import QueryObjectDict, VizData, VizPayload
from superset.utils import core as utils, csv
from superset.utils.cache import set_and_log_cache
from superset.utils.core import (
    DTTM_ALIAS,
    JS_MAX_INTEGER,
    merge_extra_filters,
    QueryMode,
    to_adhoc,
)
from superset.utils.date_parser import get_since_until, parse_past_timedelta
from superset.utils.dates import datetime_to_epoch
from superset.utils.hashing import md5_sha_from_str

import dataclasses  # isort:skip


if TYPE_CHECKING:
    from superset.connectors.base.models import BaseDatasource

config = app.config
stats_logger = config["STATS_LOGGER"]
relative_start = config["DEFAULT_RELATIVE_START_TIME"]
relative_end = config["DEFAULT_RELATIVE_END_TIME"]
logger = logging.getLogger(__name__)

METRIC_KEYS = [
    "metric",
    "metrics",
    "percent_metrics",
    "metric_2",
    "secondary_metric",
    "x",
    "y",
    "size",
]

# This regex is to get user defined filter column name, which is the first param in the filter_values function.
# see the definition of filter_values template:
# https://github.com/apache/superset/blob/24ad6063d736c1f38ad6f962e586b9b1a21946af/superset/jinja_context.py#L63
FILTER_VALUES_REGEX = re.compile(r"filter_values\(['\"](\w+)['\"]\,")


class BaseViz:

    """All visualizations derive this base class"""

    viz_type: Optional[str] = None
    verbose_name = "Base Viz"
    credits = ""
    is_timeseries = False
    cache_type = "df"
    enforce_numerical_metrics = True

    def __init__(
        self,
        datasource: "BaseDatasource",
        form_data: Dict[str, Any],
        force: bool = False,
        force_cached: bool = False,
    ) -> None:
        if not datasource:
            raise QueryObjectValidationError(_("Viz is missing a datasource"))

        self.datasource = datasource
        self.request = request
        self.viz_type = form_data.get("viz_type")
        self.form_data = form_data

        self.query = ""
        self.token = utils.get_form_data_token(form_data)

        self.groupby: List[str] = self.form_data.get("groupby") or []
        self.time_shift = timedelta()

        self.status: Optional[str] = None
        self.error_msg = ""
        self.results: Optional[QueryResult] = None
        self.errors: List[Dict[str, Any]] = []
        self.force = force
        self._force_cached = force_cached
        self.from_dttm: Optional[datetime] = None
        self.to_dttm: Optional[datetime] = None
        self._extra_chart_data: List[Tuple[str, pd.DataFrame]] = []

        self.process_metrics()

        self.applied_filters: List[Dict[str, str]] = []
        self.rejected_filters: List[Dict[str, str]] = []

    @property
    def force_cached(self) -> bool:
        return self._force_cached

    def process_metrics(self) -> None:
        # metrics in Viz is order sensitive, so metric_dict should be
        # OrderedDict
        self.metric_dict = OrderedDict()
        fd = self.form_data
        for mkey in METRIC_KEYS:
            val = fd.get(mkey)
            if val:
                if not isinstance(val, list):
                    val = [val]
                for o in val:
                    label = utils.get_metric_name(o)
                    self.metric_dict[label] = o

        # Cast to list needed to return serializable object in py3
        self.all_metrics = list(self.metric_dict.values())
        self.metric_labels = list(self.metric_dict.keys())

    @staticmethod
    def handle_js_int_overflow(
        data: Dict[str, List[Dict[str, Any]]]
    ) -> Dict[str, List[Dict[str, Any]]]:
        for d in data.get("records", {}):
            for k, v in list(d.items()):
                if isinstance(v, int):
                    # if an int is too big for Java Script to handle
                    # convert it to a string
                    if abs(v) > JS_MAX_INTEGER:
                        d[k] = str(v)
        return data

    def run_extra_queries(self) -> None:
        """Lifecycle method to use when more than one query is needed

        In rare-ish cases, a visualization may need to execute multiple
        queries. That is the case for FilterBox or for time comparison
        in Line chart for instance.

        In those cases, we need to make sure these queries run before the
        main `get_payload` method gets called, so that the overall caching
        metadata can be right. The way it works here is that if any of
        the previous `get_df_payload` calls hit the cache, the main
        payload's metadata will reflect that.

        The multi-query support may need more work to become a first class
        use case in the framework, and for the UI to reflect the subtleties
        (show that only some of the queries were served from cache for
        instance). In the meantime, since multi-query is rare, we treat
        it with a bit of a hack. Note that the hack became necessary
        when moving from caching the visualization's data itself, to caching
        the underlying query(ies).
        """
        pass

    def apply_rolling(self, df: pd.DataFrame) -> pd.DataFrame:
        fd = self.form_data
        rolling_type = fd.get("rolling_type")
        rolling_periods = int(fd.get("rolling_periods") or 0)
        min_periods = int(fd.get("min_periods") or 0)

        if rolling_type in ("mean", "std", "sum") and rolling_periods:
            kwargs = dict(window=rolling_periods, min_periods=min_periods)
            if rolling_type == "mean":
                df = df.rolling(**kwargs).mean()
            elif rolling_type == "std":
                df = df.rolling(**kwargs).std()
            elif rolling_type == "sum":
                df = df.rolling(**kwargs).sum()
        elif rolling_type == "cumsum":
            df = df.cumsum()
        if min_periods:
            df = df[min_periods:]
        if df.empty:
            raise QueryObjectValidationError(
                _(
                    "Applied rolling window did not return any data. Please make sure "
                    "the source query satisfies the minimum periods defined in the "
                    "rolling window."
                )
            )
        return df

    def get_samples(self) -> List[Dict[str, Any]]:
        query_obj = self.query_obj()
        query_obj.update(
            {
                "is_timeseries": False,
                "groupby": [],
                "metrics": [],
                "orderby": [],
                "row_limit": config["SAMPLES_ROW_LIMIT"],
                "columns": [o.column_name for o in self.datasource.columns],
            }
        )
        df = self.get_df_payload(query_obj)["df"]  # leverage caching logic
        return df.to_dict(orient="records")

    def get_df(self, query_obj: Optional[QueryObjectDict] = None) -> pd.DataFrame:
        """Returns a pandas dataframe based on the query object"""
        if not query_obj:
            query_obj = self.query_obj()
        if not query_obj:
            return pd.DataFrame()

        self.error_msg = ""

        timestamp_format = None
        if self.datasource.type == "table":
            granularity_col = self.datasource.get_column(query_obj["granularity"])
            if granularity_col:
                timestamp_format = granularity_col.python_date_format

        # The datasource here can be different backend but the interface is common
        self.results = self.datasource.query(query_obj)
        self.query = self.results.query
        self.status = self.results.status
        self.errors = self.results.errors

        df = self.results.df
        # Transform the timestamp we received from database to pandas supported
        # datetime format. If no python_date_format is specified, the pattern will
        # be considered as the default ISO date format
        # If the datetime format is unix, the parse will use the corresponding
        # parsing logic.
        if not df.empty:
            utils.normalize_dttm_col(
                df=df,
                timestamp_format=timestamp_format,
                offset=self.datasource.offset,
                time_shift=self.time_shift,
            )

            if self.enforce_numerical_metrics:
                self.df_metrics_to_num(df)

            df.replace([np.inf, -np.inf], np.nan, inplace=True)
        return df

    def df_metrics_to_num(self, df: pd.DataFrame) -> None:
        """Converting metrics to numeric when pandas.read_sql cannot"""
        metrics = self.metric_labels
        for col, dtype in df.dtypes.items():
            if dtype.type == np.object_ and col in metrics:
                df[col] = pd.to_numeric(df[col], errors="coerce")

    def process_query_filters(self) -> None:
        utils.convert_legacy_filters_into_adhoc(self.form_data)
        merge_extra_filters(self.form_data)
        utils.split_adhoc_filters_into_base_filters(self.form_data)

    def query_obj(self) -> QueryObjectDict:
        """Building a query object"""
        form_data = self.form_data

        self.process_query_filters()

        gb = self.groupby
        metrics = self.all_metrics or []
        columns = form_data.get("columns") or []
        # merge list and dedup while preserving order
        groupby = list(OrderedDict.fromkeys(gb + columns))

        is_timeseries = self.is_timeseries
        if DTTM_ALIAS in groupby:
            groupby.remove(DTTM_ALIAS)
            is_timeseries = True

        granularity = form_data.get("granularity") or form_data.get("granularity_sqla")
        limit = int(form_data.get("limit") or 0)
        timeseries_limit_metric = form_data.get("timeseries_limit_metric")
        row_limit = int(form_data.get("row_limit") or config["ROW_LIMIT"])

        # default order direction
        order_desc = form_data.get("order_desc", True)

        try:
            since, until = get_since_until(
                relative_start=relative_start,
                relative_end=relative_end,
                time_range=form_data.get("time_range"),
                since=form_data.get("since"),
                until=form_data.get("until"),
            )
        except ValueError as ex:
            raise QueryObjectValidationError(str(ex))

        time_shift = form_data.get("time_shift", "")
        self.time_shift = parse_past_timedelta(time_shift)
        from_dttm = None if since is None else (since - self.time_shift)
        to_dttm = None if until is None else (until - self.time_shift)
        if from_dttm and to_dttm and from_dttm > to_dttm:
            raise QueryObjectValidationError(
                _("From date cannot be larger than to date")
            )

        self.from_dttm = from_dttm
        self.to_dttm = to_dttm

        # extras are used to query elements specific to a datasource type
        # for instance the extra where clause that applies only to Tables
        extras = {
            "druid_time_origin": form_data.get("druid_time_origin", ""),
            "having": form_data.get("having", ""),
            "having_druid": form_data.get("having_filters", []),
            "time_grain_sqla": form_data.get("time_grain_sqla"),
            "time_range_endpoints": form_data.get("time_range_endpoints"),
            "where": form_data.get("where", ""),
        }

        return {
            "granularity": granularity,
            "from_dttm": from_dttm,
            "to_dttm": to_dttm,
            "is_timeseries": is_timeseries,
            "groupby": groupby,
            "metrics": metrics,
            "row_limit": row_limit,
            "filter": self.form_data.get("filters", []),
            "timeseries_limit": limit,
            "extras": extras,
            "timeseries_limit_metric": timeseries_limit_metric,
            "order_desc": order_desc,
        }

    @property
    def cache_timeout(self) -> int:
        if self.form_data.get("cache_timeout") is not None:
            return int(self.form_data["cache_timeout"])
        if self.datasource.cache_timeout is not None:
            return self.datasource.cache_timeout
        if (
            hasattr(self.datasource, "database")
            and self.datasource.database.cache_timeout
        ) is not None:
            return self.datasource.database.cache_timeout
        if config["DATA_CACHE_CONFIG"].get("CACHE_DEFAULT_TIMEOUT") is not None:
            return config["DATA_CACHE_CONFIG"]["CACHE_DEFAULT_TIMEOUT"]
        return config["CACHE_DEFAULT_TIMEOUT"]

    def get_json(self) -> str:
        return json.dumps(
            self.get_payload(), default=utils.json_int_dttm_ser, ignore_nan=True
        )

    def cache_key(self, query_obj: QueryObjectDict, **extra: Any) -> str:
        """
        The cache key is made out of the key/values in `query_obj`, plus any
        other key/values in `extra`.

        We remove datetime bounds that are hard values, and replace them with
        the use-provided inputs to bounds, which may be time-relative (as in
        "5 days ago" or "now").

        The `extra` arguments are currently used by time shift queries, since
        different time shifts wil differ only in the `from_dttm`, `to_dttm`,
        `inner_from_dttm`, and `inner_to_dttm` values which are stripped.
        """
        cache_dict = copy.copy(query_obj)
        cache_dict.update(extra)

        for k in ["from_dttm", "to_dttm", "inner_from_dttm", "inner_to_dttm"]:
            if k in cache_dict:
                del cache_dict[k]

        cache_dict["time_range"] = self.form_data.get("time_range")
        cache_dict["datasource"] = self.datasource.uid
        cache_dict["extra_cache_keys"] = self.datasource.get_extra_cache_keys(query_obj)
        cache_dict["rls"] = (
            security_manager.get_rls_ids(self.datasource)
            if is_feature_enabled("ROW_LEVEL_SECURITY")
            and self.datasource.is_rls_supported
            else []
        )
        cache_dict["changed_on"] = self.datasource.changed_on
        json_data = self.json_dumps(cache_dict, sort_keys=True)
        return md5_sha_from_str(json_data)

    def get_payload(self, query_obj: Optional[QueryObjectDict] = None) -> VizPayload:
        """Returns a payload of metadata and data"""

        try:
            self.run_extra_queries()
        except SupersetSecurityException as ex:
            error = dataclasses.asdict(ex.error)
            self.errors.append(error)
            self.status = utils.QueryStatus.FAILED

        payload = self.get_df_payload(query_obj)

        df = payload.get("df")

        if self.status != utils.QueryStatus.FAILED:
            payload["data"] = self.get_data(df)
        if "df" in payload:
            del payload["df"]

        filters = self.form_data.get("filters", [])
        filter_columns = [flt.get("col") for flt in filters]
        columns = set(self.datasource.column_names)
        filter_values_columns = []

        # if using virtual datasource, check filter_values
        if self.datasource.sql:
            filter_values_columns = (
                re.findall(FILTER_VALUES_REGEX, self.datasource.sql)
            ) or []

        applied_time_extras = self.form_data.get("applied_time_extras", {})
        applied_time_columns, rejected_time_columns = utils.get_time_filter_status(
            self.datasource, applied_time_extras
        )
        payload["applied_filters"] = [
            {"column": col}
            for col in filter_columns
            if col in columns or col in filter_values_columns
        ] + applied_time_columns
        payload["rejected_filters"] = [
            {"reason": "not_in_datasource", "column": col}
            for col in filter_columns
            if col not in columns and col not in filter_values_columns
        ] + rejected_time_columns

        return payload

    def get_df_payload(
        self, query_obj: Optional[QueryObjectDict] = None, **kwargs: Any
    ) -> Dict[str, Any]:
        """Handles caching around the df payload retrieval"""
        if not query_obj:
            query_obj = self.query_obj()
        cache_key = self.cache_key(query_obj, **kwargs) if query_obj else None
        cache_value = None
        logger.info("Cache key: {}".format(cache_key))
        is_loaded = False
        stacktrace = None
        df = None
        if cache_key and cache_manager.data_cache and not self.force:
            cache_value = cache_manager.data_cache.get(cache_key)
            if cache_value:
                stats_logger.incr("loading_from_cache")
                try:
                    df = cache_value["df"]
                    self.query = cache_value["query"]
                    self.status = utils.QueryStatus.SUCCESS
                    is_loaded = True
                    stats_logger.incr("loaded_from_cache")
                except Exception as ex:
                    logger.exception(ex)
                    logger.error(
                        "Error reading cache: " + utils.error_msg_from_exception(ex)
                    )
                logger.info("Serving from cache")

        if query_obj and not is_loaded:
            if self.force_cached:
                logger.warning(
                    f"force_cached (viz.py): value not found for cache key {cache_key}"
                )
                raise CacheLoadError(_("Cached value not found"))
            try:
                invalid_columns = [
                    col
                    for col in (query_obj.get("columns") or [])
                    + (query_obj.get("groupby") or [])
                    + utils.get_column_names_from_metrics(
                        cast(
                            List[Union[str, Dict[str, Any]]],
                            query_obj.get("metrics") or [],
                        )
                    )
                    if col not in self.datasource.column_names
                ]
                if invalid_columns:
                    raise QueryObjectValidationError(
                        _(
                            "Columns missing in datasource: %(invalid_columns)s",
                            invalid_columns=invalid_columns,
                        )
                    )
                df = self.get_df(query_obj)
                if self.status != utils.QueryStatus.FAILED:
                    stats_logger.incr("loaded_from_source")
                    if not self.force:
                        stats_logger.incr("loaded_from_source_without_force")
                    is_loaded = True
            except QueryObjectValidationError as ex:
                error = dataclasses.asdict(
                    SupersetError(
                        message=str(ex),
                        level=ErrorLevel.ERROR,
                        error_type=SupersetErrorType.VIZ_GET_DF_ERROR,
                    )
                )
                self.errors.append(error)
                self.status = utils.QueryStatus.FAILED
            except Exception as ex:
                logger.exception(ex)

                error = dataclasses.asdict(
                    SupersetError(
                        message=str(ex),
                        level=ErrorLevel.ERROR,
                        error_type=SupersetErrorType.VIZ_GET_DF_ERROR,
                    )
                )
                self.errors.append(error)
                self.status = utils.QueryStatus.FAILED
                stacktrace = utils.get_stacktrace()

            if is_loaded and cache_key and self.status != utils.QueryStatus.FAILED:
                set_and_log_cache(
                    cache_manager.data_cache,
                    cache_key,
                    {"df": df, "query": self.query},
                    self.cache_timeout,
                    self.datasource.uid,
                )
        return {
            "cache_key": cache_key,
            "cached_dttm": cache_value["dttm"] if cache_value is not None else None,
            "cache_timeout": self.cache_timeout,
            "df": df,
            "errors": self.errors,
            "form_data": self.form_data,
            "is_cached": cache_value is not None,
            "query": self.query,
            "from_dttm": self.from_dttm,
            "to_dttm": self.to_dttm,
            "status": self.status,
            "stacktrace": stacktrace,
            "rowcount": len(df.index) if df is not None else 0,
        }

    def json_dumps(self, obj: Any, sort_keys: bool = False) -> str:
        return json.dumps(
            obj, default=utils.json_int_dttm_ser, ignore_nan=True, sort_keys=sort_keys
        )

    def has_error(self, payload: VizPayload) -> bool:
        return (
            payload.get("status") == utils.QueryStatus.FAILED
            or payload.get("error") is not None
            or bool(payload.get("errors"))
        )

    def payload_json_and_has_error(self, payload: VizPayload) -> Tuple[str, bool]:
        return self.json_dumps(payload), self.has_error(payload)

    @property
    def data(self) -> Dict[str, Any]:
        """This is the data object serialized to the js layer"""
        content = {
            "form_data": self.form_data,
            "token": self.token,
            "viz_name": self.viz_type,
            "filter_select_enabled": self.datasource.filter_select_enabled,
        }
        return content

    def get_csv(self) -> Optional[str]:
        df = self.get_df_payload()["df"]  # leverage caching logic
        include_index = not isinstance(df.index, pd.RangeIndex)
        return csv.df_to_escaped_csv(df, index=include_index, **config["CSV_EXPORT"])

    def get_data(self, df: pd.DataFrame) -> VizData:
        return df.to_dict(orient="records")

    @property
    def json_data(self) -> str:
        return json.dumps(self.data)

    def raise_for_access(self) -> None:
        """
        Raise an exception if the user cannot access the resource.

        :raises SupersetSecurityException: If the user cannot access the resource
        """

        security_manager.raise_for_access(viz=self)


class TableViz(BaseViz):

    """A basic html table that is sortable and searchable"""

    viz_type = "table"
    verbose_name = _("Table View")
    credits = 'a <a href="https://github.com/airbnb/superset">Superset</a> original'
    is_timeseries = False
    enforce_numerical_metrics = False

    def process_metrics(self) -> None:
        """Process form data and store parsed column configs.
        1. Determine query mode based on form_data params.
             - Use `query_mode` if it has a valid value
             - Set as RAW mode if `all_columns` is set
             - Otherwise defaults to AGG mode
        2. Determine output columns based on query mode.
        """
        # Verify form data first: if not specifying query mode, then cannot have both
        # GROUP BY and RAW COLUMNS.
        fd = self.form_data
        if (
            not fd.get("query_mode")
            and fd.get("all_columns")
            and (fd.get("groupby") or fd.get("metrics") or fd.get("percent_metrics"))
        ):
            raise QueryObjectValidationError(
                _(
                    "You cannot use [Columns] in combination with "
                    "[Group By]/[Metrics]/[Percentage Metrics]. "
                    "Please choose one or the other."
                )
            )

        super().process_metrics()

        self.query_mode: QueryMode = QueryMode.get(fd.get("query_mode")) or (
            # infer query mode from the presence of other fields
            QueryMode.RAW
            if len(fd.get("all_columns") or []) > 0
            else QueryMode.AGGREGATE
        )

        columns: List[str] = []  # output columns sans time and percent_metric column
        percent_columns: List[str] = []  # percent columns that needs extra computation

        if self.query_mode == QueryMode.RAW:
            columns = utils.get_metric_names(fd.get("all_columns") or [])
        else:
            columns = utils.get_metric_names(self.groupby + (fd.get("metrics") or []))
            percent_columns = utils.get_metric_names(fd.get("percent_metrics") or [])

        self.columns = columns
        self.percent_columns = percent_columns
        self.is_timeseries = self.should_be_timeseries()

    def should_be_timeseries(self) -> bool:
        fd = self.form_data
        # TODO handle datasource-type-specific code in datasource
        conditions_met = (fd.get("granularity") and fd.get("granularity") != "all") or (
            fd.get("granularity_sqla") and fd.get("time_grain_sqla")
        )
        if fd.get("include_time") and not conditions_met:
            raise QueryObjectValidationError(
                _("Pick a granularity in the Time section or " "uncheck 'Include Time'")
            )
        return bool(fd.get("include_time"))

    def query_obj(self) -> QueryObjectDict:
        d = super().query_obj()
        fd = self.form_data
        if self.query_mode == QueryMode.RAW:
            d["columns"] = fd.get("all_columns")
            order_by_cols = fd.get("order_by_cols") or []
            d["orderby"] = [json.loads(t) for t in order_by_cols]
            # must disable groupby and metrics in raw mode
            d["groupby"] = []
            d["metrics"] = []
            # raw mode does not support timeseries queries
            d["timeseries_limit_metric"] = None
            d["timeseries_limit"] = None
            d["is_timeseries"] = None
        else:
            sort_by = fd.get("timeseries_limit_metric")
            if sort_by:
                sort_by_label = utils.get_metric_name(sort_by)
                if sort_by_label not in utils.get_metric_names(d["metrics"]):
                    d["metrics"].append(sort_by)
                d["orderby"] = [(sort_by, not fd.get("order_desc", True))]
            elif d["metrics"]:
                # Legacy behavior of sorting by first metric by default
                first_metric = d["metrics"][0]
                d["orderby"] = [(first_metric, not fd.get("order_desc", True))]
        return d

    def get_data(self, df: pd.DataFrame) -> VizData:
        """
        Transform the query result to the table representation.

        :param df: The interim dataframe
        :returns: The table visualization data

        The interim dataframe comprises of the group-by and non-group-by columns and
        the union of the metrics representing the non-percent and percent metrics. Note
        the percent metrics have yet to be transformed.
        """
        # Transform the data frame to adhere to the UI ordering of the columns and
        # metrics whilst simultaneously computing the percentages (via normalization)
        # for the percent metrics.
        if df.empty:
            return None

        columns, percent_columns = self.columns, self.percent_columns
        if DTTM_ALIAS in df and self.is_timeseries:
            columns = [DTTM_ALIAS] + columns
        df = pd.concat(
            [
                df[columns],
                (df[percent_columns].div(df[percent_columns].sum()).add_prefix("%")),
            ],
            axis=1,
        )
        return self.handle_js_int_overflow(
            dict(records=df.to_dict(orient="records"), columns=list(df.columns))
        )

    def json_dumps(self, obj: Any, sort_keys: bool = False) -> str:
        return json.dumps(
            obj, default=utils.json_iso_dttm_ser, sort_keys=sort_keys, ignore_nan=True
        )


class TimeTableViz(BaseViz):

    """A data table with rich time-series related columns"""

    viz_type = "time_table"
    verbose_name = _("Time Table View")
    credits = 'a <a href="https://github.com/airbnb/superset">Superset</a> original'
    is_timeseries = True

    def query_obj(self) -> QueryObjectDict:
        d = super().query_obj()
        fd = self.form_data

        if not fd.get("metrics"):
            raise QueryObjectValidationError(_("Pick at least one metric"))

        if fd.get("groupby") and len(fd["metrics"]) > 1:
            raise QueryObjectValidationError(
                _("When using 'Group By' you are limited to use a single metric")
            )
        return d

    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None

        fd = self.form_data
        columns = None
        values: Union[List[str], str] = self.metric_labels
        if fd.get("groupby"):
            values = self.metric_labels[0]
            columns = fd.get("groupby")
        pt = df.pivot_table(index=DTTM_ALIAS, columns=columns, values=values)
        pt.index = pt.index.map(str)
        pt = pt.sort_index()
        return dict(
            records=pt.to_dict(orient="index"),
            columns=list(pt.columns),
            is_group_by=True if fd.get("groupby") else False,
        )


class PivotTableViz(BaseViz):

    """A pivot table view, define your rows, columns and metrics"""

    viz_type = "pivot_table"
    verbose_name = _("Pivot Table")
    credits = 'a <a href="https://github.com/airbnb/superset">Superset</a> original'
    is_timeseries = False
    enforce_numerical_metrics = False

    def query_obj(self) -> QueryObjectDict:
        d = super().query_obj()
        groupby = self.form_data.get("groupby")
        columns = self.form_data.get("columns")
        metrics = self.form_data.get("metrics")
        transpose = self.form_data.get("transpose_pivot")
        if not columns:
            columns = []
        if not groupby:
            groupby = []
        if not groupby:
            raise QueryObjectValidationError(
                _("Please choose at least one 'Group by' field ")
            )
        if transpose and not columns:
            raise QueryObjectValidationError(
                _(
                    (
                        "Please choose at least one 'Columns' field when "
                        "select 'Transpose Pivot' option"
                    )
                )
            )
        if not metrics:
            raise QueryObjectValidationError(_("Please choose at least one metric"))
        if set(groupby) & set(columns):
            raise QueryObjectValidationError(_("Group By' and 'Columns' can't overlap"))
        sort_by = self.form_data.get("timeseries_limit_metric")
        if sort_by:
            sort_by_label = utils.get_metric_name(sort_by)
            if sort_by_label not in utils.get_metric_names(d["metrics"]):
                d["metrics"].append(sort_by)
            if self.form_data.get("order_desc"):
                d["orderby"] = [(sort_by, not self.form_data.get("order_desc", True))]
        return d

    @staticmethod
    def get_aggfunc(
        metric: str, df: pd.DataFrame, form_data: Dict[str, Any]
    ) -> Union[str, Callable[[Any], Any]]:
        aggfunc = form_data.get("pandas_aggfunc") or "sum"
        if pd.api.types.is_numeric_dtype(df[metric]):
            # Ensure that Pandas's sum function mimics that of SQL.
            if aggfunc == "sum":
                return lambda x: x.sum(min_count=1)
        # only min and max work properly for non-numerics
        return aggfunc if aggfunc in ("min", "max") else "max"

    @staticmethod
    def _format_datetime(value: Union[pd.Timestamp, datetime, date, str]) -> str:
        """
        Format a timestamp in such a way that the viz will be able to apply
        the correct formatting in the frontend.

        :param value: the value of a temporal column
        :return: formatted timestamp if it is a valid timestamp, otherwise
                 the original value
        """
        tstamp: Optional[pd.Timestamp] = None
        if isinstance(value, pd.Timestamp):
            tstamp = value
        if isinstance(value, datetime) or isinstance(value, date):
            tstamp = pd.Timestamp(value)
        if isinstance(value, str):
            try:
                tstamp = pd.Timestamp(value)
            except ValueError:
                pass
        if tstamp:
            return f"__timestamp:{datetime_to_epoch(tstamp)}"
        # fallback in case something incompatible is returned
        return cast(str, value)

    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None

        if self.form_data.get("granularity") == "all" and DTTM_ALIAS in df:
            del df[DTTM_ALIAS]

        metrics = [utils.get_metric_name(m) for m in self.form_data["metrics"]]
        aggfuncs: Dict[str, Union[str, Callable[[Any], Any]]] = {}
        for metric in metrics:
            aggfuncs[metric] = self.get_aggfunc(metric, df, self.form_data)

        groupby = self.form_data.get("groupby") or []
        columns = self.form_data.get("columns") or []

        for column_name in groupby + columns:
            column = self.datasource.get_column(column_name)
            if column and column.is_temporal:
                ts = df[column_name].apply(self._format_datetime)
                df[column_name] = ts

        if self.form_data.get("transpose_pivot"):
            groupby, columns = columns, groupby

        df = df.pivot_table(
            index=groupby,
            columns=columns,
            values=metrics,
            aggfunc=aggfuncs,
            margins=self.form_data.get("pivot_margins"),
        )

        # Re-order the columns adhering to the metric ordering.
        df = df[metrics]

        # Display metrics side by side with each column
        if self.form_data.get("combine_metric"):
            df = df.stack(0).unstack()
        return dict(
            columns=list(df.columns),
            html=df.to_html(
                na_rep="null",
                classes=(
                    "dataframe table table-striped table-bordered "
                    "table-condensed table-hover"
                ).split(" "),
            ),
        )


class TreemapViz(BaseViz):

    """Tree map visualisation for hierarchical data."""

    viz_type = "treemap"
    verbose_name = _("Treemap")
    credits = '<a href="https://d3js.org">d3.js</a>'
    is_timeseries = False

    def query_obj(self) -> QueryObjectDict:
        d = super().query_obj()
        sort_by = self.form_data.get("timeseries_limit_metric")
        if sort_by:
            sort_by_label = utils.get_metric_name(sort_by)
            if sort_by_label not in utils.get_metric_names(d["metrics"]):
                d["metrics"].append(sort_by)
            if self.form_data.get("order_desc"):
                d["orderby"] = [(sort_by, not self.form_data.get("order_desc", True))]
        return d

    def _nest(self, metric: str, df: pd.DataFrame) -> List[Dict[str, Any]]:
        nlevels = df.index.nlevels
        if nlevels == 1:
            result = [{"name": n, "value": v} for n, v in zip(df.index, df[metric])]
        else:
            result = [
                {"name": l, "children": self._nest(metric, df.loc[l])}
                for l in df.index.levels[0]
            ]
        return result

    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None

        df = df.set_index(self.form_data.get("groupby"))
        chart_data = [
            {"name": metric, "children": self._nest(metric, df)}
            for metric in df.columns
        ]
        return chart_data


class CalHeatmapViz(BaseViz):

    """Calendar heatmap."""

    viz_type = "cal_heatmap"
    verbose_name = _("Calendar Heatmap")
    credits = "<a href=https://github.com/wa0x6e/cal-heatmap>cal-heatmap</a>"
    is_timeseries = True

    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None

        form_data = self.form_data
        data = {}
        records = df.to_dict("records")
        for metric in self.metric_labels:
            values = {}
            for obj in records:
                v = obj[DTTM_ALIAS]
                if hasattr(v, "value"):
                    v = v.value
                values[str(v / 10 ** 9)] = obj.get(metric)
            data[metric] = values

        try:
            start, end = get_since_until(
                relative_start=relative_start,
                relative_end=relative_end,
                time_range=form_data.get("time_range"),
                since=form_data.get("since"),
                until=form_data.get("until"),
            )
        except ValueError as ex:
            raise QueryObjectValidationError(str(ex))
        if not start or not end:
            raise QueryObjectValidationError(
                "Please provide both time bounds (Since and Until)"
            )
        domain = form_data.get("domain_granularity")
        diff_delta = rdelta.relativedelta(end, start)
        diff_secs = (end - start).total_seconds()

        if domain == "year":
            range_ = end.year - start.year + 1
        elif domain == "month":
            range_ = diff_delta.years * 12 + diff_delta.months + 1
        elif domain == "week":
            range_ = diff_delta.years * 53 + diff_delta.weeks + 1
        elif domain == "day":
            range_ = diff_secs // (24 * 60 * 60) + 1  # type: ignore
        else:
            range_ = diff_secs // (60 * 60) + 1  # type: ignore

        return {
            "data": data,
            "start": start,
            "domain": domain,
            "subdomain": form_data.get("subdomain_granularity"),
            "range": range_,
        }

    def query_obj(self) -> QueryObjectDict:
        d = super().query_obj()
        fd = self.form_data
        d["metrics"] = fd.get("metrics")
        mapping = {
            "min": "PT1M",
            "hour": "PT1H",
            "day": "P1D",
            "week": "P1W",
            "month": "P1M",
            "year": "P1Y",
        }
        time_grain = mapping[fd.get("subdomain_granularity", "min")]
        if self.datasource.type == "druid":
            d["granularity"] = time_grain
        else:
            d["extras"]["time_grain_sqla"] = time_grain
        return d


class NVD3Viz(BaseViz):

    """Base class for all nvd3 vizs"""

    credits = '<a href="http://nvd3.org/">NVD3.org</a>'
    viz_type: Optional[str] = None
    verbose_name = "Base NVD3 Viz"
    is_timeseries = False


class BubbleViz(NVD3Viz):

    """Based on the NVD3 bubble chart"""

    viz_type = "bubble"
    verbose_name = _("Bubble Chart")
    is_timeseries = False

    def query_obj(self) -> QueryObjectDict:
        form_data = self.form_data
        d = super().query_obj()
        d["groupby"] = [form_data.get("entity")]
        if form_data.get("series"):
            d["groupby"].append(form_data.get("series"))

        # dedup groupby if it happens to be the same
        d["groupby"] = list(dict.fromkeys(d["groupby"]))

        self.x_metric = form_data["x"]
        self.y_metric = form_data["y"]
        self.z_metric = form_data["size"]
        self.entity = form_data.get("entity")
        self.series = form_data.get("series") or self.entity
        d["row_limit"] = form_data.get("limit")

        d["metrics"] = [self.z_metric, self.x_metric, self.y_metric]
        if len(set(self.metric_labels)) < 3:
            raise QueryObjectValidationError(_("Please use 3 different metric labels"))
        if not all(d["metrics"] + [self.entity]):
            raise QueryObjectValidationError(_("Pick a metric for x, y and size"))
        return d

    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None

        df["x"] = df[[utils.get_metric_name(self.x_metric)]]
        df["y"] = df[[utils.get_metric_name(self.y_metric)]]
        df["size"] = df[[utils.get_metric_name(self.z_metric)]]
        df["shape"] = "circle"
        df["group"] = df[[self.series]]

        series: Dict[Any, List[Any]] = defaultdict(list)
        for row in df.to_dict(orient="records"):
            series[row["group"]].append(row)
        chart_data = []
        for k, v in series.items():
            chart_data.append({"key": k, "values": v})
        return chart_data


class BulletViz(NVD3Viz):

    """Based on the NVD3 bullet chart"""

    viz_type = "bullet"
    verbose_name = _("Bullet Chart")
    is_timeseries = False

    def query_obj(self) -> QueryObjectDict:
        form_data = self.form_data
        d = super().query_obj()
        self.metric = form_data["metric"]

        d["metrics"] = [self.metric]
        if not self.metric:
            raise QueryObjectValidationError(_("Pick a metric to display"))
        return d

    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None
        df["metric"] = df[[utils.get_metric_name(self.metric)]]
        values = df["metric"].values
        return {
            "measures": values.tolist(),
        }


class BigNumberViz(BaseViz):

    """Put emphasis on a single metric with this big number viz"""

    viz_type = "big_number"
    verbose_name = _("Big Number with Trendline")
    credits = 'a <a href="https://github.com/airbnb/superset">Superset</a> original'
    is_timeseries = True

    def query_obj(self) -> QueryObjectDict:
        d = super().query_obj()
        metric = self.form_data.get("metric")
        if not metric:
            raise QueryObjectValidationError(_("Pick a metric!"))
        d["metrics"] = [self.form_data.get("metric")]
        self.form_data["metric"] = metric
        return d

    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None

        df = df.pivot_table(
            index=DTTM_ALIAS,
            columns=[],
            values=self.metric_labels,
            dropna=False,
            aggfunc=np.min,  # looking for any (only) value, preserving `None`
        )
        df = self.apply_rolling(df)
        df[DTTM_ALIAS] = df.index
        return super().get_data(df)


class BigNumberTotalViz(BaseViz):

    """Put emphasis on a single metric with this big number viz"""

    viz_type = "big_number_total"
    verbose_name = _("Big Number")
    credits = 'a <a href="https://github.com/airbnb/superset">Superset</a> original'
    is_timeseries = False

    def query_obj(self) -> QueryObjectDict:
        d = super().query_obj()
        metric = self.form_data.get("metric")
        if not metric:
            raise QueryObjectValidationError(_("Pick a metric!"))
        d["metrics"] = [self.form_data.get("metric")]
        self.form_data["metric"] = metric

        # Limiting rows is not required as only one cell is returned
        d["row_limit"] = None
        return d


class NVD3TimeSeriesViz(NVD3Viz):

    """A rich line chart component with tons of options"""

    viz_type = "line"
    verbose_name = _("Time Series - Line Chart")
    sort_series = False
    is_timeseries = True
    pivot_fill_value: Optional[int] = None

    def to_series(
        self, df: pd.DataFrame, classed: str = "", title_suffix: str = ""
    ) -> List[Dict[str, Any]]:
        cols = []
        for col in df.columns:
            if col == "":
                cols.append("N/A")
            elif col is None:
                cols.append("NULL")
            else:
                cols.append(col)
        df.columns = cols
        series = df.to_dict("series")

        chart_data = []
        for name in df.T.index.tolist():
            ys = series[name]
            if df[name].dtype.kind not in "biufc":
                continue
            series_title: Union[List[str], str, Tuple[str, ...]]
            if isinstance(name, list):
                series_title = [str(title) for title in name]
            elif isinstance(name, tuple):
                series_title = tuple(str(title) for title in name)
            else:
                series_title = str(name)
            if (
                isinstance(series_title, (list, tuple))
                and len(series_title) > 1
                and len(self.metric_labels) == 1
            ):
                # Removing metric from series name if only one metric
                series_title = series_title[1:]
            if title_suffix:
                if isinstance(series_title, str):
                    series_title = (series_title, title_suffix)
                elif isinstance(series_title, list):
                    series_title = series_title + [title_suffix]
                elif isinstance(series_title, tuple):
                    series_title = series_title + (title_suffix,)

            values = []
            non_nan_cnt = 0
            for ds in df.index:
                if ds in ys:
                    d = {"x": ds, "y": ys[ds]}
                    if not np.isnan(ys[ds]):
                        non_nan_cnt += 1
                else:
                    d = {}
                values.append(d)

            if non_nan_cnt == 0:
                continue

            d = {"key": series_title, "values": values}
            if classed:
                d["classed"] = classed
            chart_data.append(d)
        return chart_data

    def process_data(self, df: pd.DataFrame, aggregate: bool = False) -> VizData:
        fd = self.form_data
        if fd.get("granularity") == "all":
            raise QueryObjectValidationError(
                _("Pick a time granularity for your time series")
            )

        if df.empty:
            return df

        if aggregate:
            df = df.pivot_table(
                index=DTTM_ALIAS,
                columns=fd.get("groupby"),
                values=self.metric_labels,
                fill_value=0,
                aggfunc=sum,
            )
        else:
            df = df.pivot_table(
                index=DTTM_ALIAS,
                columns=fd.get("groupby"),
                values=self.metric_labels,
                fill_value=self.pivot_fill_value,
            )

        rule = fd.get("resample_rule")
        method = fd.get("resample_method")

        if rule and method:
            df = getattr(df.resample(rule), method)()

        if self.sort_series:
            dfs = df.sum()
            dfs.sort_values(ascending=False, inplace=True)
            df = df[dfs.index]

        df = self.apply_rolling(df)
        if fd.get("contribution"):
            dft = df.T
            df = (dft / dft.sum()).T

        return df

    def run_extra_queries(self) -> None:
        fd = self.form_data

        time_compare = fd.get("time_compare") or []
        # backwards compatibility
        if not isinstance(time_compare, list):
            time_compare = [time_compare]

        for option in time_compare:
            query_object = self.query_obj()
            try:
                delta = parse_past_timedelta(option)
            except ValueError as ex:
                raise QueryObjectValidationError(str(ex))
            query_object["inner_from_dttm"] = query_object["from_dttm"]
            query_object["inner_to_dttm"] = query_object["to_dttm"]

            if not query_object["from_dttm"] or not query_object["to_dttm"]:
                raise QueryObjectValidationError(
                    _(
                        "An enclosed time range (both start and end) must be specified "
                        "when using a Time Comparison."
                    )
                )
            query_object["from_dttm"] -= delta
            query_object["to_dttm"] -= delta

            df2 = self.get_df_payload(query_object, time_compare=option).get("df")
            if df2 is not None and DTTM_ALIAS in df2:
                dttm_series = df2[DTTM_ALIAS] + delta
                df2 = df2.drop(DTTM_ALIAS, axis=1)
                df2 = pd.concat([dttm_series, df2], axis=1)
                label = "{} offset".format(option)
                df2 = self.process_data(df2)
                self._extra_chart_data.append((label, df2))

    def get_data(self, df: pd.DataFrame) -> VizData:
        fd = self.form_data
        comparison_type = fd.get("comparison_type") or "values"
        df = self.process_data(df)
        if comparison_type == "values":
            # Filter out series with all NaN
            chart_data = self.to_series(df.dropna(axis=1, how="all"))

            for i, (label, df2) in enumerate(self._extra_chart_data):
                chart_data.extend(
                    self.to_series(
                        df2, classed="time-shift-{}".format(i), title_suffix=label
                    )
                )
        else:
            chart_data = []
            for i, (label, df2) in enumerate(self._extra_chart_data):
                # reindex df2 into the df2 index
                combined_index = df.index.union(df2.index)
                df2 = (
                    df2.reindex(combined_index)
                    .interpolate(method="time")
                    .reindex(df.index)
                )

                if comparison_type == "absolute":
                    diff = df - df2
                elif comparison_type == "percentage":
                    diff = (df - df2) / df2
                elif comparison_type == "ratio":
                    diff = df / df2
                else:
                    raise QueryObjectValidationError(
                        "Invalid `comparison_type`: {0}".format(comparison_type)
                    )

                # remove leading/trailing NaNs from the time shift difference
                diff = diff[diff.first_valid_index() : diff.last_valid_index()]

                chart_data.extend(
                    self.to_series(
                        diff, classed="time-shift-{}".format(i), title_suffix=label
                    )
                )

        if not self.sort_series:
            chart_data = sorted(chart_data, key=lambda x: tuple(x["key"]))
        return chart_data


class MultiLineViz(NVD3Viz):

    """Pile on multiple line charts"""

    viz_type = "line_multi"
    verbose_name = _("Time Series - Multiple Line Charts")

    is_timeseries = True

    def query_obj(self) -> QueryObjectDict:
        return {}

    def get_data(self, df: pd.DataFrame) -> VizData:
        multiline_fd = self.form_data
        # Late import to avoid circular import issues
        from superset.charts.dao import ChartDAO

        axis1_chart_ids = multiline_fd.get("line_charts", [])
        axis2_chart_ids = multiline_fd.get("line_charts_2", [])
        all_charts = {
            chart.id: chart
            for chart in ChartDAO.find_by_ids(axis1_chart_ids + axis2_chart_ids)
        }
        axis1_charts = [all_charts[chart_id] for chart_id in axis1_chart_ids]
        axis2_charts = [all_charts[chart_id] for chart_id in axis2_chart_ids]

        filters = multiline_fd.get("filters", [])
        add_prefix = multiline_fd.get("prefix_metric_with_slice_name", False)
        data = []
        min_x, max_x = None, None

        for chart, y_axis in [(chart, 1) for chart in axis1_charts] + [
            (chart, 2) for chart in axis2_charts
        ]:
            prefix = f"{chart.chart}: " if add_prefix else ""
            chart_fd = chart.form_data
            chart_fd["filters"] = chart_fd.get("filters", []) + filters
            if "extra_filters" in multiline_fd:
                chart_fd["extra_filters"] = multiline_fd["extra_filters"]
            if "time_range" in multiline_fd:
                chart_fd["time_range"] = multiline_fd["time_range"]
            viz_obj = viz_types[chart.viz_type](
                chart.datasource,
                form_data=chart_fd,
                force=self.force,
                force_cached=self.force_cached,
            )
            df = viz_obj.get_df_payload()["df"]
            chart_series = viz_obj.get_data(df) or []
            for series in chart_series:
                x_values = [value["x"] for value in series["values"]]
                min_x = min(x_values + ([min_x] if min_x is not None else []))
                max_x = max(x_values + ([max_x] if max_x is not None else []))
                series_key = (
                    series["key"]
                    if isinstance(series["key"], (list, tuple))
                    else [series["key"]]
                )
                data.append(
                    {
                        "key": prefix + ", ".join(series_key),
                        "type": "line",
                        "values": series["values"],
                        "yAxis": y_axis,
                    }
                )
        bounds = []
        if min_x is not None:
            bounds.append({"x": min_x, "y": None})
        if max_x is not None:
            bounds.append({"x": max_x, "y": None})

        for series in data:
            series["values"].extend(bounds)
        return data


class NVD3DualLineViz(NVD3Viz):

    """A rich line chart with dual axis"""

    viz_type = "dual_line"
    verbose_name = _("Time Series - Dual Axis Line Chart")
    sort_series = False
    is_timeseries = True

    def query_obj(self) -> QueryObjectDict:
        d = super().query_obj()
        m1 = self.form_data.get("metric")
        m2 = self.form_data.get("metric_2")
        d["metrics"] = [m1, m2]
        if not m1:
            raise QueryObjectValidationError(_("Pick a metric for left axis!"))
        if not m2:
            raise QueryObjectValidationError(_("Pick a metric for right axis!"))
        if m1 == m2:
            raise QueryObjectValidationError(
                _("Please choose different metrics" " on left and right axis")
            )
        return d

    def to_series(self, df: pd.DataFrame, classed: str = "") -> List[Dict[str, Any]]:
        cols = []
        for col in df.columns:
            if col == "":
                cols.append("N/A")
            elif col is None:
                cols.append("NULL")
            else:
                cols.append(col)
        df.columns = cols
        series = df.to_dict("series")
        chart_data = []
        metrics = [self.form_data["metric"], self.form_data["metric_2"]]
        for i, m in enumerate(metrics):
            m = utils.get_metric_name(m)
            ys = series[m]
            if df[m].dtype.kind not in "biufc":
                continue
            series_title = m
            d = {
                "key": series_title,
                "classed": classed,
                "values": [
                    {"x": ds, "y": ys[ds] if ds in ys else None} for ds in df.index
                ],
                "yAxis": i + 1,
                "type": "line",
            }
            chart_data.append(d)
        return chart_data

    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None

        fd = self.form_data

        if self.form_data.get("granularity") == "all":
            raise QueryObjectValidationError(
                _("Pick a time granularity for your time series")
            )

        metric = utils.get_metric_name(fd["metric"])
        metric_2 = utils.get_metric_name(fd["metric_2"])
        df = df.pivot_table(index=DTTM_ALIAS, values=[metric, metric_2])

        chart_data = self.to_series(df)
        return chart_data


class NVD3TimeSeriesBarViz(NVD3TimeSeriesViz):

    """A bar chart where the x axis is time"""

    viz_type = "bar"
    sort_series = True
    verbose_name = _("Time Series - Bar Chart")


class NVD3TimePivotViz(NVD3TimeSeriesViz):

    """Time Series - Periodicity Pivot"""

    viz_type = "time_pivot"
    sort_series = True
    verbose_name = _("Time Series - Period Pivot")

    def query_obj(self) -> QueryObjectDict:
        d = super().query_obj()
        d["metrics"] = [self.form_data.get("metric")]
        return d

    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None

        fd = self.form_data
        df = self.process_data(df)
        freq = to_offset(fd.get("freq"))
        try:
            freq = type(freq)(freq.n, normalize=True, **freq.kwds)
        except ValueError:
            freq = type(freq)(freq.n, **freq.kwds)
        df.index.name = None
        df[DTTM_ALIAS] = df.index.map(freq.rollback)
        df["ranked"] = df[DTTM_ALIAS].rank(method="dense", ascending=False) - 1
        df.ranked = df.ranked.map(int)
        df["series"] = "-" + df.ranked.map(str)
        df["series"] = df["series"].str.replace("-0", "current")
        rank_lookup = {
            row["series"]: row["ranked"] for row in df.to_dict(orient="records")
        }
        max_ts = df[DTTM_ALIAS].max()
        max_rank = df["ranked"].max()
        df[DTTM_ALIAS] = df.index + (max_ts - df[DTTM_ALIAS])
        df = df.pivot_table(
            index=DTTM_ALIAS,
            columns="series",
            values=utils.get_metric_name(fd["metric"]),
        )
        chart_data = self.to_series(df)
        for serie in chart_data:
            serie["rank"] = rank_lookup[serie["key"]]
            serie["perc"] = 1 - (serie["rank"] / (max_rank + 1))
        return chart_data


class NVD3CompareTimeSeriesViz(NVD3TimeSeriesViz):

    """A line chart component where you can compare the % change over time"""

    viz_type = "compare"
    verbose_name = _("Time Series - Percent Change")


class NVD3TimeSeriesStackedViz(NVD3TimeSeriesViz):

    """A rich stack area chart"""

    viz_type = "area"
    verbose_name = _("Time Series - Stacked")
    sort_series = True
    pivot_fill_value = 0

    def query_obj(self) -> QueryObjectDict:
        d = super().query_obj()
        sort_by = self.form_data.get("timeseries_limit_metric")
        if sort_by:
            sort_by_label = utils.get_metric_name(sort_by)
            if sort_by_label not in utils.get_metric_names(d["metrics"]):
                d["metrics"].append(sort_by)
            if self.form_data.get("order_desc"):
                d["orderby"] = [(sort_by, not self.form_data.get("order_desc", True))]
        return d


class HistogramViz(BaseViz):

    """Histogram"""

    viz_type = "histogram"
    verbose_name = _("Histogram")
    is_timeseries = False

    def query_obj(self) -> QueryObjectDict:
        """Returns the query object for this visualization"""
        d = super().query_obj()
        d["row_limit"] = self.form_data.get("row_limit", int(config["VIZ_ROW_LIMIT"]))
        numeric_columns = self.form_data.get("all_columns_x")
        if numeric_columns is None:
            raise QueryObjectValidationError(
                _("Must have at least one numeric column specified")
            )
        self.columns = numeric_columns
        d["columns"] = numeric_columns + self.groupby
        # override groupby entry to avoid aggregation
        d["groupby"] = None
        d["metrics"] = None
        return d

    def labelify(self, keys: Union[List[str], str], column: str) -> str:
        if isinstance(keys, str):
            keys = [keys]
        # removing undesirable characters
        labels = [re.sub(r"\W+", r"_", k) for k in keys]
        if len(self.columns) > 1 or not self.groupby:
            # Only show numeric column in label if there are many
            labels = [column] + labels
        return "__".join(labels)

    def get_data(self, df: pd.DataFrame) -> VizData:
        """Returns the chart data"""
        if df.empty:
            return None

        chart_data = []
        if len(self.groupby) > 0:
            groups = df.groupby(self.groupby)
        else:
            groups = [((), df)]
        for keys, data in groups:
            chart_data.extend(
                [
                    {
                        "key": self.labelify(keys, column),
                        "values": data[column].tolist(),
                    }
                    for column in self.columns
                ]
            )
        return chart_data


class DistributionBarViz(BaseViz):

    """A good old bar chart"""

    viz_type = "dist_bar"
    verbose_name = _("Distribution - Bar Chart")
    is_timeseries = False

    def query_obj(self) -> QueryObjectDict:
        d = super().query_obj()
        fd = self.form_data
        if len(d["groupby"]) < len(fd.get("groupby") or []) + len(
            fd.get("columns") or []
        ):
            raise QueryObjectValidationError(
                _("Can't have overlap between Series and Breakdowns")
            )
        if not fd.get("metrics"):
            raise QueryObjectValidationError(_("Pick at least one metric"))
        if not fd.get("groupby"):
            raise QueryObjectValidationError(_("Pick at least one field for [Series]"))

        sort_by = fd.get("timeseries_limit_metric")
        if sort_by:
            sort_by_label = utils.get_metric_name(sort_by)
            if sort_by_label not in utils.get_metric_names(d["metrics"]):
                d["metrics"].append(sort_by)
            d["orderby"] = [(sort_by, not fd.get("order_desc", True))]
        elif d["metrics"]:
            # Legacy behavior of sorting by first metric by default
            first_metric = d["metrics"][0]
            d["orderby"] = [(first_metric, not fd.get("order_desc", True))]

        return d

    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None

        fd = self.form_data
        metrics = self.metric_labels
        columns = fd.get("columns") or []

        # pandas will throw away nulls when grouping/pivoting,
        # so we substitute NULL_STRING for any nulls in the necessary columns
        filled_cols = self.groupby + columns
        df = df.copy()
        df[filled_cols] = df[filled_cols].fillna(value=NULL_STRING)

        row = df.groupby(self.groupby).sum()[metrics[0]].copy()
        row.sort_values(ascending=False, inplace=True)
        pt = df.pivot_table(index=self.groupby, columns=columns, values=metrics)
        if fd.get("contribution"):
            pt = pt.T
            pt = (pt / pt.sum()).T
        pt = pt.reindex(row.index)

        # Re-order the columns adhering to the metric ordering.
        pt = pt[metrics]
        chart_data = []
        for name, ys in pt.items():
            if pt[name].dtype.kind not in "biufc" or name in self.groupby:
                continue
            if isinstance(name, str):
                series_title = name
            else:
                offset = 0 if len(metrics) > 1 else 1
                series_title = ", ".join([str(s) for s in name[offset:]])
            values = []
            for i, v in ys.items():
                x = i
                if isinstance(x, (tuple, list)):
                    x = ", ".join([str(s) for s in x])
                else:
                    x = str(x)
                values.append({"x": x, "y": v})
            d = {"key": series_title, "values": values}
            chart_data.append(d)
        return chart_data


class SunburstViz(BaseViz):

    """A multi level sunburst chart"""

    viz_type = "sunburst"
    verbose_name = _("Sunburst")
    is_timeseries = False
    credits = (
        "Kerry Rodden "
        '@<a href="https://bl.ocks.org/kerryrodden/7090426">bl.ocks.org</a>'
    )

    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None
        fd = copy.deepcopy(self.form_data)
        cols = fd.get("groupby") or []
        cols.extend(["m1", "m2"])
        metric = utils.get_metric_name(fd["metric"])
        secondary_metric = (
            utils.get_metric_name(fd["secondary_metric"])
            if "secondary_metric" in fd
            else None
        )
        if metric == secondary_metric or secondary_metric is None:
            df.rename(columns={df.columns[-1]: "m1"}, inplace=True)
            df["m2"] = df["m1"]
        else:
            df.rename(columns={df.columns[-2]: "m1"}, inplace=True)
            df.rename(columns={df.columns[-1]: "m2"}, inplace=True)

        # Re-order the columns as the query result set column ordering may differ from
        # that listed in the hierarchy.
        df = df[cols]
        return df.to_numpy().tolist()

    def query_obj(self) -> QueryObjectDict:
        qry = super().query_obj()
        fd = self.form_data
        qry["metrics"] = [fd["metric"]]
        secondary_metric = fd.get("secondary_metric")
        if secondary_metric and secondary_metric != fd["metric"]:
            qry["metrics"].append(secondary_metric)
        if self.form_data.get("sort_by_metric", False):
            qry["orderby"] = [(qry["metrics"][0], False)]
        return qry


class SankeyViz(BaseViz):

    """A Sankey diagram that requires a parent-child dataset"""

    viz_type = "sankey"
    verbose_name = _("Sankey")
    is_timeseries = False
    credits = '<a href="https://www.npmjs.com/package/d3-sankey">d3-sankey on npm</a>'

    def query_obj(self) -> QueryObjectDict:
        qry = super().query_obj()
        if len(qry["groupby"]) != 2:
            raise QueryObjectValidationError(
                _("Pick exactly 2 columns as [Source / Target]")
            )
        qry["metrics"] = [self.form_data["metric"]]
        if self.form_data.get("sort_by_metric", False):
            qry["orderby"] = [(qry["metrics"][0], False)]
        return qry

    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None
        source, target = self.groupby
        (value,) = self.metric_labels
        df.rename(
            columns={source: "source", target: "target", value: "value",}, inplace=True,
        )
        df["source"] = df["source"].astype(str)
        df["target"] = df["target"].astype(str)
        recs = df.to_dict(orient="records")

        hierarchy: Dict[str, Set[str]] = defaultdict(set)
        for row in recs:
            hierarchy[row["source"]].add(row["target"])

        def find_cycle(g: Dict[str, Set[str]]) -> Optional[Tuple[str, str]]:
            """Whether there's a cycle in a directed graph"""
            path = set()

            def visit(vertex: str) -> Optional[Tuple[str, str]]:
                path.add(vertex)
                for neighbour in g.get(vertex, ()):
                    if neighbour in path or visit(neighbour):
                        return (vertex, neighbour)
                path.remove(vertex)
                return None

            for v in g:
                cycle = visit(v)
                if cycle:
                    return cycle
            return None

        cycle = find_cycle(hierarchy)
        if cycle:
            raise QueryObjectValidationError(
                _(
                    "There's a loop in your Sankey, please provide a tree. "
                    "Here's a faulty link: {}"
                ).format(cycle)
            )
        return recs


class ChordViz(BaseViz):

    """A Chord diagram"""

    viz_type = "chord"
    verbose_name = _("Directed Force Layout")
    credits = '<a href="https://github.com/d3/d3-chord">Bostock</a>'
    is_timeseries = False

    def query_obj(self) -> QueryObjectDict:
        qry = super().query_obj()
        fd = self.form_data
        qry["groupby"] = [fd.get("groupby"), fd.get("columns")]
        qry["metrics"] = [fd.get("metric")]
        if self.form_data.get("sort_by_metric", False):
            qry["orderby"] = [(qry["metrics"][0], False)]
        return qry

    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None

        df.columns = ["source", "target", "value"]

        # Preparing a symetrical matrix like d3.chords calls for
        nodes = list(set(df["source"]) | set(df["target"]))
        matrix = {}
        for source, target in product(nodes, nodes):
            matrix[(source, target)] = 0
        for source, target, value in df.to_records(index=False):
            matrix[(source, target)] = value
        m = [[matrix[(n1, n2)] for n1 in nodes] for n2 in nodes]
        return {"nodes": list(nodes), "matrix": m}


class CountryMapViz(BaseViz):

    """A country centric"""

    viz_type = "country_map"
    verbose_name = _("Country Map")
    is_timeseries = False
    credits = "From bl.ocks.org By john-guerra"

    def query_obj(self) -> QueryObjectDict:
        qry = super().query_obj()
        metric = self.form_data.get("metric")
        entity = self.form_data.get("entity")
        if not self.form_data.get("select_country"):
            raise QueryObjectValidationError("Must specify a country")
        if not metric:
            raise QueryObjectValidationError("Must specify a metric")
        if not entity:
            raise QueryObjectValidationError("Must provide ISO codes")
        qry["metrics"] = [metric]
        qry["groupby"] = [entity]
        return qry

    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None
        fd = self.form_data
        cols = [fd.get("entity")]
        metric = self.metric_labels[0]
        cols += [metric]
        ndf = df[cols]
        df = ndf
        df.columns = ["country_id", "metric"]
        d = df.to_dict(orient="records")
        return d


class WorldMapViz(BaseViz):

    """A country centric world map"""

    viz_type = "world_map"
    verbose_name = _("World Map")
    is_timeseries = False
    credits = 'datamaps on <a href="https://www.npmjs.com/package/datamaps">npm</a>'

    def query_obj(self) -> QueryObjectDict:
        qry = super().query_obj()
        qry["groupby"] = [self.form_data["entity"]]
        if self.form_data.get("sort_by_metric", False):
            qry["orderby"] = [(qry["metrics"][0], False)]
        return qry

    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None

        from superset.examples import countries

        fd = self.form_data
        cols = [fd.get("entity")]
        metric = utils.get_metric_name(fd["metric"])
        secondary_metric = (
            utils.get_metric_name(fd["secondary_metric"])
            if "secondary_metric" in fd
            else None
        )
        columns = ["country", "m1", "m2"]
        if metric == secondary_metric:
            ndf = df[cols]
            ndf["m1"] = df[metric]
            ndf["m2"] = ndf["m1"]
        else:
            if secondary_metric:
                cols += [metric, secondary_metric]
            else:
                cols += [metric]
                columns = ["country", "m1"]
            ndf = df[cols]
        df = ndf
        df.columns = columns
        d = df.to_dict(orient="records")
        for row in d:
            country = None
            if isinstance(row["country"], str):
                if "country_fieldtype" in fd:
                    country = countries.get(fd["country_fieldtype"], row["country"])
            if country:
                row["country"] = country["cca3"]
                row["latitude"] = country["lat"]
                row["longitude"] = country["lng"]
                row["name"] = country["name"]
            else:
                row["country"] = "XXX"
        return d


class FilterBoxViz(BaseViz):

    """A multi filter, multi-choice filter box to make dashboards interactive"""

    viz_type = "filter_box"
    verbose_name = _("Filters")
    is_timeseries = False
    credits = 'a <a href="https://github.com/airbnb/superset">Superset</a> original'
    cache_type = "get_data"
    filter_row_limit = 1000

    def query_obj(self) -> QueryObjectDict:
        return {}

    def run_extra_queries(self) -> None:
        from superset.common.query_context import QueryContext

        qry = super().query_obj()
        filters = self.form_data.get("filter_configs") or []
        qry["row_limit"] = self.filter_row_limit
        self.dataframes = {}
        for flt in filters:
            col = flt.get("column")
            if not col:
                raise QueryObjectValidationError(
                    _("Invalid filter configuration, please select a column")
                )
            qry["groupby"] = [col]
            metric = flt.get("metric")
            qry["metrics"] = [metric] if metric else []
            asc = flt.get("asc")
            if metric and asc is not None:
                qry["orderby"] = [(metric, asc)]
            QueryContext(
                datasource={"id": self.datasource.id, "type": self.datasource.type},
                queries=[qry],
            ).raise_for_access()
            df = self.get_df_payload(query_obj=qry).get("df")
            self.dataframes[col] = df

    def get_data(self, df: pd.DataFrame) -> VizData:
        filters = self.form_data.get("filter_configs") or []
        d = {}
        for flt in filters:
            col = flt.get("column")
            metric = flt.get("metric")
            df = self.dataframes.get(col)
            if df is not None and not df.empty:
                if metric:
                    df = df.sort_values(
                        utils.get_metric_name(metric), ascending=flt.get("asc")
                    )
                    d[col] = [
                        {"id": row[0], "text": row[0], "metric": row[1]}
                        for row in df.itertuples(index=False)
                    ]
                else:
                    df = df.sort_values(col, ascending=flt.get("asc"))
                    d[col] = [
                        {"id": row[0], "text": row[0]}
                        for row in df.itertuples(index=False)
                    ]
            else:
                df[col] = []
        return d


class ParallelCoordinatesViz(BaseViz):

    """Interactive parallel coordinate implementation

    Uses this amazing javascript library
    https://github.com/syntagmatic/parallel-coordinates
    """

    viz_type = "para"
    verbose_name = _("Parallel Coordinates")
    credits = (
        '<a href="https://syntagmatic.github.io/parallel-coordinates/">'
        "Syntagmatic's library</a>"
    )
    is_timeseries = False

    def query_obj(self) -> QueryObjectDict:
        d = super().query_obj()
        fd = self.form_data
        d["groupby"] = [fd.get("series")]
        sort_by = self.form_data.get("timeseries_limit_metric")
        if sort_by:
            sort_by_label = utils.get_metric_name(sort_by)
            if sort_by_label not in utils.get_metric_names(d["metrics"]):
                d["metrics"].append(sort_by)
            if self.form_data.get("order_desc"):
                d["orderby"] = [(sort_by, not self.form_data.get("order_desc", True))]
        return d

    def get_data(self, df: pd.DataFrame) -> VizData:
        return df.to_dict(orient="records")


class HeatmapViz(BaseViz):

    """A nice heatmap visualization that support high density through canvas"""

    viz_type = "heatmap"
    verbose_name = _("Heatmap")
    is_timeseries = False
    credits = (
        'inspired from mbostock @<a href="http://bl.ocks.org/mbostock/3074470">'
        "bl.ocks.org</a>"
    )

    def query_obj(self) -> QueryObjectDict:
        d = super().query_obj()
        fd = self.form_data
        d["metrics"] = [fd.get("metric")]
        d["groupby"] = [fd.get("all_columns_x"), fd.get("all_columns_y")]

        if self.form_data.get("sort_by_metric", False):
            d["orderby"] = [(d["metrics"][0], False)]

        return d

    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None

        fd = self.form_data
        x = fd.get("all_columns_x")
        y = fd.get("all_columns_y")
        v = self.metric_labels[0]
        if x == y:
            df.columns = ["x", "y", "v"]
        else:
            df = df[[x, y, v]]
            df.columns = ["x", "y", "v"]
        norm = fd.get("normalize_across")
        overall = False
        max_ = df.v.max()
        min_ = df.v.min()
        if norm == "heatmap":
            overall = True
        else:
            gb = df.groupby(norm, group_keys=False)
            if len(gb) <= 1:
                overall = True
            else:
                df["perc"] = gb.apply(
                    lambda x: (x.v - x.v.min()) / (x.v.max() - x.v.min())
                )
                df["rank"] = gb.apply(lambda x: x.v.rank(pct=True))
        if overall:
            df["perc"] = (df.v - min_) / (max_ - min_)
            df["rank"] = df.v.rank(pct=True)
        return {"records": df.to_dict(orient="records"), "extents": [min_, max_]}


class HorizonViz(NVD3TimeSeriesViz):

    """Horizon chart

    https://www.npmjs.com/package/d3-horizon-chart
    """

    viz_type = "horizon"
    verbose_name = _("Horizon Charts")
    credits = (
        '<a href="https://www.npmjs.com/package/d3-horizon-chart">'
        "d3-horizon-chart</a>"
    )

    def query_obj(self) -> QueryObjectDict:
        d = super().query_obj()
        metrics = self.form_data.get("metrics")
        sort_by = self.form_data.get("timeseries_limit_metric")
        if sort_by:
            sort_by_label = utils.get_metric_name(sort_by)
            if sort_by_label not in utils.get_metric_names(d["metrics"]):
                d["metrics"].append(sort_by)
            if self.form_data.get("order_desc"):
                d["orderby"] = [(sort_by, not self.form_data.get("order_desc", True))]
        return d


class MapboxViz(BaseViz):

    """Rich maps made with Mapbox"""

    viz_type = "mapbox"
    verbose_name = _("Mapbox")
    is_timeseries = False
    credits = "<a href=https://www.mapbox.com/mapbox-gl-js/api/>Mapbox GL JS</a>"

    def query_obj(self) -> QueryObjectDict:
        d = super().query_obj()
        fd = self.form_data
        label_col = fd.get("mapbox_label")

        if not fd.get("groupby"):
            if fd.get("all_columns_x") is None or fd.get("all_columns_y") is None:
                raise QueryObjectValidationError(
                    _("[Longitude] and [Latitude] must be set")
                )
            d["columns"] = [fd.get("all_columns_x"), fd.get("all_columns_y")]

            if label_col and len(label_col) >= 1:
                if label_col[0] == "count":
                    raise QueryObjectValidationError(
                        _(
                            "Must have a [Group By] column to have 'count' as the "
                            + "[Label]"
                        )
                    )
                d["columns"].append(label_col[0])

            if fd.get("point_radius") != "Auto":
                d["columns"].append(fd.get("point_radius"))

            d["columns"] = list(set(d["columns"]))
        else:
            # Ensuring columns chosen are all in group by
            if (
                label_col
                and len(label_col) >= 1
                and label_col[0] != "count"
                and label_col[0] not in fd["groupby"]
            ):
                raise QueryObjectValidationError(
                    _("Choice of [Label] must be present in [Group By]")
                )

            if (
                fd.get("point_radius") != "Auto"
                and fd.get("point_radius") not in fd["groupby"]
            ):
                raise QueryObjectValidationError(
                    _("Choice of [Point Radius] must be present in [Group By]")
                )

            if (
                fd.get("all_columns_x") not in fd["groupby"]
                or fd.get("all_columns_y") not in fd["groupby"]
            ):
                raise QueryObjectValidationError(
                    _(
                        "[Longitude] and [Latitude] columns must be present in "
                        + "[Group By]"
                    )
                )
        return d

    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None

        fd = self.form_data
        label_col = fd.get("mapbox_label")
        has_custom_metric = label_col is not None and len(label_col) > 0
        metric_col = [None] * len(df.index)
        if has_custom_metric:
            if label_col[0] == fd.get("all_columns_x"):  # type: ignore
                metric_col = df[fd.get("all_columns_x")]
            elif label_col[0] == fd.get("all_columns_y"):  # type: ignore
                metric_col = df[fd.get("all_columns_y")]
            else:
                metric_col = df[label_col[0]]  # type: ignore
        point_radius_col = (
            [None] * len(df.index)
            if fd.get("point_radius") == "Auto"
            else df[fd.get("point_radius")]
        )

        # limiting geo precision as long decimal values trigger issues
        # around json-bignumber in Mapbox
        GEO_PRECISION = 10
        # using geoJSON formatting
        geo_json = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"metric": metric, "radius": point_radius},
                    "geometry": {
                        "type": "Point",
                        "coordinates": [
                            round(lon, GEO_PRECISION),
                            round(lat, GEO_PRECISION),
                        ],
                    },
                }
                for lon, lat, metric, point_radius in zip(
                    df[fd.get("all_columns_x")],
                    df[fd.get("all_columns_y")],
                    metric_col,
                    point_radius_col,
                )
            ],
        }

        x_series, y_series = df[fd.get("all_columns_x")], df[fd.get("all_columns_y")]
        south_west = [x_series.min(), y_series.min()]
        north_east = [x_series.max(), y_series.max()]

        return {
            "geoJSON": geo_json,
            "hasCustomMetric": has_custom_metric,
            "mapboxApiKey": config["MAPBOX_API_KEY"],
            "mapStyle": fd.get("mapbox_style"),
            "aggregatorName": fd.get("pandas_aggfunc"),
            "clusteringRadius": fd.get("clustering_radius"),
            "pointRadiusUnit": fd.get("point_radius_unit"),
            "globalOpacity": fd.get("global_opacity"),
            "bounds": [south_west, north_east],
            "renderWhileDragging": fd.get("render_while_dragging"),
            "tooltip": fd.get("rich_tooltip"),
            "color": fd.get("mapbox_color"),
        }


class DeckGLMultiLayer(BaseViz):

    """Pile on multiple DeckGL layers"""

    viz_type = "deck_multi"
    verbose_name = _("Deck.gl - Multiple Layers")

    is_timeseries = False
    credits = '<a href="https://uber.github.io/deck.gl/">deck.gl</a>'

    def query_obj(self) -> QueryObjectDict:
        return {}

    def get_data(self, df: pd.DataFrame) -> VizData:
        fd = self.form_data
        # Late imports to avoid circular import issues
        from superset import db
        from superset.models.slice import Slice

        slice_ids = fd.get("deck_slices")
        slices = db.session.query(Slice).filter(Slice.id.in_(slice_ids)).all()
        return {
            "mapboxApiKey": config["MAPBOX_API_KEY"],
            "slices": [slc.data for slc in slices],
        }


class BaseDeckGLViz(BaseViz):

    """Base class for deck.gl visualizations"""

    is_timeseries = False
    credits = '<a href="https://uber.github.io/deck.gl/">deck.gl</a>'
    spatial_control_keys: List[str] = []

    def get_metrics(self) -> List[str]:
        self.metric = self.form_data.get("size")
        return [self.metric] if self.metric else []

    def process_spatial_query_obj(self, key: str, group_by: List[str]) -> None:
        group_by.extend(self.get_spatial_columns(key))

    def get_spatial_columns(self, key: str) -> List[str]:
        spatial = self.form_data.get(key)
        if spatial is None:
            raise ValueError(_("Bad spatial key"))

        if spatial.get("type") == "latlong":
            return [spatial.get("lonCol"), spatial.get("latCol")]
        elif spatial.get("type") == "delimited":
            return [spatial.get("lonlatCol")]
        elif spatial.get("type") == "geohash":
            return [spatial.get("geohashCol")]
        return []

    @staticmethod
    def parse_coordinates(s: Any) -> Optional[Tuple[float, float]]:
        if not s:
            return None
        try:
            p = Point(s)
            return (p.latitude, p.longitude)
        except Exception:
            raise SpatialException(_("Invalid spatial point encountered: %s" % s))

    @staticmethod
    def reverse_geohash_decode(geohash_code: str) -> Tuple[str, str]:
        lat, lng = geohash.decode(geohash_code)
        return (lng, lat)

    @staticmethod
    def reverse_latlong(df: pd.DataFrame, key: str) -> None:
        df[key] = [tuple(reversed(o)) for o in df[key] if isinstance(o, (list, tuple))]

    def process_spatial_data_obj(self, key: str, df: pd.DataFrame) -> pd.DataFrame:
        spatial = self.form_data.get(key)
        if spatial is None:
            raise ValueError(_("Bad spatial key"))

        if spatial.get("type") == "latlong":
            df[key] = list(
                zip(
                    pd.to_numeric(df[spatial.get("lonCol")], errors="coerce"),
                    pd.to_numeric(df[spatial.get("latCol")], errors="coerce"),
                )
            )
        elif spatial.get("type") == "delimited":
            lon_lat_col = spatial.get("lonlatCol")
            df[key] = df[lon_lat_col].apply(self.parse_coordinates)
            del df[lon_lat_col]
        elif spatial.get("type") == "geohash":
            df[key] = df[spatial.get("geohashCol")].map(self.reverse_geohash_decode)
            del df[spatial.get("geohashCol")]

        if spatial.get("reverseCheckbox"):
            self.reverse_latlong(df, key)

        if df.get(key) is None:
            raise NullValueException(
                _(
                    "Encountered invalid NULL spatial entry, \
                                       please consider filtering those out"
                )
            )
        return df

    def add_null_filters(self) -> None:
        fd = self.form_data
        spatial_columns = set()
        for key in self.spatial_control_keys:
            for column in self.get_spatial_columns(key):
                spatial_columns.add(column)

        if fd.get("adhoc_filters") is None:
            fd["adhoc_filters"] = []

        line_column = fd.get("line_column")
        if line_column:
            spatial_columns.add(line_column)

        for column in sorted(spatial_columns):
            filter_ = to_adhoc({"col": column, "op": "IS NOT NULL", "val": ""})
            fd["adhoc_filters"].append(filter_)

    def query_obj(self) -> QueryObjectDict:
        fd = self.form_data

        # add NULL filters
        if fd.get("filter_nulls", True):
            self.add_null_filters()

        d = super().query_obj()
        gb: List[str] = []

        for key in self.spatial_control_keys:
            self.process_spatial_query_obj(key, gb)

        if fd.get("dimension"):
            gb += [fd["dimension"]]

        if fd.get("js_columns"):
            gb += fd.get("js_columns") or []
        metrics = self.get_metrics()
        gb = list(set(gb))
        if metrics:
            d["groupby"] = gb
            d["metrics"] = metrics
            d["columns"] = []
            first_metric = d["metrics"][0]
            d["orderby"] = [(first_metric, not fd.get("order_desc", True))]
        else:
            d["columns"] = gb
        return d

    def get_js_columns(self, d: Dict[str, Any]) -> Dict[str, Any]:
        cols = self.form_data.get("js_columns") or []
        return {col: d.get(col) for col in cols}

    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None

        # Processing spatial info
        for key in self.spatial_control_keys:
            df = self.process_spatial_data_obj(key, df)

        features = []
        for d in df.to_dict(orient="records"):
            feature = self.get_properties(d)
            extra_props = self.get_js_columns(d)
            if extra_props:
                feature["extraProps"] = extra_props
            features.append(feature)

        return {
            "features": features,
            "mapboxApiKey": config["MAPBOX_API_KEY"],
            "metricLabels": self.metric_labels,
        }

    def get_properties(self, d: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError()


class DeckScatterViz(BaseDeckGLViz):

    """deck.gl's ScatterLayer"""

    viz_type = "deck_scatter"
    verbose_name = _("Deck.gl - Scatter plot")
    spatial_control_keys = ["spatial"]
    is_timeseries = True

    def query_obj(self) -> QueryObjectDict:
        fd = self.form_data
        self.is_timeseries = bool(fd.get("time_grain_sqla") or fd.get("granularity"))
        self.point_radius_fixed = fd.get("point_radius_fixed") or {
            "type": "fix",
            "value": 500,
        }
        return super().query_obj()

    def get_metrics(self) -> List[str]:
        self.metric = None
        if self.point_radius_fixed.get("type") == "metric":
            self.metric = self.point_radius_fixed["value"]
            return [self.metric]
        return []

    def get_properties(self, d: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "metric": d.get(self.metric_label) if self.metric_label else None,
            "radius": self.fixed_value
            if self.fixed_value
            else d.get(self.metric_label)
            if self.metric_label
            else None,
            "cat_color": d.get(self.dim) if self.dim else None,
            "position": d.get("spatial"),
            DTTM_ALIAS: d.get(DTTM_ALIAS),
        }

    def get_data(self, df: pd.DataFrame) -> VizData:
        fd = self.form_data
        self.metric_label = utils.get_metric_name(self.metric) if self.metric else None
        self.point_radius_fixed = fd.get("point_radius_fixed")
        self.fixed_value = None
        self.dim = self.form_data.get("dimension")
        if self.point_radius_fixed and self.point_radius_fixed.get("type") != "metric":
            self.fixed_value = self.point_radius_fixed.get("value")
        return super().get_data(df)


class DeckScreengrid(BaseDeckGLViz):

    """deck.gl's ScreenGridLayer"""

    viz_type = "deck_screengrid"
    verbose_name = _("Deck.gl - Screen Grid")
    spatial_control_keys = ["spatial"]
    is_timeseries = True

    def query_obj(self) -> QueryObjectDict:
        fd = self.form_data
        self.is_timeseries = bool(fd.get("time_grain_sqla") or fd.get("granularity"))
        return super().query_obj()

    def get_properties(self, d: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "position": d.get("spatial"),
            "weight": (d.get(self.metric_label) if self.metric_label else None) or 1,
            "__timestamp": d.get(DTTM_ALIAS) or d.get("__time"),
        }

    def get_data(self, df: pd.DataFrame) -> VizData:
        self.metric_label = utils.get_metric_name(self.metric) if self.metric else None
        return super().get_data(df)


class DeckGrid(BaseDeckGLViz):

    """deck.gl's DeckLayer"""

    viz_type = "deck_grid"
    verbose_name = _("Deck.gl - 3D Grid")
    spatial_control_keys = ["spatial"]

    def get_properties(self, d: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "position": d.get("spatial"),
            "weight": (d.get(self.metric_label) if self.metric_label else None) or 1,
        }

    def get_data(self, df: pd.DataFrame) -> VizData:
        self.metric_label = utils.get_metric_name(self.metric) if self.metric else None
        return super().get_data(df)


def geohash_to_json(geohash_code: str) -> List[List[float]]:
    p = geohash.bbox(geohash_code)
    return [
        [p.get("w"), p.get("n")],
        [p.get("e"), p.get("n")],
        [p.get("e"), p.get("s")],
        [p.get("w"), p.get("s")],
        [p.get("w"), p.get("n")],
    ]


class DeckPathViz(BaseDeckGLViz):

    """deck.gl's PathLayer"""

    viz_type = "deck_path"
    verbose_name = _("Deck.gl - Paths")
    deck_viz_key = "path"
    is_timeseries = True
    deser_map = {
        "json": json.loads,
        "polyline": polyline.decode,
        "geohash": geohash_to_json,
    }

    def query_obj(self) -> QueryObjectDict:
        fd = self.form_data
        self.is_timeseries = bool(fd.get("time_grain_sqla") or fd.get("granularity"))
        d = super().query_obj()
        self.metric = fd.get("metric")
        line_col = fd.get("line_column")
        if d["metrics"]:
            self.has_metrics = True
            d["groupby"].append(line_col)
        else:
            self.has_metrics = False
            d["columns"].append(line_col)
        return d

    def get_properties(self, d: Dict[str, Any]) -> Dict[str, Any]:
        fd = self.form_data
        line_type = fd["line_type"]
        deser = self.deser_map[line_type]
        line_column = fd["line_column"]
        path = deser(d[line_column])
        if fd.get("reverse_long_lat"):
            path = [(o[1], o[0]) for o in path]
        d[self.deck_viz_key] = path
        if line_type != "geohash":
            del d[line_column]
        d["__timestamp"] = d.get(DTTM_ALIAS) or d.get("__time")
        return d

    def get_data(self, df: pd.DataFrame) -> VizData:
        self.metric_label = utils.get_metric_name(self.metric) if self.metric else None
        return super().get_data(df)


class DeckPolygon(DeckPathViz):

    """deck.gl's Polygon Layer"""

    viz_type = "deck_polygon"
    deck_viz_key = "polygon"
    verbose_name = _("Deck.gl - Polygon")

    def query_obj(self) -> QueryObjectDict:
        fd = self.form_data
        self.elevation = fd.get("point_radius_fixed") or {"type": "fix", "value": 500}
        return super().query_obj()

    def get_metrics(self) -> List[str]:
        metrics = [self.form_data.get("metric")]
        if self.elevation.get("type") == "metric":
            metrics.append(self.elevation.get("value"))
        return [metric for metric in metrics if metric]

    def get_properties(self, d: Dict[str, Any]) -> Dict[str, Any]:
        super().get_properties(d)
        fd = self.form_data
        elevation = fd["point_radius_fixed"]["value"]
        type_ = fd["point_radius_fixed"]["type"]
        d["elevation"] = (
            d.get(utils.get_metric_name(elevation)) if type_ == "metric" else elevation
        )
        return d


class DeckHex(BaseDeckGLViz):

    """deck.gl's DeckLayer"""

    viz_type = "deck_hex"
    verbose_name = _("Deck.gl - 3D HEX")
    spatial_control_keys = ["spatial"]

    def get_properties(self, d: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "position": d.get("spatial"),
            "weight": (d.get(self.metric_label) if self.metric_label else None) or 1,
        }

    def get_data(self, df: pd.DataFrame) -> VizData:
        self.metric_label = utils.get_metric_name(self.metric) if self.metric else None
        return super(DeckHex, self).get_data(df)


class DeckGeoJson(BaseDeckGLViz):

    """deck.gl's GeoJSONLayer"""

    viz_type = "deck_geojson"
    verbose_name = _("Deck.gl - GeoJSON")

    def query_obj(self) -> QueryObjectDict:
        d = super().query_obj()
        d["columns"] += [self.form_data.get("geojson")]
        d["metrics"] = []
        d["groupby"] = []
        return d

    def get_properties(self, d: Dict[str, Any]) -> Dict[str, Any]:
        geojson = d[self.form_data["geojson"]]
        return json.loads(geojson)


class DeckArc(BaseDeckGLViz):

    """deck.gl's Arc Layer"""

    viz_type = "deck_arc"
    verbose_name = _("Deck.gl - Arc")
    spatial_control_keys = ["start_spatial", "end_spatial"]
    is_timeseries = True

    def query_obj(self) -> QueryObjectDict:
        fd = self.form_data
        self.is_timeseries = bool(fd.get("time_grain_sqla") or fd.get("granularity"))
        return super().query_obj()

    def get_properties(self, d: Dict[str, Any]) -> Dict[str, Any]:
        dim = self.form_data.get("dimension")
        return {
            "sourcePosition": d.get("start_spatial"),
            "targetPosition": d.get("end_spatial"),
            "cat_color": d.get(dim) if dim else None,
            DTTM_ALIAS: d.get(DTTM_ALIAS),
        }

    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None

        d = super().get_data(df)

        return {
            "features": d["features"],  # type: ignore
            "mapboxApiKey": config["MAPBOX_API_KEY"],
        }


class EventFlowViz(BaseViz):

    """A visualization to explore patterns in event sequences"""

    viz_type = "event_flow"
    verbose_name = _("Event flow")
    credits = 'from <a href="https://github.com/williaster/data-ui">@data-ui</a>'
    is_timeseries = True

    def query_obj(self) -> QueryObjectDict:
        query = super().query_obj()
        form_data = self.form_data

        event_key = form_data["all_columns_x"]
        entity_key = form_data["entity"]
        meta_keys = [
            col
            for col in form_data["all_columns"] or []
            if col != event_key and col != entity_key
        ]

        query["columns"] = [event_key, entity_key] + meta_keys

        if form_data["order_by_entity"]:
            query["orderby"] = [(entity_key, True)]

        return query

    def get_data(self, df: pd.DataFrame) -> VizData:
        return df.to_dict(orient="records")


class PairedTTestViz(BaseViz):

    """A table displaying paired t-test values"""

    viz_type = "paired_ttest"
    verbose_name = _("Time Series - Paired t-test")
    sort_series = False
    is_timeseries = True

    def query_obj(self) -> QueryObjectDict:
        d = super().query_obj()
        metrics = self.form_data.get("metrics")
        sort_by = self.form_data.get("timeseries_limit_metric")
        if sort_by:
            sort_by_label = utils.get_metric_name(sort_by)
            if sort_by_label not in utils.get_metric_names(d["metrics"]):
                d["metrics"].append(sort_by)
            if self.form_data.get("order_desc"):
                d["orderby"] = [(sort_by, not self.form_data.get("order_desc", True))]
        return d

    def get_data(self, df: pd.DataFrame) -> VizData:
        """
        Transform received data frame into an object of the form:
        {
            'metric1': [
                {
                    groups: ('groupA', ... ),
                    values: [ {x, y}, ... ],
                }, ...
            ], ...
        }
        """

        if df.empty:
            return None

        fd = self.form_data
        groups = fd.get("groupby")
        metrics = self.metric_labels
        df = df.pivot_table(index=DTTM_ALIAS, columns=groups, values=metrics)
        cols = []
        # Be rid of falsey keys
        for col in df.columns:
            if col == "":
                cols.append("N/A")
            elif col is None:
                cols.append("NULL")
            else:
                cols.append(col)
        df.columns = cols
        data: Dict[str, List[Dict[str, Any]]] = {}
        series = df.to_dict("series")
        for nameSet in df.columns:
            # If no groups are defined, nameSet will be the metric name
            hasGroup = not isinstance(nameSet, str)
            Y = series[nameSet]
            d = {
                "group": nameSet[1:] if hasGroup else "All",
                "values": [{"x": t, "y": Y[t] if t in Y else None} for t in df.index],
            }
            key = nameSet[0] if hasGroup else nameSet
            if key in data:
                data[key].append(d)
            else:
                data[key] = [d]
        return data


class RoseViz(NVD3TimeSeriesViz):

    viz_type = "rose"
    verbose_name = _("Time Series - Nightingale Rose Chart")
    sort_series = False
    is_timeseries = True

    def query_obj(self) -> QueryObjectDict:
        d = super().query_obj()
        sort_by = self.form_data.get("timeseries_limit_metric")
        if sort_by:
            sort_by_label = utils.get_metric_name(sort_by)
            if sort_by_label not in utils.get_metric_names(d["metrics"]):
                d["metrics"].append(sort_by)
            if self.form_data.get("order_desc"):
                d["orderby"] = [(sort_by, not self.form_data.get("order_desc", True))]
        return d

    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None

        data = super().get_data(df)
        result: Dict[str, List[Dict[str, str]]] = {}
        for datum in data:  # type: ignore
            key = datum["key"]
            for val in datum["values"]:
                timestamp = val["x"].value
                if not result.get(timestamp):
                    result[timestamp] = []
                value = 0 if math.isnan(val["y"]) else val["y"]
                result[timestamp].append(
                    {
                        "key": key,
                        "value": value,
                        "name": ", ".join(key) if isinstance(key, list) else key,
                        "time": val["x"],
                    }
                )
        return result


class PartitionViz(NVD3TimeSeriesViz):

    """
    A hierarchical data visualization with support for time series.
    """

    viz_type = "partition"
    verbose_name = _("Partition Diagram")

    def query_obj(self) -> QueryObjectDict:
        query_obj = super().query_obj()
        time_op = self.form_data.get("time_series_option", "not_time")
        # Return time series data if the user specifies so
        query_obj["is_timeseries"] = time_op != "not_time"
        sort_by = self.form_data.get("timeseries_limit_metric")
        if sort_by:
            sort_by_label = utils.get_metric_name(sort_by)
            if sort_by_label not in utils.get_metric_names(query_obj["metrics"]):
                query_obj["metrics"].append(sort_by)
            query_obj["orderby"] = [
                (sort_by, not self.form_data.get("order_desc", True))
            ]
        return query_obj

    def levels_for(
        self, time_op: str, groups: List[str], df: pd.DataFrame
    ) -> Dict[int, pd.Series]:
        """
        Compute the partition at each `level` from the dataframe.
        """
        levels = {}
        for i in range(0, len(groups) + 1):
            agg_df = df.groupby(groups[:i]) if i else df
            levels[i] = (
                agg_df.mean()
                if time_op == "agg_mean"
                else agg_df.sum(numeric_only=True)
            )
        return levels

    def levels_for_diff(
        self, time_op: str, groups: List[str], df: pd.DataFrame
    ) -> Dict[int, pd.DataFrame]:
        # Obtain a unique list of the time grains
        times = list(set(df[DTTM_ALIAS]))
        times.sort()
        until = times[len(times) - 1]
        since = times[0]
        # Function describing how to calculate the difference
        func = {
            "point_diff": [pd.Series.sub, lambda a, b, fill_value: a - b],
            "point_factor": [pd.Series.div, lambda a, b, fill_value: a / float(b)],
            "point_percent": [
                lambda a, b, fill_value=0: a.div(b, fill_value=fill_value) - 1,
                lambda a, b, fill_value: a / float(b) - 1,
            ],
        }[time_op]
        agg_df = df.groupby(DTTM_ALIAS).sum()
        levels = {
            0: pd.Series(
                {
                    m: func[1](agg_df[m][until], agg_df[m][since], 0)
                    for m in agg_df.columns
                }
            )
        }
        for i in range(1, len(groups) + 1):
            agg_df = df.groupby([DTTM_ALIAS] + groups[:i]).sum()
            levels[i] = pd.DataFrame(
                {
                    m: func[0](agg_df[m][until], agg_df[m][since], fill_value=0)
                    for m in agg_df.columns
                }
            )
        return levels

    def levels_for_time(
        self, groups: List[str], df: pd.DataFrame
    ) -> Dict[int, VizData]:
        procs = {}
        for i in range(0, len(groups) + 1):
            self.form_data["groupby"] = groups[:i]
            df_drop = df.drop(groups[i:], 1)
            procs[i] = self.process_data(df_drop, aggregate=True)
        self.form_data["groupby"] = groups
        return procs

    def nest_values(
        self,
        levels: Dict[int, pd.DataFrame],
        level: int = 0,
        metric: Optional[str] = None,
        dims: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Nest values at each level on the back-end with
        access and setting, instead of summing from the bottom.
        """
        if dims is None:
            dims = []
        if not level:
            return [
                {
                    "name": m,
                    "val": levels[0][m],
                    "children": self.nest_values(levels, 1, m),
                }
                for m in levels[0].index
            ]
        if level == 1:
            metric_level = levels[1][metric]
            return [
                {
                    "name": i,
                    "val": metric_level[i],
                    "children": self.nest_values(levels, 2, metric, [i]),
                }
                for i in metric_level.index
            ]
        if level >= len(levels):
            return []
        dim_level = levels[level][metric][[dims[0]]]
        return [
            {
                "name": i,
                "val": dim_level[i],
                "children": self.nest_values(levels, level + 1, metric, dims + [i]),
            }
            for i in dim_level.index
        ]

    def nest_procs(
        self,
        procs: Dict[int, pd.DataFrame],
        level: int = -1,
        dims: Optional[Tuple[str, ...]] = None,
        time: Any = None,
    ) -> List[Dict[str, Any]]:
        if dims is None:
            dims = ()
        if level == -1:
            return [
                {"name": m, "children": self.nest_procs(procs, 0, (m,))}
                for m in procs[0].columns
            ]
        if not level:
            return [
                {
                    "name": t,
                    "val": procs[0][dims[0]][t],
                    "children": self.nest_procs(procs, 1, dims, t),
                }
                for t in procs[0].index
            ]
        if level >= len(procs):
            return []
        return [
            {
                "name": i,
                "val": procs[level][dims][i][time],
                "children": self.nest_procs(procs, level + 1, dims + (i,), time),
            }
            for i in procs[level][dims].columns
        ]

    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None
        fd = self.form_data
        groups = fd.get("groupby", [])
        time_op = fd.get("time_series_option", "not_time")
        if not len(groups):
            raise ValueError("Please choose at least one groupby")
        if time_op == "not_time":
            levels = self.levels_for("agg_sum", groups, df)
        elif time_op in ["agg_sum", "agg_mean"]:
            levels = self.levels_for(time_op, groups, df)
        elif time_op in ["point_diff", "point_factor", "point_percent"]:
            levels = self.levels_for_diff(time_op, groups, df)
        elif time_op == "adv_anal":
            procs = self.levels_for_time(groups, df)
            return self.nest_procs(procs)
        else:
            levels = self.levels_for("agg_sum", [DTTM_ALIAS] + groups, df)
        return self.nest_values(levels)


def get_subclasses(cls: Type[BaseViz]) -> Set[Type[BaseViz]]:
    return set(cls.__subclasses__()).union(
        [sc for c in cls.__subclasses__() for sc in get_subclasses(c)]
    )


viz_types = {
    o.viz_type: o
    for o in get_subclasses(BaseViz)
    if o.viz_type not in config["VIZ_TYPE_DENYLIST"]
}
